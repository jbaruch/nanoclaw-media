"""Baseline tests for skills/trakt-watch-history/scripts/trakt-watch-history.py.

Locks down the documented contract per `coding-policy: testing-standards`:

  - Reads `TRAKT_CLIENT_ID` + `TRAKT_ACCESS_TOKEN` from the environment
    at `main()` time; either missing → `{"error": "..."}` to stdout +
    exit 1
  - Issues four `api_get` calls in sequence: watched/shows,
    watched/movies, ratings/shows, ratings/movies
  - Each call: `Request` to `https://api.trakt.tv<path>` carrying the
    five Trakt headers built from the env credentials
  - Episodes-watched per show summed across `seasons[*].episodes`
  - Ratings attached by `slug` to both shows and movies; missing slug
    in ratings → `rating: None`
  - Output is sorted by `last_watched` descending
  - Output payload: `{shows, movies, stats: {total_shows,
    total_movies, rated}, fetched_at}`
  - Failure modes (all hit the `fail()` → JSON error + exit 1
    contract):
      * `urllib.error.HTTPError` → `Trakt API <path> returned HTTP
        <code>: <preview>` (preview bounded to `ERROR_PREVIEW_BYTES`)
      * `urllib.error.URLError` with `TimeoutError` reason →
        `timed out after <N>s`
      * `urllib.error.URLError` other → `network error: <reason>`
      * bare `TimeoutError` (defensive fallback) → `timed out after
        <N>s (bare TimeoutError)`
      * `JSONDecodeError` on a non-JSON response → preview from raw
        bytes (consistent byte-bound across paths)

Tests freeze `module.datetime` (now() returning a fixed UTC instant)
so `fetched_at` is deterministic, and patch
`urllib.request.urlopen` to drive each API path / failure branch
without real network I/O.
"""

import io
import json
import urllib.error
from datetime import datetime, timezone
from email.message import Message

import pytest

_FROZEN_NOW = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)


def _make_frozen_datetime(real_datetime):
    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return _FROZEN_NOW.replace(tzinfo=None)
            return _FROZEN_NOW.astimezone(tz)

    return FrozenDateTime


class _FakeResponse:
    def __init__(self, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def read(self):
        return self._body


def _patch_urlopen(monkeypatch, payloads):
    """Patch `urllib.request.urlopen` to dispatch per URL substring.

    `payloads` is a dict {url_substring: payload}. payload is a Python
    object (json-serialized), bytes/str (returned raw), or an Exception
    instance (raised on call)."""

    def _fake_urlopen(req, timeout=None):
        target = req.full_url if hasattr(req, "full_url") else str(req)
        for needle, payload in payloads.items():
            if needle in target:
                if isinstance(payload, Exception):
                    raise payload
                if isinstance(payload, (bytes, str)):
                    return _FakeResponse(payload)
                return _FakeResponse(json.dumps(payload))
        raise AssertionError(f"unexpected URL fetched: {target!r}")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)


def _shows_payload(*shows):
    """Wrap show dicts in the watched/shows envelope shape."""
    return [{"show": s, "seasons": s.pop("_seasons", [])} for s in shows]


def _movies_payload(*movies):
    return [{"movie": m, "last_watched_at": m.pop("_last_watched", None)} for m in movies]


def _all_endpoints(*, shows=None, movies=None, show_ratings=None, movie_ratings=None):
    """Convenience: build a 4-endpoint payload dict default-empty per
    endpoint, so a single-branch test only specifies what it cares
    about."""
    return {
        "/users/me/watched/shows": shows if shows is not None else [],
        "/users/me/watched/movies": movies if movies is not None else [],
        "/users/me/ratings/shows": show_ratings if show_ratings is not None else [],
        "/users/me/ratings/movies": movie_ratings if movie_ratings is not None else [],
    }


