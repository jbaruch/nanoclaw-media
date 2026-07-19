"""Baseline tests for skills/trakt-watch-history/scripts/trakt-watch-history.py.

Locks down the documented contract per `coding-policy: testing-standards`:

  - Reads `TRAKT_CLIENT_ID` from the environment at `main()` time;
    missing/empty → `{"error": "..."}` to stdout + exit 1
  - Trakt requests route through the OneCLI gateway proxy: each `api_get`
    sends `trakt-api-version: 2`, `trakt-api-key: <client_id>`, a browser
    `User-Agent`, and the placeholder `Authorization: Bearer
    onecli-managed` the gateway swaps for the real token. No
    access/refresh token is read or sent
  - Issues four `api_get` calls in sequence: watched/shows,
    watched/movies, ratings/shows, ratings/movies
  - Episodes-watched per show summed across `seasons[*].episodes`
  - Ratings attached by `slug` to both shows and movies; missing slug
    in ratings → `rating: None`
  - Output is sorted by `last_watched` descending
  - On success the record is written atomically to `TRAKT_HISTORY_OUT`
    AND printed to stdout: `{schema_version, shows, movies, stats:
    {total_shows, total_movies, rated}, fetched_at}`
  - Failure modes (all hit the `fail()` → JSON error + exit 1
    contract, leaving any existing record untouched):
      * `urllib.error.HTTPError` → `Trakt API <path> returned HTTP
        <code>: <preview>` (preview bounded to `ERROR_PREVIEW_BYTES`);
        401/403 add a gateway-reconnect hint
      * `urllib.error.URLError` with `TimeoutError` reason →
        `timed out after <N>s`
      * `urllib.error.URLError` other → `network error: <reason>`
      * bare `TimeoutError` (defensive fallback) → `timed out after
        <N>s (bare TimeoutError)`
      * `JSONDecodeError` on a non-JSON response → preview from raw
        bytes (consistent byte-bound across paths)

Tests freeze `module.datetime` (now() returning a fixed UTC instant)
so `fetched_at` is deterministic, patch `urllib.request.urlopen` to
drive each API path / failure branch without real network I/O, and
point `TRAKT_HISTORY_OUT` at a tmp file so the atomic write lands in a
scratch directory.
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


def _run(module, monkeypatch, capsys, tmp_path, *, env=None):
    monkeypatch.setattr("sys.argv", ["trakt-watch-history.py"])
    monkeypatch.setattr(module, "datetime", _make_frozen_datetime(datetime))
    out_path = tmp_path / "trakt-history.json"
    monkeypatch.setenv("TRAKT_HISTORY_OUT", str(out_path))
    if env is None:
        env = {"TRAKT_CLIENT_ID": "cid"}
    for k in ("TRAKT_CLIENT_ID",):
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
    return code, captured.out, captured.err, out_path


# ---------------------------------------------------------------------------
# Credential gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"TRAKT_CLIENT_ID": ""},
    ],
)
def test_missing_client_id_exits_1_with_error_json(
    trakt_watch_history, monkeypatch, capsys, tmp_path, env
):
    """TRAKT_CLIENT_ID missing OR empty → `{"error": "..."}` on stdout
    + exit 1, before any API call. The gateway owns the token, so
    client_id is the only credential the container needs."""
    code, out, _, _ = _run(trakt_watch_history, monkeypatch, capsys, tmp_path, env=env)
    assert code == 1
    payload = json.loads(out)
    assert "TRAKT_CLIENT_ID" in payload["error"]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_full_payload_written_and_printed(
    trakt_watch_history, monkeypatch, capsys, tmp_path
):
    """Both shows + movies + ratings populated → record sorted by
    `last_watched` desc, episodes summed across seasons, ratings
    attached by slug, stats consistent. The same record is written to
    TRAKT_HISTORY_OUT and printed to stdout."""
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

    code, out, _, out_path = _run(trakt_watch_history, monkeypatch, capsys, tmp_path)
    assert code == 0
    payload = json.loads(out)
    # The persisted record is byte-identical to what was printed.
    assert json.loads(out_path.read_text()) == payload
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


def test_show_without_rating_emits_none(trakt_watch_history, monkeypatch, capsys, tmp_path):
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

    _, out, _, _ = _run(trakt_watch_history, monkeypatch, capsys, tmp_path)
    payload = json.loads(out)
    assert payload["shows"][0]["rating"] is None
    assert payload["stats"]["rated"] == 0


def test_empty_history_is_a_valid_written_record(
    trakt_watch_history, monkeypatch, capsys, tmp_path
):
    """No shows and no movies is a VALID state (fresh/privacy-restricted
    account) — the record is still written with empty arrays and
    zeroed stats, not treated as a failure."""
    _patch_urlopen(monkeypatch, _all_endpoints())

    code, out, _, out_path = _run(trakt_watch_history, monkeypatch, capsys, tmp_path)
    assert code == 0
    payload = json.loads(out_path.read_text())
    assert payload == json.loads(out)
    assert payload["shows"] == []
    assert payload["movies"] == []
    assert payload["stats"] == {"total_shows": 0, "total_movies": 0, "rated": 0}


def test_request_carries_gateway_headers(trakt_watch_history, monkeypatch, capsys, tmp_path):
    """Each `api_get` constructs a Request with the gateway-routed Trakt
    headers: api-version, api-key from the client_id, browser UA, and
    the placeholder Bearer the gateway swaps. No real token is sent."""
    captured = {}

    def _capture(req, timeout=None):
        # Stash only the FIRST request — main() makes 4 api_get calls
        # back-to-back; the headers contract is identical across them,
        # so capturing once and asserting once is enough.
        if "request" not in captured:
            captured["request"] = req
        return _FakeResponse("[]")

    monkeypatch.setattr("urllib.request.urlopen", _capture)

    _run(trakt_watch_history, monkeypatch, capsys, tmp_path)
    headers = captured["request"].headers
    # `urllib.request.Request` normalizes header names by title-casing
    # the first letter of each word and lower-casing the rest, so the
    # documented `trakt-api-version` arrives back as `Trakt-api-version`.
    assert headers["Trakt-api-version"] == "2"
    assert headers["Trakt-api-key"] == "cid"
    # Placeholder Bearer — the gateway injects the real token. No
    # access/refresh token ever reaches this header.
    assert headers["Authorization"] == "Bearer onecli-managed"
    # Browser-shaped UA — Cloudflare in front of api.trakt.tv 403s
    # short custom UAs. Asserting on the Chrome marker keeps the test
    # robust against future Chrome version bumps in the UA literal
    # while still pinning the contract (real-browser shape).
    assert "Chrome/" in headers["User-agent"]
    assert headers["User-agent"].startswith("Mozilla/5.0")
    assert headers["Content-type"] == "application/json"


# ---------------------------------------------------------------------------
# Failure modes (each maps to fail() → exit 1 with JSON error)
# ---------------------------------------------------------------------------


def test_http_error_emits_status_and_bounded_preview(
    trakt_watch_history, monkeypatch, capsys, tmp_path
):
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

    code, out, _, _ = _run(trakt_watch_history, monkeypatch, capsys, tmp_path)
    assert code == 1
    payload = json.loads(out)
    assert "HTTP 502" in payload["error"]
    # Preview portion of the error message is bounded.
    assert payload["error"].count("Z") <= trakt_watch_history.ERROR_PREVIEW_BYTES + 50


def test_http_401_adds_gateway_reconnect_hint(trakt_watch_history, monkeypatch, capsys, tmp_path):
    """A 401/403 means the gateway could not inject a Trakt token. The
    error must point the operator at reconnecting the gateway's Trakt
    connection — not a local re-auth (the container holds no tokens)."""
    error = urllib.error.HTTPError(
        url="https://api.trakt.tv/users/me/watched/shows",
        code=401,
        msg="Unauthorized",
        hdrs=Message(),
        fp=io.BytesIO(b""),
    )
    _patch_urlopen(monkeypatch, {"/users/me/watched/shows": error})

    code, out, _, _ = _run(trakt_watch_history, monkeypatch, capsys, tmp_path)
    assert code == 1
    payload = json.loads(out)
    assert "HTTP 401" in payload["error"]
    assert "gateway" in payload["error"].lower()


def test_error_does_not_clobber_existing_record(trakt_watch_history, monkeypatch, capsys, tmp_path):
    """A failed fetch must NOT overwrite a previously-written
    trakt-history.json — the error goes to stdout, the existing record
    on disk is left intact."""
    out_path = tmp_path / "trakt-history.json"
    out_path.write_text('{"schema_version": 1, "shows": ["previous"]}')

    error = urllib.error.HTTPError(
        url="https://api.trakt.tv/users/me/watched/shows",
        code=500,
        msg="Server Error",
        hdrs=Message(),
        fp=io.BytesIO(b""),
    )
    _patch_urlopen(monkeypatch, {"/users/me/watched/shows": error})

    code, out, _, returned_path = _run(trakt_watch_history, monkeypatch, capsys, tmp_path)
    assert code == 1
    assert returned_path == out_path
    assert "error" in json.loads(out)
    # Prior record untouched.
    assert json.loads(out_path.read_text()) == {"schema_version": 1, "shows": ["previous"]}


def test_url_error_with_timeout_reason_message(trakt_watch_history, monkeypatch, capsys, tmp_path):
    """`URLError(reason=TimeoutError())` → operator-friendly "timed
    out after Ns" message."""
    err = urllib.error.URLError(reason=TimeoutError("read timed out"))
    _patch_urlopen(monkeypatch, {"/users/me/watched/shows": err})

    code, out, _, _ = _run(trakt_watch_history, monkeypatch, capsys, tmp_path)
    assert code == 1
    payload = json.loads(out)
    assert "timed out after" in payload["error"]
    assert str(trakt_watch_history.TIMEOUT_SECONDS) in payload["error"]


