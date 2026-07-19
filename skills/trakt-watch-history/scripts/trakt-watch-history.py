#!/usr/bin/env python3
"""
Fetch Trakt.tv watch history through the OneCLI gateway proxy.

Runs in-container. Trakt requests are routed through the container's
gateway proxy; the gateway owns ALL Trakt credentials and injects them
on the wire. The caller sends only placeholders the gateway overwrites:
  - trakt-api-key: onecli-managed  (placeholder; the gateway injects the
    real client id via a header-injection secret)
  - trakt-api-version: 2
  - Authorization: Bearer onecli-managed  (placeholder; the gateway
    swaps it for the real OAuth token via the custom-oauth connection)

The container holds NO Trakt config — no client id, no tokens.

Optional environment:
  TRAKT_HISTORY_OUT — destination path for the written record
    (default /workspace/group/trakt-history.json)

On success, writes the record atomically to TRAKT_HISTORY_OUT and also
prints it to stdout. Output shape:
  {
    "schema_version": 1,
    "shows": [{"title", "year", "trakt_id", "slug", "episodes_watched", "last_watched", "rating"}],
    "movies": [{"title", "year", "trakt_id", "slug", "last_watched", "rating"}],
    "stats": {"total_shows", "total_movies", "rated"},
    "fetched_at": "ISO 8601 UTC timestamp"
  }

The record shape is the versioned stateful-artifact contract documented
in skills/trakt-watch-history/state-schema.md (owner: this skill).

Per-item `rating` is sourced from the Trakt ratings endpoints and
attached by slug. No top-level ratings map is emitted; callers that
need a slug -> rating lookup should build one from the items.

On API/network/credentials failure, emits {"error": "..."} to stdout
and exits 1 (leaving any existing record untouched) so the caller
(trakt-watch-history / entertainment-sync Step 1) gets structured
output instead of a stack trace.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import NoReturn

TIMEOUT_SECONDS = 30
ERROR_PREVIEW_BYTES = 200
# Version of the emitted trakt-history.json record shape, per
# `jbaruch/coding-policy: stateful-artifacts`. Bump on any shape change;
# the contract lives in skills/trakt-watch-history/state-schema.md.
SCHEMA_VERSION = 1

# Default destination for the persisted record. In-container this is the
# shared owner-data mount; tests point TRAKT_HISTORY_OUT elsewhere.
DEFAULT_OUTPUT_PATH = "/workspace/group/trakt-history.json"

# Single placeholder value the gateway swaps for the real Trakt
# credentials on BOTH headers. The gateway matches on api.trakt.tv and
# overwrites each header (SetHeader semantics), keying on host +
# header-name, not the incoming value — so one placeholder serves both:
# the custom-oauth connection injects the OAuth Bearer, and a
# header-injection secret injects the real client id as `trakt-api-key`.
# Sending placeholders (rather than omitting the headers) keeps the
# request shape identical to a normal Trakt call and keeps every Trakt
# credential out of the container.
ONECLI_MANAGED_PLACEHOLDER = "onecli-managed"

# Browser-shaped User-Agent. Cloudflare in front of api.trakt.tv flags
# short custom UAs (`NanoClaw/1.0` was intermittently blocked). A Chrome
# desktop UA passes Cloudflare's UA heuristic. The Chrome major version
# is not load-bearing beyond "looks like a real recent browser"; bump it
# occasionally if Cloudflare tightens.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)


def fail(msg) -> NoReturn:
    # `NoReturn` annotation lets type checkers see that every non-
    # success path through `api_get` terminates, so downstream
    # iteration (`for entry in api_get(...)`) isn't flagged as
    # "Object of type None cannot be iterated." Runtime behavior
    # unchanged — sys.exit raises SystemExit either way.
    print(json.dumps({"error": msg}))
    sys.exit(1)


def _build_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": ONECLI_MANAGED_PLACEHOLDER,
        "Authorization": f"Bearer {ONECLI_MANAGED_PLACEHOLDER}",
        "User-Agent": BROWSER_UA,
    }


def api_get(path: str, headers: dict):
    req = urllib.request.Request(
        f"https://api.trakt.tv{path}",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        # Bound the error-body read so a multi-MB HTML/JSON error page
        # from Trakt (or the gateway) doesn't force us to pull the whole
        # thing into memory just to throw most of it away. `e.read(n)`
        # reads at most n bytes off the stream.
        body = ""
        try:
            body = e.read(ERROR_PREVIEW_BYTES).decode("utf-8", errors="replace")
        # Narrow per `jbaruch/coding-policy: error-handling`. `e.read`
        # raises OSError on socket issues; `.decode(errors="replace")`
        # cannot raise. Programmer errors propagate.
        except OSError as read_exc:
            # Best-effort enrichment: the outer fail() still fires with
            # the HTTP status code, so losing the body preview is not
            # catastrophic. Log to stderr so repeated body-read failures
            # become visible instead of this branch being a silent black
            # hole. Wrap the stderr.write itself: a closed stderr must
            # not prevent fail() below from running — otherwise a
            # traceback leaks past the `{ "error": "..." }` contract.
            try:
                sys.stderr.write(
                    f"trakt-watch-history: {path} HTTP {e.code} body preview "
                    f"read failed ({type(read_exc).__name__}: {read_exc})\n"
                )
            except OSError:
                pass
        # 401/403 from the gateway path means the gateway could not
        # inject a valid token — the Trakt connection is missing or its
        # gateway-side refresh is failing. Point the operator at the
        # gateway, not a local re-auth (the container holds no tokens).
        if e.code in (401, 403):
            fail(
                f"Trakt API {path} returned HTTP {e.code}: {body} — the OneCLI "
                f"gateway could not inject a Trakt token. Reconnect the Trakt "
                f"custom-oauth connection on the gateway and retry."
            )
        fail(f"Trakt API {path} returned HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        # `urlopen(..., timeout=...)` raises URLError with `reason` set
        # to a `TimeoutError` on timeout — NOT a bare `TimeoutError`
        # exception. A separate `except TimeoutError` below would
        # therefore never fire. Check `.reason` inline so the operator
        # gets "timed out after Ns" instead of a generic "network
        # error: The read operation timed out."
        if isinstance(e.reason, TimeoutError):
            fail(f"Trakt API {path} timed out after {TIMEOUT_SECONDS}s")
        fail(f"Trakt API {path} network error: {e.reason}")
    except TimeoutError:
        # Defensive fallback: kept in case the stdlib ever reverts to
        # raising a bare `TimeoutError` (formerly the `socket.timeout`
        # alias). If this branch ever fires on current Python, the
        # wrapping-in-URLError invariant has changed — the message
        # makes it visible so we notice.
        fail(f"Trakt API {path} timed out after {TIMEOUT_SECONDS}s (bare TimeoutError)")

    # Parse outside the urlopen try/except so JSONDecodeError has
    # access to the raw response bytes for a preview in the fail
    # message. Parsing inside the `with` would lose `raw` by the time
    # this handler runs. Decode with `errors="replace"` before json
    # parsing so invalid UTF-8 bytes in the response (seen with
    # misconfigured intermediate proxies, unusual character encodings)
    # don't raise UnicodeDecodeError and bypass `fail()` — every
    # failure path here must still emit the `{ "error": "..." }`
    # contract on stdout.
    decoded = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(decoded)
    except json.JSONDecodeError as e:
        # Build the preview from raw bytes, not the decoded string, so
        # `ERROR_PREVIEW_BYTES` is a consistent byte bound across both
        # this path and the HTTPError path above. Slicing the decoded
        # string would be char-bounded, which — on a multi-byte UTF-8
        # response — could let a preview exceed the intended byte cap.
        preview = raw[:ERROR_PREVIEW_BYTES].decode("utf-8", errors="replace")
        fail(f"Trakt API {path} returned non-JSON response: {e}; preview={preview!r}")


def _write_record(out_path: str, result: dict) -> None:
    """Atomic-write the record: write a per-process temp file, then
    os.replace onto the destination (atomic on the same filesystem)
    so a crash mid-write never leaves a truncated trakt-history.json.
    The temp name carries the PID so two concurrent invocations (e.g.
    entertainment-sync and a manual recommend-shows run) don't clobber
    each other's in-flight temp before the replace. The destination
    directory must already exist (the /workspace/group mount
    in-container); a missing directory surfaces via fail()."""
    tmp_path = f"{out_path}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(result, f, indent=2)
        os.replace(tmp_path, out_path)
    except OSError as e:
        # Best-effort cleanup so a failed write doesn't leave the temp
        # behind. The unlink is itself wrapped: if the temp was never
        # created (open failed) or is already gone, that must not mask
        # the original write failure surfaced by fail() below.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        fail(
            f"Fetched Trakt history but could not write {out_path}: "
            f"{type(e).__name__}: {e} — check the destination directory "
            f"exists and is writable."
        )


def main():
    out_path = os.environ.get("TRAKT_HISTORY_OUT", DEFAULT_OUTPUT_PATH)
    headers = _build_headers()

    # Watched shows (with play counts)
    watched_shows = api_get("/users/me/watched/shows", headers)
    shows = []
    for entry in watched_shows:
        show = entry.get("show", {})
        eps = sum(len(season.get("episodes", [])) for season in entry.get("seasons", []))
        shows.append(
            {
                "title": show.get("title"),
                "year": show.get("year"),
                "trakt_id": show.get("ids", {}).get("trakt"),
                "slug": show.get("ids", {}).get("slug"),
                "episodes_watched": eps,
                "last_watched": entry.get("last_watched_at"),
            }
        )

    # Watched movies
    watched_movies = api_get("/users/me/watched/movies", headers)
    movies = []
    for entry in watched_movies:
        movie = entry.get("movie", {})
        movies.append(
            {
                "title": movie.get("title"),
                "year": movie.get("year"),
                "trakt_id": movie.get("ids", {}).get("trakt"),
                "slug": movie.get("ids", {}).get("slug"),
                "last_watched": entry.get("last_watched_at"),
            }
        )

    # Ratings (shows and movies)
    ratings = {}
    for item in api_get("/users/me/ratings/shows", headers):
        slug = item.get("show", {}).get("ids", {}).get("slug")
        if slug:
            ratings[slug] = item.get("rating")
    for item in api_get("/users/me/ratings/movies", headers):
        slug = item.get("movie", {}).get("ids", {}).get("slug")
        if slug:
            ratings[slug] = item.get("rating")

    # Attach ratings to shows/movies
    for s in shows:
        s["rating"] = ratings.get(s["slug"])
    for m in movies:
        m["rating"] = ratings.get(m["slug"])

    # Sort by last watched (most recent first)
    shows.sort(key=lambda x: x.get("last_watched") or "", reverse=True)
    movies.sort(key=lambda x: x.get("last_watched") or "", reverse=True)

    result = {
        "schema_version": SCHEMA_VERSION,
        "shows": shows,
        "movies": movies,
        "stats": {
            "total_shows": len(shows),
            "total_movies": len(movies),
            "rated": len(ratings),
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    _write_record(out_path, result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