def _run(module, monkeypatch, capsys, *, env=None):
    monkeypatch.setattr("sys.argv", ["trakt-watch-history.py"])
    monkeypatch.setattr(module, "datetime", _make_frozen_datetime(datetime))
    if env is None:
        env = {"TRAKT_CLIENT_ID": "cid", "TRAKT_ACCESS_TOKEN": "tok"}
    for k in ("TRAKT_CLIENT_ID", "TRAKT_ACCESS_TOKEN"):
        if k in env:
            monkeypatch.setenv(k, env[k])
        else:
            monkeypatch.delenv(k, raising=False)
    code = 0
    try:
        result = module.main()
        code = 0 if result is None else int(result)
    except SystemExit as exc:
        code = 0 if exc.code is None else int(exc.code)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# Credential gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"TRAKT_CLIENT_ID": "cid"},
        {"TRAKT_ACCESS_TOKEN": "tok"},
        {"TRAKT_CLIENT_ID": "", "TRAKT_ACCESS_TOKEN": "tok"},
        {"TRAKT_CLIENT_ID": "cid", "TRAKT_ACCESS_TOKEN": ""},
    ],
)
def test_missing_credentials_exits_1_with_error_json(trakt_watch_history, monkeypatch, capsys, env):
    """Either env var missing OR empty → `{"error": "..."}` on stdout
    + exit 1, before any API call."""
    code, out, _ = _run(trakt_watch_history, monkeypatch, capsys, env=env)
    assert code == 1
    payload = json.loads(out)
    assert "TRAKT_CLIENT_ID" in payload["error"]
    assert "TRAKT_ACCESS_TOKEN" in payload["error"]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_full_payload(trakt_watch_history, monkeypatch, capsys):
    """Both shows + movies + ratings populated → emitted payload sorted
    by `last_watched` desc, episodes summed across seasons, ratings
    attached by slug, stats consistent."""
    shows_endpoint = [
        {
            "show": {
                "title": "Severance",
                "year": 2022,
                "ids": {"trakt": 100, "slug": "severance"},
            },
            "last_watched_at": "2026-04-25T12:00:00Z",
            "seasons": [
                {"episodes": [{"number": 1}, {"number": 2}, {"number": 3}]},
                {"episodes": [{"number": 1}, {"number": 2}]},
            ],
        },
        {
            "show": {
                "title": "Andor",
                "year": 2022,
                "ids": {"trakt": 101, "slug": "andor"},
            },
            "last_watched_at": "2026-04-29T20:00:00Z",
            "seasons": [{"episodes": [{"number": 1}]}],
        },
    ]
    movies_endpoint = [
        {
            "movie": {
                "title": "Dune Part Two",
                "year": 2024,
                "ids": {"trakt": 200, "slug": "dune-part-two"},
            },
            "last_watched_at": "2026-04-20T22:00:00Z",
        },
    ]
    show_ratings = [
        {"show": {"ids": {"slug": "severance"}}, "rating": 9},
    ]
    movie_ratings = [
        {"movie": {"ids": {"slug": "dune-part-two"}}, "rating": 10},
    ]
    _patch_urlopen(
        monkeypatch,
        {
            "/users/me/watched/shows": shows_endpoint,
            "/users/me/watched/movies": movies_endpoint,
            "/users/me/ratings/shows": show_ratings,
            "/users/me/ratings/movies": movie_ratings,
        },
    )

    code, out, _ = _run(trakt_watch_history, monkeypatch, capsys)
    assert code == 0
    payload = json.loads(out)
    # Versioned stateful-artifact record (state-schema.md, issue #33).
    assert payload["schema_version"] == trakt_watch_history.SCHEMA_VERSION
    # Sort: most recent first.
    assert [s["title"] for s in payload["shows"]] == ["Andor", "Severance"]
    severance = next(s for s in payload["shows"] if s["slug"] == "severance")
    assert severance["episodes_watched"] == 5
    assert severance["rating"] == 9
    assert payload["movies"][0]["rating"] == 10
    assert payload["stats"] == {"total_shows": 2, "total_movies": 1, "rated": 2}
    assert payload["fetched_at"] == _FROZEN_NOW.isoformat()


def test_show_without_rating_emits_none(trakt_watch_history, monkeypatch, capsys):
    """Slug not in ratings → `rating: None` (not omitted), so the
    field is always present per the docstring."""
    _patch_urlopen(
        monkeypatch,
        _all_endpoints(
            shows=[
                {
                    "show": {
                        "title": "X",
                        "year": 2025,
                        "ids": {"trakt": 1, "slug": "x"},
                    },
                    "last_watched_at": "2026-04-01T00:00:00Z",
                    "seasons": [],
                },
            ]
        ),
    )

    _, out, _ = _run(trakt_watch_history, monkeypatch, capsys)
    payload = json.loads(out)
    assert payload["shows"][0]["rating"] is None
    assert payload["stats"]["rated"] == 0


