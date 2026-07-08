#!/usr/bin/env python3
"""
Fetch Trakt.tv watch history. Outputs JSON to stdout.

Required credentials (environment):
  TRAKT_CLIENT_ID, TRAKT_ACCESS_TOKEN

Optional credentials for autorefresh (environment):
  TRAKT_CLIENT_SECRET, TRAKT_REFRESH_TOKEN, TRAKT_ENV_PATH

When the access token returns HTTP 401 and all three optional
credentials are present, the script POSTs to /oauth/token with
grant_type=refresh_token, persists the new tokens to TRAKT_ENV_PATH
(in-place rewrite to keep the inode stable for docker bind mounts),
and retries the original request once. If refresh fails (e.g. the
refresh token is also expired), the original 401 is surfaced —
operator runs the device-code flow manually via
`scripts/trakt-auth.py` to recover.

Returns:
  {
    "schema_version": 1,
    "shows": [{"title", "year", "trakt_id", "slug", "episodes_watched", "last_watched", "rating"}],
    "movies": [{"title", "year", "trakt_id", "slug", "last_watched", "rating"}],
    "stats": {"total_shows", "total_movies", "rated"},
    "fetched_at": "ISO timestamp"
  }

The record shape is the versioned stateful-artifact contract documented
in skills/trakt-watch-history/state-schema.md (owner: this skill).

Per-item `rating` is sourced from the Trakt ratings endpoints and
attached by slug. No top-level ratings map is emitted; callers that
need a slug -> rating lookup should build one from the items.

On API/network/credentials failure, emits {"error": "..."} to stdout
and exits 1 so the caller (fetch_trakt_history MCP, recommend-shows
Step 1) gets structured output instead of a stack trace.
"""

import json
import os
import socket
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

# Browser-shaped User-Agent shared across data fetches and the
# /oauth/token refresh-grant. See _build_headers for the Cloudflare
# rationale; centralizing the literal keeps the data fetch and the
# refresh request indistinguishable from one user-agent fingerprint
# perspective.
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


def _build_headers(client_id: str, access_token: str) -> dict:
    # Cloudflare in front of api.trakt.tv flags short custom UAs
    # (`NanoClaw/1.0` was being intermittently blocked after a few
    # quiet days, and the block window outlived the access-token
    # grace such that subsequent refreshes failed too). A Chrome
    # desktop UA passes Cloudflare's UA heuristic without other
    # changes (TLS fingerprint, cookies). The Chrome major version
    # here is not load-bearing for Cloudflare beyond "looks like a
    # real recent browser"; bump it occasionally if Cloudflare
    # tightens.
    return {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": client_id,
        "Authorization": f"Bearer {access_token}",
        "User-Agent": BROWSER_UA,
    }


