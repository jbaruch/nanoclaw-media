#!/usr/bin/env python3
"""Bounded release verification for `tessl__check-watchlist`.

Replaces SKILL.md Step 2's free-form web search. That search was
satisfied on 2026-08-14/15 by a spawned `general-purpose` subagent doing
`fetch_markdown` + `curl`; the fetches stalled, the maintenance
container emitted no SDK events for 300s, and the host inactivity
watchdog reaped it (exit 137) **before** Step 3 ran. Step 3 both sends
the notification and flips `notified` — so a killed run delivered no
alert and marked nothing, and the identical set was re-checked and
re-killed the next night (jbaruch/nanoclaw-media#67).

This script answers the same question against a structured source with
a hard wall-clock ceiling, so the verification can no longer outlive the
maintenance slot.

Source
------
TVmaze (`https://api.tvmaze.com`, no auth, JSON):
  1. `/search/shows?q=<title>` — resolve the title to a show id.
  2. `/shows/<id>/seasons` — per-season `premiereDate`, when the
     watchlist title names a season (`Fauda S5`, `MobLand Season 2`).
Series-level entries use the show's own `premiered` date instead.

Matching is deliberately strict: the resolved show's `name` must equal
the season-stripped watchlist title after casefolding and whitespace
collapse. A near-miss returns `unknown` rather than guessing, and the
skill falls back to a bounded search for those titles only.

A premiere date alone does not mean the owner can watch it. TVmaze
reports the FIRST airing anywhere — Fauda S5 premiered 2026-05-18 on
Israeli Yes while the watchlist tracks its later Netflix international
drop, the exact false alert #67's stopgap caught by hand. A `released`
verdict is therefore downgraded to `unknown` whenever the entry names a
`platform` the premiere's channel does not corroborate:
`platform_mismatch` when the channel is a different service,
`platform_unverified` when the source names no channel at all. Both
route to the skill's bounded search rather than to an alert.

Bounds (the point of the script)
--------------------------------
  - `PER_CALL_TIMEOUT_SECONDS` caps any single HTTP request.
  - `TOTAL_BUDGET_SECONDS` caps the whole run; entries not reached come
    back `unknown`/`budget_exhausted` and are NOT stamped as checked.
  - `MAX_ENTRIES` caps how many unnotified entries one run resolves;
    the overflow is reported on stdout and stderr, never dropped
    silently.

Bookkeeping (the anti-retry-loop half)
--------------------------------------
Every entry the script actually resolved gets `last_checked` (UTC
`YYYY-MM-DD`) and `last_verdict` written back to watchlist.json
atomically, BEFORE the agent composes or sends anything. A run killed
later still leaves that progress behind, and the precheck's
precision-scaled backoff (see check-watchlist-precheck.py
`_recheck_interval`) stops the same unresolved set from waking the agent
every night. Entries whose lookup never completed (transport error,
budget exhaustion) are left unstamped so an outage cannot mute a title.
A `released` verdict is never suppressed by that backoff — the alert
still has to land.

Environment
-----------
  CHECK_WATCHLIST_PATH — watchlist.json path (default
    /workspace/group/watchlist.json), shared with the precheck.
  TVMAZE_API_BASE — API root override for tests.

Output
------
Single-line JSON on stdout, exit 0:
  {"schema_version": 1, "checked_at": "<ISO UTC>",
   "results": [{"title", "verdict", "detail", "premiere_date",
                "platform", "checked"}],
   "stats": {"entries", "resolved", "released", "unreleased",
             "unknown", "skipped_over_cap"},
   "write_error": "<msg>",     # only when the write-back failed
   "write_skipped": "<msg>",   # only when the record's version is not
                               # this writer's
   "migrated_to_schema_version": 1}  # only on a legacy record's first
                                     # stamp
The two are not equivalent, and the caller acts on them differently
(SKILL.md Steps 2 and 4):
  - `write_error` is a warning. The verdicts are valid and a released
    show must still be notified, so the caller continues and reports
    the diagnostic.
  - `write_skipped` stops the caller. The record is at a version this
    writer does not implement, so nothing may be delivered or marked —
    an alert that cannot be recorded repeats on every later fire.
The writer stamps only a record it authored: no `schema_version`, or
this writer's own. Any other value belongs to a writer this one does
not implement, and rewriting the marker would downgrade a newer record
into a shape nothing understands.

Both diagnostics also go to stderr.

On an unreadable/malformed watchlist: `{"error": "..."}` on stdout, the
same diagnostic on stderr, and exit 1 — matching the `{"error": ...}`
contract the other in-container fetch scripts use. A root without a
`tracking` list counts as malformed: migration upgrades a valid older
record, never legitimizes a broken one.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_WATCHLIST_PATH = "/workspace/group/watchlist.json"
DEFAULT_API_BASE = "https://api.tvmaze.com"

# Version of the `results` payload emitted on stdout. Bump on any shape
# change.
OUTPUT_SCHEMA_VERSION = 1

# Version of the watchlist.json record itself, stamped on the root
# whenever this script writes (contract:
# skills/check-watchlist/state-schema.md). Pre-existing files carry no
# stamp; they are legacy pre-v1 with the same shape, and the first
# write-back brings them up to v1.
WATCHLIST_SCHEMA_VERSION = 1

# Per-request ceiling. TVmaze answers a search in well under a second;
# 10s is generous for a slow link and still 30x under the 300s host
# inactivity watchdog that killed the free-form runs.
PER_CALL_TIMEOUT_SECONDS = 10.0

# Whole-run ceiling, checked before every request. Worst case a run
# spends this plus one in-flight request (bounded above), so the script
# cannot approach the watchdog even if every lookup times out.
TOTAL_BUDGET_SECONDS = 120.0

# Cap on unnotified entries resolved per run: 2 requests each, kept
# under TVmaze's ~20-calls-per-10s rate limit for a single run. The
# watchlist has run to ~5 unnotified entries; the overflow is reported,
# not dropped, and the next run picks it up — entries are ordered
# least-recently-resolved first (`_prioritize`), so the capped set
# rotates and a deferred entry cannot starve behind the same first 12.
MAX_ENTRIES = 12

ERROR_PREVIEW_BYTES = 200

# Stand-in for a result whose entry carries no usable `title`. An empty
# string is indistinguishable from a title the run failed to read, and
# the skill's Step 3 would spend a search slot on it; the sentinel names
# the entry as unsearchable in both the payload and the operator's eye.
UNTITLED = "<untitled entry>"

VERDICT_RELEASED = "released"
VERDICT_UNRELEASED = "unreleased"
VERDICT_UNKNOWN = "unknown"

# Season suffixes the watchlist actually carries: `MobLand Season 2`,
# `Black Doves S2`, `Slow Horses - Season 6`. Anything else is treated
# as a series-level entry and matched on the show's own premiere date.
_SEASON_RE = re.compile(
    r"^(?P<base>.+?)[\s:,\-–—]+(?:season\s*(?P<long>\d{1,2})|s(?P<short>\d{1,2}))$",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# Platform names are compared as canonical slugs, never by substring —
# `Max` is a substring of `Cinemax`, and conflating the two would fire a
# false alert. Stripping non-alphanumerics already reconciles the
# punctuation variants (`Apple TV+` / `Apple TV`, `Paramount+` /
# `Paramount`); this table covers the rest, mapping an alias slug to its
# canonical one. A pair that isn't here simply doesn't match, which
# routes the entry to the skill's bounded search — the safe direction.
_PLATFORM_ALIASES = {
    "amazonprimevideo": "primevideo",
    "amazonprime": "primevideo",
    "appletvplus": "appletv",
    "disneyplus": "disney",
    "paramountplus": "paramount",
    "hbomax": "max",
}


class ApiError(Exception):
    """A TVmaze lookup that did not complete. Never fatal to the run —
    the entry becomes `unknown` and stays unstamped so the next run
    retries it."""


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip().casefold()


def _split_season(title: str) -> tuple[str, int | None]:
    """`("Fauda S5")` -> `("Fauda", 5)`; a title with no season suffix
    comes back unchanged with `None`."""
    match = _SEASON_RE.fullmatch(title.strip())
    if not match:
        return title.strip(), None
    number = match.group("long") or match.group("short")
    return match.group("base").strip(), int(number)


def _api_get(base_url: str, path: str, query: dict[str, str], deadline: float) -> Any:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ApiError("budget_exhausted")
    url = f"{base_url}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(
            request, timeout=min(PER_CALL_TIMEOUT_SECONDS, remaining)
        ) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # 404 on /search is not a thing (it returns []), but /shows/<id>/
        # seasons 404s for a bad id. Either way the entry is unresolved.
        raise ApiError(f"http_{exc.code}") from exc
    except urllib.error.URLError as exc:
        # urlopen(timeout=) surfaces a timeout as URLError with
        # `reason` set to TimeoutError, not as a bare TimeoutError.
        if isinstance(exc.reason, TimeoutError):
            raise ApiError("timeout") from exc
        raise ApiError(f"network: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ApiError("timeout") from exc

    decoded = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(decoded)
    except json.JSONDecodeError as exc:
        preview = raw[:ERROR_PREVIEW_BYTES].decode("utf-8", errors="replace")
        raise ApiError(f"non_json: {exc}; preview={preview!r}") from exc


def _match_show(results: Any, base_title: str) -> dict[str, Any] | None:
    """Exact (casefolded, whitespace-collapsed) name match only. TVmaze
    ranks fuzzy matches high enough that taking the top hit would
    happily resolve a cancelled spin-off; a miss here is recoverable
    (the skill searches), a wrong match would fire a false alert."""
    if not isinstance(results, list):
        return None
    wanted = _normalize(base_title)
    for item in results:
        if not isinstance(item, dict):
            continue
        show = item.get("show")
        if not isinstance(show, dict):
            continue
        name = show.get("name")
        if isinstance(name, str) and _normalize(name) == wanted:
            return show
    return None


def _platform(*candidates: Any) -> str | None:
    """First named `webChannel`/`network` among the objects given, in
    order — season-level first, then the show's."""
    for candidate in candidates:
        if isinstance(candidate, dict):
            name = candidate.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