def test_request_carries_trakt_headers(trakt_watch_history, monkeypatch, capsys):
    """Each `api_get` constructs a Request with the five documented
    Trakt headers. Capture the first request and verify the contract
    rather than just the URL."""
    captured = {}

    def _capture(req, timeout=None):
        # Stash only the FIRST request — main() makes 4 api_get calls
        # back-to-back; the headers contract is identical across them,
        # so capturing once and asserting once is enough.
        if "request" not in captured:
            captured["request"] = req
        return _FakeResponse("[]")

    monkeypatch.setattr("urllib.request.urlopen", _capture)

    _run(trakt_watch_history, monkeypatch, capsys)
    headers = captured["request"].headers
    # `urllib.request.Request` normalizes header names by title-casing
    # the first letter of each word and lower-casing the rest, so the
    # documented `trakt-api-version` arrives back as `Trakt-api-version`.
    assert headers["Trakt-api-version"] == "2"
    assert headers["Trakt-api-key"] == "cid"
    assert headers["Authorization"] == "Bearer tok"
    # Browser-shaped UA — Cloudflare in front of api.trakt.tv 403s
    # short custom UAs like the previous `NanoClaw/1.0`. Asserting on
    # the Chrome marker keeps the test robust against future Chrome
    # version bumps in the UA literal while still pinning the
    # contract (real-browser shape, not custom app shape).
    assert "Chrome/" in headers["User-agent"]
    assert headers["User-agent"].startswith("Mozilla/5.0")
    assert headers["Content-type"] == "application/json"


# ---------------------------------------------------------------------------
# Failure modes (each maps to fail() → exit 1 with JSON error)
# ---------------------------------------------------------------------------


def test_http_error_emits_status_and_bounded_preview(trakt_watch_history, monkeypatch, capsys):
    """`HTTPError` from urlopen → fail() with `Trakt API <path>
    returned HTTP <code>: <preview>`. The preview is bounded to
    `ERROR_PREVIEW_BYTES` even when the upstream body is much larger."""
    big_body = ("Z" * (trakt_watch_history.ERROR_PREVIEW_BYTES + 500)).encode("utf-8")
    error = urllib.error.HTTPError(
        url="https://api.trakt.tv/users/me/watched/shows",
        code=502,
        msg="Bad Gateway",
        hdrs=Message(),
        fp=io.BytesIO(big_body),
    )
    _patch_urlopen(monkeypatch, {"/users/me/watched/shows": error})

    code, out, _ = _run(trakt_watch_history, monkeypatch, capsys)
    assert code == 1
    payload = json.loads(out)
    assert "HTTP 502" in payload["error"]
    # Preview portion of the error message is bounded.
    assert payload["error"].count("Z") <= trakt_watch_history.ERROR_PREVIEW_BYTES + 50


def test_url_error_with_timeout_reason_message(trakt_watch_history, monkeypatch, capsys):
    """`URLError(reason=TimeoutError())` → operator-friendly "timed
    out after Ns" message."""
    err = urllib.error.URLError(reason=TimeoutError("read timed out"))
    _patch_urlopen(monkeypatch, {"/users/me/watched/shows": err})

    code, out, _ = _run(trakt_watch_history, monkeypatch, capsys)
    assert code == 1
    payload = json.loads(out)
    assert "timed out after" in payload["error"]
    assert str(trakt_watch_history.TIMEOUT_SECONDS) in payload["error"]


def test_url_error_other_reason_emits_network_error(trakt_watch_history, monkeypatch, capsys):
    """`URLError` with non-timeout reason → "network error: <reason>"
    so the operator sees the underlying cause."""
    err = urllib.error.URLError(reason="Name or service not known")
    _patch_urlopen(monkeypatch, {"/users/me/watched/shows": err})

    code, out, _ = _run(trakt_watch_history, monkeypatch, capsys)
    assert code == 1
    payload = json.loads(out)
    assert "network error: Name or service not known" in payload["error"]


def test_bare_timeout_error_defensive_fallback(trakt_watch_history, monkeypatch, capsys):
    """Defensive `except TimeoutError` keeps a stable error message
    if the stdlib ever stops wrapping in URLError. The marker `(bare
    TimeoutError)` makes that drift immediately visible in the run
    log."""
    _patch_urlopen(monkeypatch, {"/users/me/watched/shows": TimeoutError("raw")})

    code, out, _ = _run(trakt_watch_history, monkeypatch, capsys)
    assert code == 1
    payload = json.loads(out)
    assert "(bare TimeoutError)" in payload["error"]