def _persist_tokens_to_env(env_path: str, access_token: str, refresh_token: str) -> None:
    """Rewrite .env in place, preserving the inode so a docker
    bind-mount of this file continues to see the new content.
    Replaces existing TRAKT_ACCESS_TOKEN / TRAKT_REFRESH_TOKEN
    lines if present; appends both if not. All other lines
    preserved verbatim.

    NB: temp-file + rename would swap the inode and break the
    bind mount — bind mounts attach to the inode, not the path,
    and the renamed file ends up unmounted from the container.
    `open("w")` truncates the existing file in place, keeping
    the inode."""
    try:
        with open(env_path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []

    # Skip-later-duplicates: stacked TRAKT_*_TOKEN= lines exist in
    # the wild (the older `trakt-auth.py` flow appended on every run
    # rather than replacing). Rewriting every match would leave the
    # duplicates with the new value, defeating the "exactly one of
    # each key" invariant. First match wins; subsequent matches are
    # dropped entirely.
    seen_access = False
    seen_refresh = False
    out_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("TRAKT_ACCESS_TOKEN="):
            if not seen_access:
                out_lines.append(f"TRAKT_ACCESS_TOKEN={access_token}\n")
                seen_access = True
            # else: drop the duplicate
        elif stripped.startswith("TRAKT_REFRESH_TOKEN="):
            if not seen_refresh:
                out_lines.append(f"TRAKT_REFRESH_TOKEN={refresh_token}\n")
                seen_refresh = True
            # else: drop the duplicate
        else:
            out_lines.append(line)

    # Normalize the last preserved line to end with a newline before
    # appending new keys. Without this, an .env that lacks a final
    # newline (`EXISTING_VAR=val` with no `\n`) would concatenate as
    # `EXISTING_VAR=valTRAKT_ACCESS_TOKEN=...`, corrupting the env
    # file and breaking the next read.
    if out_lines and not out_lines[-1].endswith("\n"):
        out_lines[-1] += "\n"

    if not seen_access:
        out_lines.append(f"TRAKT_ACCESS_TOKEN={access_token}\n")
    if not seen_refresh:
        out_lines.append(f"TRAKT_REFRESH_TOKEN={refresh_token}\n")

    with open(env_path, "w") as f:
        f.writelines(out_lines)


def _refresh_tokens(client_id: str, client_secret: str, refresh_token: str) -> tuple[str, str]:
    """POST /oauth/token with grant_type=refresh_token. Returns
    (new_access_token, new_refresh_token). Raises urllib.error.HTTPError
    on a 4xx (refresh token is also dead — operator needs the
    device-code flow) and URLError on a network/Cloudflare failure;
    caller decides whether to swallow either."""
    req = urllib.request.Request(
        "https://api.trakt.tv/oauth/token",
        data=json.dumps(
            {
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
                "grant_type": "refresh_token",
            }
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": client_id,
            "User-Agent": BROWSER_UA,
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        payload = json.loads(resp.read())
    return payload["access_token"], payload["refresh_token"]


def _try_refresh_and_retry(headers: dict) -> bool:
    """Best-effort token refresh. Returns True iff tokens were
    refreshed AND `headers["Authorization"]` was updated in place
    (caller should retry the original request). Returns False if
    refresh isn't configured (missing client-secret/refresh-token/
    env-path) or the refresh request itself failed."""
    client_id = os.environ.get("TRAKT_CLIENT_ID", "")
    client_secret = os.environ.get("TRAKT_CLIENT_SECRET", "")
    refresh_token = os.environ.get("TRAKT_REFRESH_TOKEN", "")
    env_path = os.environ.get("TRAKT_ENV_PATH", "")

    # All four env vars must be present. Without env_path the new
    # tokens couldn't be persisted, and refreshing without persistence
    # would burn the refresh token (Trakt rotates it per grant) and
    # leave the next invocation with the SAME expired access token
    # plus a now-invalid refresh token — strictly worse than not
    # refreshing at all. Surface the original 401 instead so the
    # operator runs `scripts/trakt-auth.py` for a clean device-code
    # re-auth.
    if not (client_id and client_secret and refresh_token and env_path):
        return False

    try:
        new_access, new_refresh = _refresh_tokens(client_id, client_secret, refresh_token)
    # Narrow per `jbaruch/coding-policy: error-handling`. urlopen
    # raises HTTPError (refresh token dead → device-code re-auth
    # required) and URLError (network / Cloudflare); both are
    # expected. JSONDecodeError + KeyError + TypeError catch the
    # 200-but-malformed shape (proxy injected an HTML interstitial,
    # Trakt API change). Programmer errors still propagate.
    #
    # Surface an actionable stderr diagnostic before falling back to
    # the original 401 — operators with refresh creds configured
    # otherwise have no signal whether refresh was attempted, why it
    # failed, or what to do next. Never logs token VALUES; only the
    # failure kind + recovery action.
    except urllib.error.HTTPError as e:
        try:
            sys.stderr.write(
                f"trakt-watch-history: refresh-grant returned HTTP {e.code} "
                f"({e.reason}). The refresh token is likely expired or revoked "
                f"— recovery: `docker exec -it nanoclaw python3 "
                f"/app/scripts/trakt-auth.py` for a fresh device-code re-auth. "
                f"Falling back to original 401.\n"
            )
        except OSError:
            pass
        return False
    except urllib.error.URLError as e:
        try:
            sys.stderr.write(
                f"trakt-watch-history: refresh-grant network failure "
                f"({type(e).__name__}: {e.reason}). Cloudflare block or "
                f"transient network — recovery: wait a few minutes and the "
                f"next fetch will retry. Falling back to original 401.\n"
            )
        except OSError:
            pass
        return False
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        # 200-response-but-malformed: response body wasn't JSON, or
        # was JSON but missing access_token / refresh_token. Likely
        # causes: intermediate proxy injected an HTML page, Trakt
        # API contract change, partial body read. Treat as a refresh
        # failure and surface the original 401 — file a follow-up
        # if this fires in practice.
        try:
            sys.stderr.write(
                f"trakt-watch-history: refresh-grant returned a 200 with a "
                f"malformed payload ({type(e).__name__}: {e}). Likely an "
                f"intermediate-proxy interstitial or a Trakt API contract "
                f"change — recovery: inspect the runtime log + Trakt status "
                f"page; if persistent, re-auth via `docker exec -it nanoclaw "
                f"python3 /app/scripts/trakt-auth.py`. Falling back to "
                f"original 401.\n"
            )
        except OSError:
            pass
        return False

    headers["Authorization"] = f"Bearer {new_access}"

    # env_path presence is guaranteed by the gate above; persistence
    # is still wrapped in try/except OSError because filesystem
    # failures at write time can still happen (disk full, permission
    # errors). The new token is already in `headers` and will satisfy
    # this in-flight request; the only consequence of a persist
    # failure is that the next IPC invocation would burn another
    # refresh-grant. Log to stderr so the regression is visible.
    try:
        _persist_tokens_to_env(env_path, new_access, new_refresh)
    except OSError as persist_exc:
        try:
            sys.stderr.write(
                f"trakt-watch-history: token refresh succeeded but "
                f"could not persist to {env_path}: "
                f"{type(persist_exc).__name__}: {persist_exc}\n"
            )
        except OSError:
            pass

    return True


def api_get(path: str, headers: dict, _retry: bool = True):
    req = urllib.request.Request(
        f"https://api.trakt.tv{path}",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 401 and _retry and _try_refresh_and_retry(headers):
            # Refresh succeeded; `headers["Authorization"]` has been
            # updated in place. Single retry — _retry=False makes
            # the next 401 propagate (refresh token also dead).
            return api_get(path, headers, _retry=False)
        # Bound the error-body read so a multi-MB HTML/JSON error page
        # from Trakt (or an intermediate proxy) doesn't force us to
        # pull the whole thing into memory just to throw most of it
        # away. `e.read(n)` reads at most n bytes off the stream.
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
            # (Trakt sending no content, socket closed mid-read) become
            # visible instead of this branch being a silent black hole.
            #
            # Wrap the stderr.write itself: BrokenPipeError / OSError
            # from a closed stderr must not prevent fail() below from
            # running — otherwise a traceback leaks past the script's
            # `{ "error": "..." }` JSON on stdout contract.
            try:
                sys.stderr.write(
                    f"trakt-watch-history: {path} HTTP {e.code} body preview "
                    f"read failed ({type(read_exc).__name__}: {read_exc})\n"
                )
            except OSError:
                pass
        fail(f"Trakt API {path} returned HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        # `urlopen(..., timeout=...)` raises URLError with `reason` set
        # to a `socket.timeout` on timeout — NOT a bare `socket.timeout`
        # exception. A separate `except socket.timeout` below would
        # therefore never fire. Check `.reason` inline so the operator
        # gets "timed out after Ns" instead of a generic "network
        # error: The read operation timed out."
        if isinstance(e.reason, socket.timeout):
            fail(f"Trakt API {path} timed out after {TIMEOUT_SECONDS}s")
        fail(f"Trakt API {path} network error: {e.reason}")
    except socket.timeout:
        # Defensive fallback: kept in case the stdlib ever reverts to
        # raising bare `socket.timeout`. If this branch ever fires on
        # current Python, the wrapping-in-URLError invariant has
        # changed — the message makes it visible so we notice.
        fail(f"Trakt API {path} timed out after {TIMEOUT_SECONDS}s (bare socket.timeout)")

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


def main():
    client_id = os.environ.get("TRAKT_CLIENT_ID", "")
    access_token = os.environ.get("TRAKT_ACCESS_TOKEN", "")

    if not client_id or not access_token:
        print(json.dumps({"error": "TRAKT_CLIENT_ID and TRAKT_ACCESS_TOKEN required"}))
        sys.exit(1)

    headers = _build_headers(client_id, access_token)

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

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