def _canonical_platform(name: str) -> str:
    slug = _NON_ALNUM_RE.sub("", name.casefold())
    return _PLATFORM_ALIASES.get(slug, slug)


def _platform_matches(expected: object, resolved: str | None) -> bool:
    """Whether a premiere's channel is the platform the entry tracks.

    Canonical-slug equality, never containment. An entry that names no
    platform has nothing to check and matches. An entry that names one
    against a premiere with no channel does NOT match: the alert would
    claim availability on a platform the source says nothing about."""
    if not isinstance(expected, str) or not expected.strip():
        return True
    if not resolved or not resolved.strip():
        return False
    wanted = _canonical_platform(expected)
    actual = _canonical_platform(resolved)
    if not wanted:
        return True
    return wanted == actual


def _find_season(seasons: Any, number: int) -> dict[str, Any] | None:
    if not isinstance(seasons, list):
        return None
    for season in seasons:
        if isinstance(season, dict) and season.get("number") == number:
            return season
    return None


def _result(
    title: str,
    verdict: str,
    detail: str,
    *,
    checked: bool,
    premiere_date: str | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    return {
        "title": title,
        "verdict": verdict,
        "detail": detail,
        "premiere_date": premiere_date,
        "platform": platform,
        "checked": checked,
    }


def _verdict_for(premiere: str, today: date) -> str:
    try:
        premiere_date = date.fromisoformat(premiere)
    except ValueError:
        return VERDICT_UNKNOWN
    return VERDICT_RELEASED if premiere_date <= today else VERDICT_UNRELEASED


def _premiere_result(
    title: str,
    premiere: str,
    detail: str,
    today: date,
    entry_platform: object,
    resolved_platform: str | None,
) -> dict[str, Any]:
    """Verdict for a resolved premiere date, gated on the platform the
    entry tracks — a first airing somewhere else is not a release the
    owner can watch."""
    verdict = _verdict_for(premiere, today)
    if verdict == VERDICT_RELEASED and not _platform_matches(entry_platform, resolved_platform):
        # No channel at all is a different diagnosis from the wrong
        # channel, and the operator reading task_run_logs needs to tell
        # them apart. Both route to the skill's bounded search.
        detail = "platform_mismatch" if resolved_platform else "platform_unverified"
        verdict = VERDICT_UNKNOWN
    return _result(
        title,
        verdict,
        detail,
        checked=True,
        premiere_date=premiere,
        platform=resolved_platform,
    )


def verify_entry(entry: dict[str, Any], today: date, base_url: str, deadline: float) -> dict:
    """Resolve one watchlist entry to a verdict. Never raises: a lookup
    that fails comes back `unknown` with `checked: False`."""
    title = entry.get("title")
    if not isinstance(title, str) or not title.strip():
        return _result(UNTITLED, VERDICT_UNKNOWN, "title_missing", checked=True)
    title = title.strip()
    base_title, season_number = _split_season(title)

    try:
        matches = _api_get(base_url, "/search/shows", {"q": base_title}, deadline)
    except ApiError as exc:
        detail = str(exc)
        return _result(title, VERDICT_UNKNOWN, detail, checked=False)

    show = _match_show(matches, base_title)
    if show is None:
        # TVmaze answered and has no title matching exactly. That is a
        # real answer about TVmaze's catalogue, so it counts as checked
        # and backs off; the skill's bounded search covers the title.
        return _result(title, VERDICT_UNKNOWN, "no_exact_title_match", checked=True)

    show_platform = _platform(show.get("webChannel"), show.get("network"))

    if season_number is None:
        premiere = show.get("premiered")
        if not isinstance(premiere, str) or not premiere:
            return _result(
                title,
                VERDICT_UNKNOWN,
                "premiere_date_missing",
                checked=True,
                platform=show_platform,
            )
        return _premiere_result(
            title, premiere, "show_premiere", today, entry.get("platform"), show_platform
        )

    show_id = show.get("id")
    if not isinstance(show_id, int):
        return _result(
            title, VERDICT_UNKNOWN, "show_id_missing", checked=True, platform=show_platform
        )

    try:
        seasons = _api_get(base_url, f"/shows/{show_id}/seasons", {}, deadline)
    except ApiError as exc:
        return _result(title, VERDICT_UNKNOWN, str(exc), checked=False, platform=show_platform)

    season = _find_season(seasons, season_number)
    if season is None:
        # The season isn't listed yet — announced but not scheduled.
        return _result(
            title, VERDICT_UNKNOWN, "season_not_listed", checked=True, platform=show_platform
        )

    season_platform = _platform(
        season.get("webChannel"), season.get("network"), show.get("webChannel"), show.get("network")
    )
    premiere = season.get("premiereDate")
    if not isinstance(premiere, str) or not premiere:
        return _result(
            title,
            VERDICT_UNKNOWN,
            "season_premiere_missing",
            checked=True,
            platform=season_platform,
        )
    return _premiere_result(
        title, premiere, "season_premiere", today, entry.get("platform"), season_platform
    )


def verify(entries: list[dict], today: date, base_url: str, deadline: float) -> list[dict]:
    results: list[dict] = []
    for entry in entries:
        if time.monotonic() >= deadline:
            title = entry.get("title")
            results.append(
                _result(
                    title if isinstance(title, str) and title.strip() else UNTITLED,
                    VERDICT_UNKNOWN,
                    "budget_exhausted",
                    checked=False,
                )
            )
            continue
        results.append(verify_entry(entry, today, base_url, deadline))
    return results


def _unnotified(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    tracking = payload.get("tracking")
    if not isinstance(tracking, list):
        return []
    return [item for item in tracking if isinstance(item, dict) and item.get("notified") is False]


def _fail(message: str) -> int:
    """Structured payload on stdout for the skill, actionable diagnostic
    on stderr for the operator reading `task_run_logs`."""
    print(json.dumps({"error": message}))
    sys.stderr.write(f"verify-release: {message}\n")
    return 1


def _writable_version(payload: Any) -> bool:
    """Whether this writer may stamp its fields onto the record.

    The owner writes only the shape it authored: an unstamped record
    (legacy pre-v1, same shape) or one already at
    `WATCHLIST_SCHEMA_VERSION`. A record carrying any other version came
    from a writer this one does not implement — stamping v1 fields onto
    it and rewriting the marker would silently downgrade a newer record
    into a shape nothing understands. Leave it untouched; the verdicts
    still return, so a released show is still notified."""
    if not isinstance(payload, dict):
        return False
    version = payload.get("schema_version")
    if version is None:
        return True
    return (
        isinstance(version, int)
        and not isinstance(version, bool)
        and version == WATCHLIST_SCHEMA_VERSION
    )


def _sort_stamp(entry: dict) -> str:
    """Sort key for `_prioritize`: the entry's `last_checked` normalized
    to `YYYY-MM-DD`, or `""` when it does not parse as a date.

    `_stamp` only ever writes `date.isoformat()`, but a hand-edited
    record can carry anything. A non-date string like `"yesterday"`
    compares greater than every real stamp, so sorting it as-is would
    park that entry permanently behind the MAX_ENTRIES cap and it would
    never be resolved again. An unusable stamp is no stamp. Normalizing
    keeps a differently-spelled-but-real date (`"20260817"`) ordering
    against the canonical stamps by its actual date."""
    value = entry.get("last_checked")
    if not isinstance(value, str):
        return ""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return ""


def _prioritize(entries: list[dict]) -> list[dict]:
    """Least-recently-resolved first, so the MAX_ENTRIES cap rotates.

    An entry with no usable `last_checked` sorts first — never resolved
    beats resolved-a-month-ago. The sort is stable, so entries stamped on
    the same date keep their watchlist order. Without this a capped
    watchlist would re-resolve the same leading 12 every run and the
    tail would never be verified at all."""
    return sorted(entries, key=_sort_stamp)


def _stamp(entries: list[dict], results: list[dict], today: date) -> bool:
    """Write `last_checked`/`last_verdict` onto the entries the run
    actually resolved. Returns whether anything changed."""
    changed = False
    # strict: verify() emits exactly one result per entry, so a length
    # mismatch is a bug that must surface, not a silent truncation that
    # would stamp the wrong entry.
    for entry, result in zip(entries, results, strict=True):
        if not result["checked"]:
            continue
        entry["last_checked"] = today.isoformat()
        entry["last_verdict"] = result["verdict"]
        changed = True
    return changed


def _write_watchlist(path: Path, payload: Any) -> None:
    """Atomic write: PID-suffixed temp beside the destination, then
    os.replace, so a kill mid-write can never truncate watchlist.json."""
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    watchlist_path = Path(os.environ.get("CHECK_WATCHLIST_PATH", DEFAULT_WATCHLIST_PATH))
    base_url = os.environ.get("TVMAZE_API_BASE", DEFAULT_API_BASE).rstrip("/")
    now_utc = datetime.now(timezone.utc)

    try:
        text = watchlist_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _fail(
            f"cannot read {watchlist_path}: {exc} — restore the file "
            f"or fix its read permissions, then rerun the verifier"
        )
    try:
        payload = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        return _fail(
            f"{watchlist_path} is not valid JSON: {exc} — repair or "
            f"restore valid JSON at that path, then rerun the verifier"
        )

    # Migration upgrades a valid older record; it does not legitimize a
    # malformed one. A root without a `tracking` list is not a v1 record
    # in any version, and stamping it would mint a shape every reader
    # then refuses — `append-watchlist.py` included.
    if not isinstance(payload, dict) or not isinstance(payload.get("tracking"), list):
        return _fail(
            f"{watchlist_path} has no `tracking` list — that is not a watchlist record at any "
            f"version and will not be stamped; restore the list, then rerun the verifier"
        )

    entries = _prioritize(_unnotified(payload))
    skipped = max(0, len(entries) - MAX_ENTRIES)
    if skipped:
        deferred = ", ".join(str(entry.get("title")) for entry in entries[MAX_ENTRIES:])
        sys.stderr.write(
            f"verify-release: {len(entries)} unnotified entries exceed MAX_ENTRIES="
            f"{MAX_ENTRIES}; {skipped} deferred to the next run (least recently "
            f"resolved run first, so these lead it): {deferred}\n"
        )
        entries = entries[:MAX_ENTRIES]

    deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
    results = verify(entries, now_utc.date(), base_url, deadline)

    output: dict[str, Any] = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "checked_at": now_utc.isoformat(),
        "results": results,
        "stats": {
            "entries": len(results),
            "resolved": sum(1 for r in results if r["checked"]),
            "released": sum(1 for r in results if r["verdict"] == VERDICT_RELEASED),
            "unreleased": sum(1 for r in results if r["verdict"] == VERDICT_UNRELEASED),
            "unknown": sum(1 for r in results if r["verdict"] == VERDICT_UNKNOWN),
            "skipped_over_cap": skipped,
        },
    }

    if _writable_version(payload):
        # The owner migrates a legacy record on READ, not only when a
        # verdict changed: an empty or all-notified watchlist, or a run
        # where no lookup completed, would otherwise stay unstamped
        # forever and every future reader would keep guessing at its
        # shape. `legacy` is true at most once per file — the next run
        # sees the stamp and writes nothing.
        legacy = "schema_version" not in payload
        stamped = _stamp(entries, results, now_utc.date())
        if legacy or stamped:
            payload["schema_version"] = WATCHLIST_SCHEMA_VERSION
            try:
                _write_watchlist(watchlist_path, payload)
                if legacy:
                    output["migrated_to_schema_version"] = WATCHLIST_SCHEMA_VERSION
            except OSError as exc:
                # The verdicts are still good and a released show must
                # be notified, so this is a warning rather than a
                # failure — visible on stderr and on the payload the
                # skill reads.
                message = f"could not write {watchlist_path}: {type(exc).__name__}: {exc}"
                sys.stderr.write(f"verify-release: {message}\n")
                output["write_error"] = message
    elif results:
        found = payload.get("schema_version") if isinstance(payload, dict) else "<non-object root>"
        message = (
            f"watchlist schema_version {found!r} is not "
            f"{WATCHLIST_SCHEMA_VERSION}; leaving {watchlist_path} untouched"
        )
        sys.stderr.write(f"verify-release: {message}\n")
        output["write_skipped"] = message

    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