def test_non_json_response_emits_preview(trakt_watch_history, monkeypatch, capsys):
    """Non-JSON body → JSONDecodeError → fail() with `non-JSON
    response: <error>; preview=...`. Preview is sourced from the raw
    bytes for byte-bound consistency."""
    _patch_urlopen(monkeypatch, {"/users/me/watched/shows": b"<html>not json</html>"})

    code, out, _ = _run(trakt_watch_history, monkeypatch, capsys)
    assert code == 1
    payload = json.loads(out)
    assert "non-JSON response" in payload["error"]
    assert "preview=" in payload["error"]


# ---------------------------------------------------------------------------
# Autorefresh on 401
# ---------------------------------------------------------------------------


def _run_with_refresh_env(module, monkeypatch, capsys, *, env_path):
    """Variant of _run that also sets refresh creds + TRAKT_ENV_PATH."""
    monkeypatch.setattr("sys.argv", ["trakt-watch-history.py"])
    monkeypatch.setattr(module, "datetime", _make_frozen_datetime(datetime))
    monkeypatch.setenv("TRAKT_CLIENT_ID", "cid")
    monkeypatch.setenv("TRAKT_ACCESS_TOKEN", "old-access")
    monkeypatch.setenv("TRAKT_CLIENT_SECRET", "secret")
    monkeypatch.setenv("TRAKT_REFRESH_TOKEN", "old-refresh")
    monkeypatch.setenv("TRAKT_ENV_PATH", str(env_path))
    code = 0
    try:
        result = module.main()
        code = 0 if result is None else int(result)
    except SystemExit as exc:
        code = 0 if exc.code is None else int(exc.code)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_401_triggers_refresh_grant_and_retries_with_new_token(
    trakt_watch_history, monkeypatch, capsys, tmp_path
):
    """First /users/me/watched/shows call returns 401, triggers a
    POST to /oauth/token with grant_type=refresh_token, then the
    retry succeeds with the new access token. Subsequent endpoints
    (movies, ratings) also succeed using the refreshed token in
    `headers['Authorization']` since the dict is shared across
    api_get calls in main()."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "OTHER_VAR=preserved\n"
        "TRAKT_ACCESS_TOKEN=old-access\n"
        "TRAKT_REFRESH_TOKEN=old-refresh\n"
        "ANOTHER_VAR=also-preserved\n"
    )

    state = {"shows_call_count": 0, "captured_auths": []}

    def _fake_urlopen(req, timeout=None):
        target = req.full_url
        # Capture every Authorization header we send so the test can
        # verify the retry uses the NEW token.
        auth = req.headers.get("Authorization", "")
        state["captured_auths"].append((target, auth))
        if "/oauth/token" in target:
            return _FakeResponse(
                json.dumps(
                    {
                        "access_token": "new-access",
                        "refresh_token": "new-refresh",
                        "expires_in": 7776000,
                    }
                )
            )
        if "/users/me/watched/shows" in target:
            state["shows_call_count"] += 1
            if state["shows_call_count"] == 1:
                raise urllib.error.HTTPError(
                    target, 401, "Unauthorized", hdrs=Message(), fp=io.BytesIO(b"")
                )
            return _FakeResponse("[]")
        if "/users/me/" in target:
            return _FakeResponse("[]")
        raise AssertionError(f"unexpected URL: {target!r}")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    code, out, err = _run_with_refresh_env(
        trakt_watch_history, monkeypatch, capsys, env_path=env_path
    )
    assert code == 0, f"script should succeed after refresh; stderr={err!r}"

    # First shows call sent the OLD token, then a refresh-grant POST,
    # then a retry with the NEW token in the same `headers` dict.
    assert state["shows_call_count"] == 2
    shows_auths = [a for url, a in state["captured_auths"] if "/users/me/watched/shows" in url]
    assert shows_auths == ["Bearer old-access", "Bearer new-access"], shows_auths

    # Movies + ratings endpoints called AFTER the refresh should also
    # carry the new token — the headers dict is shared.
    later_auths = [
        a
        for url, a in state["captured_auths"]
        if "/oauth/token" not in url and "/users/me/watched/shows" not in url
    ]
    assert all(a == "Bearer new-access" for a in later_auths), later_auths


def test_401_refresh_persists_new_tokens_to_env_in_place(
    trakt_watch_history, monkeypatch, capsys, tmp_path
):
    """After a successful refresh, TRAKT_ACCESS_TOKEN and
    TRAKT_REFRESH_TOKEN in .env are rewritten in place. The file's
    inode must stay stable (docker bind-mount safety); other
    lines preserved verbatim, exactly one of each TRAKT_ key."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "OTHER_VAR=preserved\n"
        "TRAKT_ACCESS_TOKEN=old-access\n"
        "TRAKT_REFRESH_TOKEN=old-refresh\n"
        "ANOTHER_VAR=also-preserved\n"
    )
    inode_before = env_path.stat().st_ino

    state = {"shows_call_count": 0}

    def _fake_urlopen(req, timeout=None):
        target = req.full_url
        if "/oauth/token" in target:
            return _FakeResponse(
                json.dumps({"access_token": "new-access", "refresh_token": "new-refresh"})
            )
        if "/users/me/watched/shows" in target:
            state["shows_call_count"] += 1
            if state["shows_call_count"] == 1:
                raise urllib.error.HTTPError(
                    target, 401, "Unauthorized", hdrs=Message(), fp=io.BytesIO(b"")
                )
            return _FakeResponse("[]")
        return _FakeResponse("[]")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    code, _, _ = _run_with_refresh_env(trakt_watch_history, monkeypatch, capsys, env_path=env_path)
    assert code == 0

    inode_after = env_path.stat().st_ino
    assert inode_after == inode_before, (
        f"inode swap would break docker bind-mounts; before={inode_before}, after={inode_after}"
    )

    new_lines = env_path.read_text().splitlines()
    assert "OTHER_VAR=preserved" in new_lines
    assert "ANOTHER_VAR=also-preserved" in new_lines
    assert "TRAKT_ACCESS_TOKEN=new-access" in new_lines
    assert "TRAKT_REFRESH_TOKEN=new-refresh" in new_lines
    # Each TRAKT key appears EXACTLY once (no stacked duplicates).
    assert sum(line.startswith("TRAKT_ACCESS_TOKEN=") for line in new_lines) == 1
    assert sum(line.startswith("TRAKT_REFRESH_TOKEN=") for line in new_lines) == 1


