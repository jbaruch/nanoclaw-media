#!/usr/bin/env python3
"""Append tracked shows to watchlist.json for `tessl__recommend-shows`.

Step 9 used to hand the agent the merge itself: read the file, check for
duplicates, add entries, write it back — plus the `schema_version`
branching that came with the state contract. Deterministic work with
known inputs and outputs belongs in a script (`coding-policy:
script-delegation`), so the agent now supplies the candidate shows and
this script owns the merge.

`recommend-shows` is a NON-OWNER writer of watchlist.json (owner:
`check-watchlist`, contract in skills/check-watchlist/state-schema.md),
which sets the version rules this script enforces:
  - No file yet -> create it stamped `WATCHLIST_SCHEMA_VERSION`. A
    record this script authors is its own, not a migration.
  - Record already at `WATCHLIST_SCHEMA_VERSION` -> append, preserving
    the stamp.
  - Unstamped (legacy pre-v1) or any other version -> read-only. Append
    nothing, write nothing, exit non-zero naming the version found.
    Migrating is the owner's job; the nightly `check-watchlist` run
    stamps a legacy record when it reads one, and the next run of this
    skill lands the shows.

Input
-----
A JSON array of candidate shows on stdin:
  [{"title", "platform", "expected", "reason", "added"}, ...]
`title` is required. `notified` is set to false by this script and is
rejected in the input — delivery state is the owner's to write.
`expected` accepts the fuzzy formats the precheck parses (`YYYY-MM-DD`,
`YYYY-Qn`, `YYYY-MM`, `YYYY`); anything else is refused, since an
unparseable window silently becomes a nightly wake.

Duplicates are detected on the title after casefolding and whitespace
collapse — the same rule the other watchlist scripts use — and skipped
rather than added twice.

Environment
-----------
  CHECK_WATCHLIST_PATH — watchlist.json path (default
    /workspace/group/watchlist.json), shared with the owner's scripts.

Output
------
Single-line JSON on stdout.
  exit 0: {"added": ["<title>", ...], "skipped_duplicates": [...],
           "created": <bool>, "tracking_count": N, "schema_version": 1}
  exit 1: {"error": "<what failed and what to do about it>"}
An empty input array is a valid no-op, not an error.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

DEFAULT_WATCHLIST_PATH = "/workspace/group/watchlist.json"

# The one record version this skill knows how to append to, matching
# the owner's WATCHLIST_SCHEMA_VERSION.
WATCHLIST_SCHEMA_VERSION = 1

# Fields carried onto a new entry, in this order.
ENTRY_FIELDS = ("title", "platform", "expected", "reason", "added")

_WHITESPACE_RE = re.compile(r"\s+")
# The fuzzy `expected` formats check-watchlist-precheck.py anchors to a
# release window. A value outside these parses as nothing and turns the
# entry into a nightly wake, so it is refused at the door.
_EXPECTED_RE = re.compile(r"\d{4}(-(\d{2}-\d{2}|[Qq][1-4]|0[1-9]|1[0-2]))?")


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip().casefold()


def _fail(message: str) -> int:
    print(json.dumps({"error": message}))
    return 1


def _valid_expected(value: object) -> bool:
    if not isinstance(value, str) or not _EXPECTED_RE.fullmatch(value.strip()):
        return False
    if len(value.strip()) == 10:
        # A full ISO date must be a real one — 2026-13-40 matches the
        # shape and anchors to nothing.
        try:
            date.fromisoformat(value.strip())
        except ValueError:
            return False
    return True


def _clean_candidates(raw: Any) -> tuple[list[dict], str | None]:
    if not isinstance(raw, list):
        return [], "stdin must carry a JSON array of show objects"
    candidates: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], f"entry {index} is not a JSON object"
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            return [], f"entry {index} has no usable `title`"
        if "notified" in item:
            return [], (
                f"entry {index} carries `notified` — delivery state belongs to check-watchlist; "
                f"drop the field and rerun"
            )
        expected = item.get("expected")
        if expected is not None and not _valid_expected(expected):
            return [], (
                f"entry {index} has `expected` {expected!r}, which the release precheck cannot "
                f"anchor — use YYYY-MM-DD, YYYY-Qn, YYYY-MM, or YYYY"
            )
        entry = {field: item[field] for field in ENTRY_FIELDS if field in item}
        entry["title"] = title.strip()
        entry["notified"] = False
        candidates.append(entry)
    return candidates, None


def _load(path: Path) -> tuple[dict | None, bool, str | None]:
    """(payload, created, error). `created` is True when the file is
    absent and this run authors the record."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"schema_version": WATCHLIST_SCHEMA_VERSION, "tracking": []}, True, None
    except (OSError, UnicodeDecodeError) as exc:
        return (
            None,
            False,
            (
                f"cannot read {path}: {exc} — restore the file or fix its read "
                f"permissions, then rerun"
            ),
        )
    if not text.strip():
        return {"schema_version": WATCHLIST_SCHEMA_VERSION, "tracking": []}, True, None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return (
            None,
            False,
            (
                f"{path} is not valid JSON: {exc} — repair or restore valid JSON at that path, "
                f"then rerun"
            ),
        )
    if not isinstance(payload, dict):
        return None, False, f"{path} root is not a JSON object — restore a valid watchlist record"
    if payload.get("schema_version") != WATCHLIST_SCHEMA_VERSION:
        return (
            None,
            False,
            (
                f"{path} is at schema_version {payload.get('schema_version')!r}, not "
                f"{WATCHLIST_SCHEMA_VERSION} — this skill is a non-owner writer and does "
                f"not migrate; "
                f"the next check-watchlist run stamps a legacy record, so rerun after it"
            ),
        )
    if not isinstance(payload.get("tracking"), list):
        payload["tracking"] = []
    return payload, False, None


def _write(path: Path, payload: dict) -> str | None:
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError as exc:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        return (
            f"could not write {path}: {type(exc).__name__}: {exc} — no show was added; "
            f"fix the destination's permissions or free space, then rerun"
        )
    return None


def main() -> int:
    path = Path(os.environ.get("CHECK_WATCHLIST_PATH", DEFAULT_WATCHLIST_PATH))
    try:
        raw = json.loads(sys.stdin.read() or "[]")
    except json.JSONDecodeError as exc:
        return _fail(f"stdin is not valid JSON: {exc} — pass a JSON array of show objects")

    candidates, candidate_error = _clean_candidates(raw)
    if candidate_error is not None:
        return _fail(candidate_error)

    payload, created, load_error = _load(path)
    if payload is None:
        return _fail(load_error or f"cannot load {path}")

    tracking = payload["tracking"]
    seen = {
        _normalize(entry["title"])
        for entry in tracking
        if isinstance(entry, dict) and isinstance(entry.get("title"), str)
    }
    added: list[str] = []
    skipped: list[str] = []
    for candidate in candidates:
        key = _normalize(candidate["title"])
        if key in seen:
            skipped.append(candidate["title"])
            continue
        seen.add(key)
        tracking.append(candidate)
        added.append(candidate["title"])

    if added or created:
        write_error = _write(path, payload)
        if write_error is not None:
            return _fail(write_error)

    print(
        json.dumps(
            {
                "added": added,
                "skipped_duplicates": skipped,
                "created": created,
                "tracking_count": len(tracking),
                "schema_version": WATCHLIST_SCHEMA_VERSION,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