def test_url_error_other_reason_emits_network_error(
    trakt_watch_history, monkeypatch, capsys, tmp_path
):
    """`URLError` with non-timeout reason → "network error: <reason>"
    so the operator sees the underlying cause."""
    err = urllib.error.URLError(reason="Name or service not known")
    _patch_urlopen(monkeypatch, {"/users/me/watched/shows": err})

    code, out, _, _ = _run(trakt_watch_history, monkeypatch, capsys, tmp_path)
    assert code == 1
    payload = json.loads(out)
    assert "network error: Name or service not known" in payload["error"]


def test_bare_timeout_error_defensive_fallback(trakt_watch_history, monkeypatch, capsys, tmp_path):
    """Defensive `except TimeoutError` keeps a stable error message
    if the stdlib ever stops wrapping in URLError. The marker `(bare
    TimeoutError)` makes that drift immediately visible in the run
    log."""
    _patch_urlopen(monkeypatch, {"/users/me/watched/shows": TimeoutError("raw")})

    code, out, _, _ = _run(trakt_watch_history, monkeypatch, capsys, tmp_path)
    assert code == 1
    payload = json.loads(out)
    assert "(bare TimeoutError)" in payload["error"]


def test_non_json_response_emits_preview(trakt_watch_history, monkeypatch, capsys, tmp_path):
    """Non-JSON body → JSONDecodeError → fail() with `non-JSON
    response: <error>; preview=...`. Preview is sourced from the raw
    bytes for byte-bound consistency."""
    _patch_urlopen(monkeypatch, {"/users/me/watched/shows": b"<html>not json</html>"})

    code, out, _, _ = _run(trakt_watch_history, monkeypatch, capsys, tmp_path)
    assert code == 1
    payload = json.loads(out)
    assert "non-JSON response" in payload["error"]
    assert "preview=" in payload["error"]