def test_401_with_missing_refresh_creds_surfaces_original_401(
    trakt_watch_history, monkeypatch, capsys
):
    """Without TRAKT_CLIENT_SECRET / TRAKT_REFRESH_TOKEN /
    TRAKT_ENV_PATH set, a 401 from Trakt propagates as a normal
    HTTP-error failure — no refresh attempted. Operator sees the
    actionable HTTP 401 message and runs trakt-auth.py manually."""

    def _fake_urlopen(req, timeout=None):
        target = req.full_url
        if "/oauth/token" in target:
            raise AssertionError("refresh grant should NOT fire when refresh creds are absent")
        raise urllib.error.HTTPError(
            target, 401, "Unauthorized", hdrs=Message(), fp=io.BytesIO(b"")
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    code, out, _ = _run(trakt_watch_history, monkeypatch, capsys)
    assert code == 1
    payload = json.loads(out)
    assert "HTTP 401" in payload["error"]


def test_401_with_refresh_grant_also_401_surfaces_original_failure(
    trakt_watch_history, monkeypatch, capsys, tmp_path
):
    """Refresh creds present but the refresh-grant itself returns 4xx
    (refresh token also expired/revoked). The data-fetch 401 should
    propagate as the surfaced error — operator sees `HTTP 401` and
    knows to run trakt-auth.py for a full device-code re-auth."""
    env_path = tmp_path / ".env"
    env_path.write_text("TRAKT_ACCESS_TOKEN=old\nTRAKT_REFRESH_TOKEN=old\n")

    def _fake_urlopen(req, timeout=None):
        target = req.full_url
        if "/oauth/token" in target:
            raise urllib.error.HTTPError(
                target, 401, "Unauthorized", hdrs=Message(), fp=io.BytesIO(b"")
            )
        raise urllib.error.HTTPError(
            target, 401, "Unauthorized", hdrs=Message(), fp=io.BytesIO(b"")
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    code, out, _ = _run_with_refresh_env(
        trakt_watch_history, monkeypatch, capsys, env_path=env_path
    )
    assert code == 1
    payload = json.loads(out)
    assert "HTTP 401" in payload["error"]


def test_no_double_retry_on_repeated_401(trakt_watch_history, monkeypatch, capsys, tmp_path):
    """If the data endpoint returns 401 AGAIN after a successful
    refresh, we MUST NOT loop — the retry budget is one shot. The
    second 401 propagates as a normal HTTP-error failure."""
    env_path = tmp_path / ".env"
    env_path.write_text("TRAKT_ACCESS_TOKEN=old\nTRAKT_REFRESH_TOKEN=old\n")

    state = {"shows_call_count": 0, "refresh_call_count": 0}

    def _fake_urlopen(req, timeout=None):
        target = req.full_url
        if "/oauth/token" in target:
            state["refresh_call_count"] += 1
            return _FakeResponse(json.dumps({"access_token": "new", "refresh_token": "new"}))
        if "/users/me/watched/shows" in target:
            state["shows_call_count"] += 1
            raise urllib.error.HTTPError(
                target, 401, "Unauthorized", hdrs=Message(), fp=io.BytesIO(b"")
            )
        return _FakeResponse("[]")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    code, out, _ = _run_with_refresh_env(
        trakt_watch_history, monkeypatch, capsys, env_path=env_path
    )
    assert code == 1
    # Exactly one refresh grant, exactly two data calls (initial + retry).
    assert state["refresh_call_count"] == 1
    assert state["shows_call_count"] == 2
    payload = json.loads(out)
    assert "HTTP 401" in payload["error"]


def test_401_without_env_path_does_not_refresh(trakt_watch_history, monkeypatch, capsys):
    """All three optional refresh credentials must be present.
    TRAKT_CLIENT_SECRET + TRAKT_REFRESH_TOKEN set, TRAKT_ENV_PATH
    absent → refresh MUST NOT fire. Reason: a refresh-grant rotates
    the refresh token (Trakt issues a new one in the response), so
    refreshing without persistence burns the rotation and leaves the
    next invocation with the same expired access token plus a
    now-invalid refresh token. Strictly worse than not refreshing."""
    monkeypatch.setattr("sys.argv", ["trakt-watch-history.py"])
    monkeypatch.setattr(trakt_watch_history, "datetime", _make_frozen_datetime(datetime))
    monkeypatch.setenv("TRAKT_CLIENT_ID", "cid")
    monkeypatch.setenv("TRAKT_ACCESS_TOKEN", "old")
    monkeypatch.setenv("TRAKT_CLIENT_SECRET", "secret")
    monkeypatch.setenv("TRAKT_REFRESH_TOKEN", "refresh")
    monkeypatch.delenv("TRAKT_ENV_PATH", raising=False)

    def _fake_urlopen(req, timeout=None):
        target = req.full_url
        if "/oauth/token" in target:
            raise AssertionError(
                "refresh grant must NOT fire when TRAKT_ENV_PATH is absent — "
                "would burn the rotating refresh token without persistence"
            )
        raise urllib.error.HTTPError(
            target, 401, "Unauthorized", hdrs=Message(), fp=io.BytesIO(b"")
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    code = 0
    try:
        result = trakt_watch_history.main()
        code = 0 if result is None else int(result)
    except SystemExit as exc:
        code = 0 if exc.code is None else int(exc.code)
    out = capsys.readouterr().out
    assert code == 1
    payload = json.loads(out)
    assert "HTTP 401" in payload["error"]


def test_401_refresh_dedups_pre_existing_stacked_token_lines(
    trakt_watch_history, monkeypatch, capsys, tmp_path
):
    """The older trakt-auth.py flow appended on every run, leaving
    stacked TRAKT_*_TOKEN= lines in .env. The first refresh after
    this PR lands MUST collapse stacked duplicates to exactly one
    of each key — keeping the first match, dropping the rest —
    rather than rewriting all matches with the new value (which
    would leave duplicates whose only difference vanished)."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "OTHER_VAR=preserved\n"
        "TRAKT_ACCESS_TOKEN=stale-1\n"
        "TRAKT_REFRESH_TOKEN=stale-1\n"
        "MIDDLE_VAR=between\n"
        "TRAKT_ACCESS_TOKEN=stale-2\n"
        "TRAKT_REFRESH_TOKEN=stale-2\n"
        "TRAKT_ACCESS_TOKEN=stale-3\n"
        "TAIL_VAR=tail\n"
    )

    fired = [False]

    def _fake_urlopen(req, timeout=None):
        target = req.full_url
        if "/oauth/token" in target:
            return _FakeResponse(json.dumps({"access_token": "fresh", "refresh_token": "fresh-r"}))
        if "/users/me/watched/shows" in target:
            if not fired[0]:
                fired[0] = True
                raise urllib.error.HTTPError(
                    target, 401, "Unauthorized", hdrs=Message(), fp=io.BytesIO(b"")
                )
            return _FakeResponse("[]")
        return _FakeResponse("[]")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    code, _, _ = _run_with_refresh_env(trakt_watch_history, monkeypatch, capsys, env_path=env_path)
    assert code == 0

    new_lines = env_path.read_text().splitlines()
    access_lines = [line for line in new_lines if line.startswith("TRAKT_ACCESS_TOKEN=")]
    refresh_lines = [line for line in new_lines if line.startswith("TRAKT_REFRESH_TOKEN=")]
    assert access_lines == ["TRAKT_ACCESS_TOKEN=fresh"], access_lines
    assert refresh_lines == ["TRAKT_REFRESH_TOKEN=fresh-r"], refresh_lines
    # All other vars preserved verbatim, ordering stable for non-trakt lines.
    assert "OTHER_VAR=preserved" in new_lines
    assert "MIDDLE_VAR=between" in new_lines
    assert "TAIL_VAR=tail" in new_lines


def test_401_refresh_http_error_surfaces_actionable_stderr_diagnostic(
    trakt_watch_history, monkeypatch, capsys, tmp_path
):
    """When the refresh-grant itself returns 4xx (refresh token dead),
    the script writes an actionable diagnostic to stderr — naming the
    HTTP code AND telling the operator exactly what to do
    (`docker exec ... python3 /app/scripts/trakt-auth.py`). Without
    this, operators with refresh creds configured see only the
    surfaced 401 and have no signal whether refresh was attempted."""
    env_path = tmp_path / ".env"
    env_path.write_text("TRAKT_ACCESS_TOKEN=old\nTRAKT_REFRESH_TOKEN=old\n")

    def _fake_urlopen(req, timeout=None):
        target = req.full_url
        if "/oauth/token" in target:
            raise urllib.error.HTTPError(
                target, 401, "Unauthorized", hdrs=Message(), fp=io.BytesIO(b"")
            )
        raise urllib.error.HTTPError(
            target, 401, "Unauthorized", hdrs=Message(), fp=io.BytesIO(b"")
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    code, out, err = _run_with_refresh_env(
        trakt_watch_history, monkeypatch, capsys, env_path=env_path
    )
    assert code == 1
    # Stderr must name the failure kind + recovery action.
    assert "HTTP 401" in err
    assert "refresh-grant" in err.lower()
    assert "trakt-auth.py" in err
    # Stdout still carries the JSON error envelope for the caller.
    payload = json.loads(out)
    assert "HTTP 401" in payload["error"]


def test_401_refresh_network_error_surfaces_distinct_diagnostic(
    trakt_watch_history, monkeypatch, capsys, tmp_path
):
    """When the refresh-grant fails with a network/Cloudflare error
    (URLError, not HTTPError), the stderr diagnostic must be DISTINCT
    from the refresh-token-dead case — operator gets "wait and retry"
    guidance, not "run device-code re-auth"."""
    env_path = tmp_path / ".env"
    env_path.write_text("TRAKT_ACCESS_TOKEN=old\nTRAKT_REFRESH_TOKEN=old\n")

    def _fake_urlopen(req, timeout=None):
        target = req.full_url
        if "/oauth/token" in target:
            raise urllib.error.URLError(reason="Cloudflare 1020 blocked")
        raise urllib.error.HTTPError(
            target, 401, "Unauthorized", hdrs=Message(), fp=io.BytesIO(b"")
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    code, out, err = _run_with_refresh_env(
        trakt_watch_history, monkeypatch, capsys, env_path=env_path
    )
    assert code == 1
    assert "network failure" in err.lower()
    assert "cloudflare" in err.lower() or "transient" in err.lower()
    # The HTTP-error recovery hint MUST NOT fire on this path —
    # device-code re-auth wouldn't help a network/Cloudflare outage.
    assert "trakt-auth.py" not in err


def test_401_refresh_malformed_200_response_surfaces_diagnostic(
    trakt_watch_history, monkeypatch, capsys, tmp_path
):
    """If the refresh-grant returns 200 but the body isn't valid
    JSON (proxy injected an HTML interstitial, partial body read),
    the script MUST NOT propagate JSONDecodeError as a traceback.
    Instead: stderr diagnostic naming the malformed-payload shape +
    fallback to the original 401 on stdout, same as the other
    refresh-failure paths."""
    env_path = tmp_path / ".env"
    env_path.write_text("TRAKT_ACCESS_TOKEN=old\nTRAKT_REFRESH_TOKEN=old\n")

    def _fake_urlopen(req, timeout=None):
        target = req.full_url
        if "/oauth/token" in target:
            return _FakeResponse(b"<html>Cloudflare interstitial</html>")
        raise urllib.error.HTTPError(
            target, 401, "Unauthorized", hdrs=Message(), fp=io.BytesIO(b"")
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    code, out, err = _run_with_refresh_env(
        trakt_watch_history, monkeypatch, capsys, env_path=env_path
    )
    assert code == 1
    assert "malformed" in err.lower()
    # Stdout still carries the structured JSON error envelope.
    payload = json.loads(out)
    assert "HTTP 401" in payload["error"]


def test_401_refresh_200_missing_access_token_surfaces_diagnostic(
    trakt_watch_history, monkeypatch, capsys, tmp_path
):
    """If the refresh-grant returns valid JSON but the payload is
    missing `access_token` (Trakt API contract change), the script
    MUST NOT propagate KeyError. Same fallback shape as the
    malformed-JSON path."""
    env_path = tmp_path / ".env"
    env_path.write_text("TRAKT_ACCESS_TOKEN=old\nTRAKT_REFRESH_TOKEN=old\n")

    def _fake_urlopen(req, timeout=None):
        target = req.full_url
        if "/oauth/token" in target:
            return _FakeResponse(json.dumps({"only_refresh": "weirdly-shaped-response"}))
        raise urllib.error.HTTPError(
            target, 401, "Unauthorized", hdrs=Message(), fp=io.BytesIO(b"")
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    code, out, err = _run_with_refresh_env(
        trakt_watch_history, monkeypatch, capsys, env_path=env_path
    )
    assert code == 1
    assert "malformed" in err.lower()
    payload = json.loads(out)
    assert "HTTP 401" in payload["error"]


def test_persist_tokens_normalizes_missing_final_newline(
    trakt_watch_history, monkeypatch, capsys, tmp_path
):
    """If the pre-existing .env's last line lacks a trailing newline,
    appending new TRAKT_*_TOKEN= lines without normalization would
    produce `EXISTING_VAR=valueTRAKT_ACCESS_TOKEN=...` — corrupted
    env file, broken on next read. The persist helper must inject a
    newline before appending."""
    env_path = tmp_path / ".env"
    # Deliberately no trailing newline on the final line.
    env_path.write_text("OTHER=preserved\nLAST=no-newline-here")

    fired = [False]

    def _fake_urlopen(req, timeout=None):
        target = req.full_url
        if "/oauth/token" in target:
            return _FakeResponse(json.dumps({"access_token": "fresh", "refresh_token": "fresh-r"}))
        if "/users/me/watched/shows" in target:
            if not fired[0]:
                fired[0] = True
                raise urllib.error.HTTPError(
                    target, 401, "Unauthorized", hdrs=Message(), fp=io.BytesIO(b"")
                )
            return _FakeResponse("[]")
        return _FakeResponse("[]")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    code, _, _ = _run_with_refresh_env(trakt_watch_history, monkeypatch, capsys, env_path=env_path)
    assert code == 0

    new_lines = env_path.read_text().splitlines()
    # Pre-existing lines preserved verbatim, new tokens on their own
    # lines (no concatenation with LAST=no-newline-here).
    assert "OTHER=preserved" in new_lines
    assert "LAST=no-newline-here" in new_lines
    assert "TRAKT_ACCESS_TOKEN=fresh" in new_lines
    assert "TRAKT_REFRESH_TOKEN=fresh-r" in new_lines
    # No concatenated-line corruption.
    assert not any("no-newline-hereTRAKT" in line for line in new_lines), env_path.read_text()
