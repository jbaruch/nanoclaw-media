"""Tests for skills/check-watchlist/scripts/verify-release.py.

Locks down the documented contract per `coding-policy: testing-standards`:

  - Season suffixes the watchlist actually carries (`Fauda S5`,
    `MobLand Season 2`, `Slow Horses - Season 6`) split into a base
    title + season number; everything else stays a series-level title.
  - Title resolution is an EXACT (casefolded, whitespace-collapsed)
    match on the TVmaze `name`. A near-miss is `unknown`, never a guess
    — a wrong match would fire a false release alert.
  - Series-level entries verdict on the show's `premiered`; entries
    naming a season verdict on that season's `premiereDate`.
  - A premiere date on or before today is `released`, after today is
    `unreleased`.
  - A `released` verdict the entry's `platform` doesn't corroborate is
    downgraded to `unknown`: `platform_mismatch` for a different service
    (TVmaze reports the first airing anywhere, and Fauda S5's Israeli
    premiere is not the Netflix drop the watchlist waits on),
    `platform_unverified` when the source names no channel at all.
    Platforms compare as canonical slugs with an explicit alias table,
    never by substring — `Max` is a substring of `Cinemax`.
  - Lookups that never completed (HTTP error, network error, timeout,
    budget exhaustion) are `unknown` with `checked: False`, so the
    entry stays unstamped and the precheck keeps it due — an outage
    must not mute a title for a backoff interval.
  - A definitive negative from TVmaze (no exact match, season not
    listed, premiere date absent) IS `checked` and does back off.
  - Entries the run resolved get `last_checked` + `last_verdict` written
    back atomically, stamped with the watchlist `schema_version`, before
    the agent composes anything (jbaruch/nanoclaw-media#67).
  - Over-cap entries are deferred and REPORTED (stderr +
    `stats.skipped_over_cap`), never silently dropped.
  - An unwritable watchlist is a `write_error` warning, not a failure:
    the verdicts still hold and a released show must still be notified.
  - An unreadable/malformed watchlist is `{"error": ...}` + exit 1.

Tests freeze `module.datetime` and patch `urllib.request.urlopen`, so
no test depends on the wall clock or the network.
"""

from __future__ import annotations

import json
import os
import urllib.error
from datetime import date, datetime, timezone
from email.message import Message

import pytest

# Frozen "now" for every test. Premiere dates in the fixtures are picked
# relative to this instant, never to the real clock.
_FROZEN_NOW = datetime(2026, 8, 17, 9, 30, tzinfo=timezone.utc)
_TODAY = date(2026, 8, 17)
_API_BASE = "https://tvmaze.test"


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
        self._body = body.encode("utf-8") if isinstance(body, str) else body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def read(self):
        return self._body


def _patch_urlopen(monkeypatch, payloads, *, record=None):
    """Patch `urllib.request.urlopen` to dispatch per URL substring.

    `payloads` maps a URL substring to a Python object (json-serialized),
    raw bytes/str, or an Exception instance (raised on call). `record`,
    when given, collects the requested URLs and timeouts."""

    def _fake_urlopen(req, timeout=None):
        target = req.full_url if hasattr(req, "full_url") else str(req)
        if record is not None:
            record.append((target, timeout))
        for needle, payload in payloads.items():
            if needle in target:
                if isinstance(payload, Exception):
                    raise payload
                if isinstance(payload, (bytes, str)):
                    return _FakeResponse(payload)
                return _FakeResponse(json.dumps(payload))
        raise AssertionError(f"unexpected URL fetched: {target!r}")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)


def _show(name, *, show_id=1, premiered=None, web_channel=None, network=None):
    return {
        "id": show_id,
        "name": name,
        "premiered": premiered,
        "webChannel": {"name": web_channel} if web_channel else None,
        "network": {"name": network} if network else None,
    }


def _search(*shows):
    return [{"score": 10.0, "show": show} for show in shows]


def _entry(title, **extra):
    entry = {"title": title, "notified": False, "expected": "2026", "platform": "Netflix"}
    entry.update(extra)
    return entry


def _write_watchlist(tmp_path, entries):
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps({"tracking": entries}), encoding="utf-8")
    return path


def _run_main(module, monkeypatch, capsys, path):
    monkeypatch.setattr(module, "datetime", _make_frozen_datetime(datetime))
    monkeypatch.setenv("CHECK_WATCHLIST_PATH", str(path))
    monkeypatch.setenv("TVMAZE_API_BASE", _API_BASE)
    code = module.main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# _split_season() — the enumerable season suffixes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,base,season",
    [
        ("Fauda S5", "Fauda", 5),
        ("MobLand Season 2", "MobLand", 2),
        ("Slow Horses - Season 6", "Slow Horses", 6),
        ("The Day of the Jackal: Season 2", "The Day of the Jackal", 2),
        ("black doves s2", "black doves", 2),
        ("  Severance   S3  ", "Severance", 3),
        ("Presumed Innocent S10", "Presumed Innocent", 10),
        ("Black Doves", "Black Doves", None),
        ("Ted Lasso 2", "Ted Lasso 2", None),
        ("Se7en", "Se7en", None),
    ],
)
def test_split_season(verify_release, title, base, season):
    assert verify_release._split_season(title) == (base, season)


# ---------------------------------------------------------------------------
# _match_show() — exact-match-or-nothing
# ---------------------------------------------------------------------------


def test_match_show_is_case_and_whitespace_insensitive(verify_release):
    results = _search(_show("Slow   Horses"))
    assert verify_release._match_show(results, "slow horses")["name"] == "Slow   Horses"


def test_match_show_rejects_near_miss(verify_release):
    """TVmaze ranks fuzzy hits high; taking the top one would resolve a
    spin-off and fire a false alert."""
    results = _search(_show("Fauda: The Movie"), _show("Fauda Origins"))
    assert verify_release._match_show(results, "Fauda") is None


def test_match_show_skips_malformed_results(verify_release):
    results = ["not-a-dict", {"show": None}, {"show": {"name": None}}, {"show": _show("Fauda")}]
    assert verify_release._match_show(results, "Fauda")["id"] == 1


def test_match_show_returns_none_when_not_a_list(verify_release):
    assert verify_release._match_show({"error": "nope"}, "Fauda") is None


# ---------------------------------------------------------------------------
# verify_entry() — verdicts
# ---------------------------------------------------------------------------


def _verify_one(module, monkeypatch, entry, payloads, *, record=None):
    _patch_urlopen(monkeypatch, payloads, record=record)
    deadline = module.time.monotonic() + module.TOTAL_BUDGET_SECONDS
    return module.verify_entry(entry, _TODAY, _API_BASE, deadline)


def test_show_premiere_in_the_past_is_released(verify_release, monkeypatch):
    result = _verify_one(
        verify_release,
        monkeypatch,
        _entry("Black Doves"),
        {
            "/search/shows": _search(
                _show("Black Doves", premiered="2026-08-01", web_channel="Netflix")
            )
        },
    )
    assert result["verdict"] == "released"
    assert result["detail"] == "show_premiere"
    assert result["premiere_date"] == "2026-08-01"
    assert result["platform"] == "Netflix"
    assert result["checked"] is True


def test_show_premiere_today_is_released(verify_release, monkeypatch):
    """Boundary: a premiere landing on today's UTC date counts as out."""
    result = _verify_one(
        verify_release,
        monkeypatch,
        _entry("Black Doves"),
        {
            "/search/shows": _search(
                _show("Black Doves", premiered="2026-08-17", web_channel="Netflix")
            )
        },
    )
    assert result["verdict"] == "released"


def test_show_premiere_in_the_future_is_unreleased(verify_release, monkeypatch):
    result = _verify_one(
        verify_release,
        monkeypatch,
        _entry("Black Doves"),
        {"/search/shows": _search(_show("Black Doves", premiered="2026-08-18"))},
    )
    assert result["verdict"] == "unreleased"
    assert result["premiere_date"] == "2026-08-18"
    assert result["checked"] is True


def test_missing_premiere_date_is_unknown_but_checked(verify_release, monkeypatch):
    result = _verify_one(
        verify_release,
        monkeypatch,
        _entry("Black Doves"),
        {"/search/shows": _search(_show("Black Doves", premiered=None))},
    )
    assert result["verdict"] == "unknown"
    assert result["detail"] == "premiere_date_missing"
    assert result["checked"] is True


def test_unparseable_premiere_date_is_unknown(verify_release, monkeypatch):
    result = _verify_one(
        verify_release,
        monkeypatch,
        _entry("Black Doves"),
        {"/search/shows": _search(_show("Black Doves", premiered="soon"))},
    )
    assert result["verdict"] == "unknown"


def test_no_exact_match_is_unknown_and_checked(verify_release, monkeypatch):
    """A definitive negative from TVmaze — the skill's bounded search
    covers it, and the entry backs off rather than re-asking nightly."""
    result = _verify_one(
        verify_release,
        monkeypatch,
        _entry("Fauda S5"),
        {"/search/shows": _search(_show("Fauda: The Movie"))},
    )
    assert result["verdict"] == "unknown"
    assert result["detail"] == "no_exact_title_match"
    assert result["checked"] is True


def test_season_premiere_in_the_past_is_released(verify_release, monkeypatch):
    result = _verify_one(
        verify_release,
        monkeypatch,
        _entry("Fauda S5"),
        {
            "/search/shows": _search(_show("Fauda", show_id=7, network="Yes")),
            "/shows/7/seasons": [
                {"number": 4, "premiereDate": "2022-01-01"},
                {"number": 5, "premiereDate": "2026-05-18", "webChannel": {"name": "Netflix"}},
            ],
        },
    )
    assert result["verdict"] == "released"
    assert result["detail"] == "season_premiere"
    assert result["premiere_date"] == "2026-05-18"
    assert result["platform"] == "Netflix"


def test_season_premiere_in_the_future_is_unreleased(verify_release, monkeypatch):
    result = _verify_one(
        verify_release,
        monkeypatch,
        _entry("MobLand Season 2"),
        {
            "/search/shows": _search(_show("MobLand", show_id=3, web_channel="Paramount+")),
            "/shows/3/seasons": [{"number": 2, "premiereDate": "2026-11-01"}],
        },
    )
    assert result["verdict"] == "unreleased"
    assert result["premiere_date"] == "2026-11-01"
    # Season carries no channel of its own — falls back to the show's.
    assert result["platform"] == "Paramount+"


@pytest.mark.parametrize(
    "entry_platform,resolved,matches",
    [
        ("Netflix", "Netflix", True),
        ("netflix", "  Netflix  ", True),
        # Same service, two spellings — punctuation strips to one slug.
        ("Apple TV+", "Apple TV", True),
        ("Paramount+", "Paramount", True),
        # Aliases that need the explicit table.
        ("Prime Video", "Amazon Prime Video", True),
        ("Disney+", "Disney Plus", True),
        ("HBO Max", "Max", True),
        # Distinct services that substring-matching used to conflate.
        ("Max", "Cinemax", False),
        ("Cinemax", "Max", False),
        ("Netflix", "Yes", False),
        # An entry that names a platform against a channel-less premiere
        # has no corroboration — never an alert.
        ("Netflix", None, False),
        ("Netflix", "   ", False),
        # An entry that names no platform has nothing to check.
        (None, "Yes", True),
        ("", "Yes", True),
        ("   ", None, True),
    ],
)
def test_platform_matches(verify_release, entry_platform, resolved, matches):
    assert verify_release._platform_matches(entry_platform, resolved) is matches


def test_a_past_premiere_with_no_channel_is_not_an_alert(verify_release, monkeypatch):
    """The entry tracks Netflix and the source names no channel at all.
    Alerting "now available on Netflix" here would be a claim with
    nothing behind it."""
    result = _verify_one(
        verify_release,
        monkeypatch,
        _entry("Black Doves", platform="Netflix"),
        {"/search/shows": _search(_show("Black Doves", premiered="2026-08-01"))},
    )
    assert result["verdict"] == "unknown"
    assert result["detail"] == "platform_unverified"
    assert result["premiere_date"] == "2026-08-01"
    assert result["checked"] is True


def test_a_past_premiere_releases_when_the_entry_names_no_platform(verify_release, monkeypatch):
    """Nothing to corroborate means nothing to contradict."""
    entry = _entry("Black Doves")
    del entry["platform"]
    result = _verify_one(
        verify_release,
        monkeypatch,
        entry,
        {"/search/shows": _search(_show("Black Doves", premiered="2026-08-01"))},
    )
    assert result["verdict"] == "released"
    assert result["detail"] == "show_premiere"


def test_first_airing_elsewhere_is_not_a_release(verify_release, monkeypatch):
    """The Fauda S5 case from #67: TVmaze carries the 2026-05-18 Israeli
    Yes premiere while the watchlist tracks the later Netflix
    international drop. Alerting on it would be a false 'now available on
    Netflix' — downgrade to `unknown` and let the bounded search rule."""
    result = _verify_one(
        verify_release,
        monkeypatch,
        _entry("Fauda S5", platform="Netflix"),
        {
            "/search/shows": _search(_show("Fauda", show_id=7)),
            "/shows/7/seasons": [
                {"number": 5, "premiereDate": "2026-05-18", "network": {"name": "Yes"}}
            ],
        },
    )
    assert result["verdict"] == "unknown"
    assert result["detail"] == "platform_mismatch"
    # Both facts survive for the skill's search prompt.
    assert result["premiere_date"] == "2026-05-18"
    assert result["platform"] == "Yes"
    assert result["checked"] is True


def test_platform_gate_does_not_touch_future_premieres(verify_release, monkeypatch):
    """A premiere still in the future is `unreleased` on any channel —
    the gate only guards the released verdict."""
    result = _verify_one(
        verify_release,
        monkeypatch,
        _entry("Fauda S5", platform="Netflix"),
        {
            "/search/shows": _search(_show("Fauda", show_id=7)),
            "/shows/7/seasons": [
                {"number": 5, "premiereDate": "2026-12-01", "network": {"name": "Yes"}}
            ],
        },
    )
    assert result["verdict"] == "unreleased"
    assert result["detail"] == "season_premiere"


def test_matching_platform_still_releases(verify_release, monkeypatch):
    result = _verify_one(
        verify_release,
        monkeypatch,
        _entry("Black Doves", platform="Netflix"),
        {
            "/search/shows": _search(
                _show("Black Doves", premiered="2026-08-01", web_channel="Netflix")
            )
        },
    )
    assert result["verdict"] == "released"


def test_season_not_listed_is_unknown_but_checked(verify_release, monkeypatch):
    result = _verify_one(
        verify_release,
        monkeypatch,
        _entry("Severance S3"),
        {
            "/search/shows": _search(_show("Severance", show_id=9)),
            "/shows/9/seasons": [{"number": 2, "premiereDate": "2026-01-17"}],
        },
    )
    assert result["verdict"] == "unknown"
    assert result["detail"] == "season_not_listed"
    assert result["checked"] is True


def test_season_without_premiere_date_is_unknown(verify_release, monkeypatch):
    result = _verify_one(
        verify_release,
        monkeypatch,
        _entry("Severance S3"),
        {
            "/search/shows": _search(_show("Severance", show_id=9)),
            "/shows/9/seasons": [{"number": 3, "premiereDate": None}],
        },
    )
    assert result["verdict"] == "unknown"
    assert result["detail"] == "season_premiere_missing"
    assert result["checked"] is True


@pytest.mark.parametrize("entry", [{"notified": False}, {"notified": False, "title": "  "}])
def test_entry_without_title_is_unknown(verify_release, monkeypatch, entry):
    """A sentinel, not an empty string: the result has to name itself as
    unsearchable so Step 3 doesn't spend a search slot on it."""
    _patch_urlopen(monkeypatch, {})
    deadline = verify_release.time.monotonic() + 10
    result = verify_release.verify_entry(entry, _TODAY, _API_BASE, deadline)
    assert result["verdict"] == "unknown"
    assert result["detail"] == "title_missing"
    assert result["title"] == verify_release.UNTITLED
    assert result["checked"] is True


def test_budget_exhausted_entry_without_title_gets_the_sentinel(verify_release, monkeypatch):
    _patch_urlopen(monkeypatch, {})
    results = verify_release.verify(
        [{"notified": False}], _TODAY, _API_BASE, verify_release.time.monotonic() - 1
    )
    assert results[0]["title"] == verify_release.UNTITLED
    assert results[0]["detail"] == "budget_exhausted"


def test_search_query_carries_the_season_stripped_title(verify_release, monkeypatch):
    record: list = []
    _verify_one(
        verify_release,
        monkeypatch,
        _entry("Slow Horses - Season 6"),
        {
            "/search/shows": _search(_show("Slow Horses", show_id=2)),
            "/shows/2/seasons": [{"number": 6, "premiereDate": "2026-09-10"}],
        },
        record=record,
    )
    assert "q=Slow+Horses" in record[0][0]
    assert record[0][0].startswith(f"{_API_BASE}/search/shows")


def test_per_call_timeout_is_capped(verify_release, monkeypatch):
    """Every request carries a bound; none is allowed to run open-ended."""
    record: list = []
    _verify_one(
        verify_release,
        monkeypatch,
        _entry("Black Doves"),
        {
            "/search/shows": _search(
                _show("Black Doves", premiered="2026-01-01", web_channel="Netflix")
            )
        },
        record=record,
    )
    assert 0 < record[0][1] <= verify_release.PER_CALL_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# verify_entry() — lookups that never completed stay unstamped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error,detail",
    [
        (urllib.error.URLError(TimeoutError("timed out")), "timeout"),
        (urllib.error.URLError("connection refused"), "network: connection refused"),
        (TimeoutError("bare"), "timeout"),
    ],
)
def test_transport_failures_are_unknown_and_unchecked(verify_release, monkeypatch, error, detail):
    result = _verify_one(
        verify_release, monkeypatch, _entry("Black Doves"), {"/search/shows": error}
    )
    assert result["verdict"] == "unknown"
    assert result["detail"] == detail
    assert result["checked"] is False


def test_http_error_is_unknown_and_unchecked(verify_release, monkeypatch):
    error = urllib.error.HTTPError(f"{_API_BASE}/search/shows", 429, "Too Many", Message(), None)
    result = _verify_one(
        verify_release, monkeypatch, _entry("Black Doves"), {"/search/shows": error}
    )
    assert result["verdict"] == "unknown"
    assert result["detail"] == "http_429"
    assert result["checked"] is False


def test_non_json_response_is_unknown_and_unchecked(verify_release, monkeypatch):
    result = _verify_one(
        verify_release, monkeypatch, _entry("Black Doves"), {"/search/shows": "<html>nope</html>"}
    )
    assert result["verdict"] == "unknown"
    assert result["detail"].startswith("non_json")
    assert result["checked"] is False


def test_seasons_call_failure_is_unknown_and_unchecked(verify_release, monkeypatch):
    result = _verify_one(
        verify_release,
        monkeypatch,
        _entry("Fauda S5"),
        {
            "/search/shows": _search(_show("Fauda", show_id=7)),
            "/shows/7/seasons": urllib.error.URLError(TimeoutError("timed out")),
        },
    )
    assert result["verdict"] == "unknown"
    assert result["detail"] == "timeout"
    assert result["checked"] is False


# ---------------------------------------------------------------------------
# verify() — the wall-clock budget
# ---------------------------------------------------------------------------


def test_expired_budget_short_circuits_every_entry(verify_release, monkeypatch):
    """The whole point of the script: an exhausted budget returns rather
    than running past the 300s host inactivity watchdog."""
    _patch_urlopen(monkeypatch, {})  # any request would fail the test
    results = verify_release.verify(
        [_entry("Black Doves"), _entry("Fauda S5")],
        _TODAY,
        _API_BASE,
        verify_release.time.monotonic() - 1,
    )
    assert [r["detail"] for r in results] == ["budget_exhausted", "budget_exhausted"]
    assert all(r["checked"] is False for r in results)


def test_budget_expiring_mid_run_marks_the_rest(verify_release, monkeypatch):
    """A budget that expires after the first lookup leaves the remaining
    entries unresolved and unstamped, not silently answered."""
    # Ticks: entry 1's loop check, entry 1's request budget check, then
    # a clock past the deadline for every later read.
    ticks = [0.0, 0.0, 999.0]
    monkeypatch.setattr(
        verify_release.time, "monotonic", lambda: ticks.pop(0) if len(ticks) > 1 else ticks[0]
    )
    _patch_urlopen(
        monkeypatch,
        {
            "/search/shows": _search(
                _show("Black Doves", premiered="2026-01-01", web_channel="Netflix")
            )
        },
    )
    results = verify_release.verify(
        [_entry("Black Doves"), _entry("Later")], _TODAY, _API_BASE, 10.0
    )
    assert results[0]["verdict"] == "released"
    assert results[1]["detail"] == "budget_exhausted"
    assert results[1]["checked"] is False


# ---------------------------------------------------------------------------
# main() — bookkeeping write-back and the stdout contract
# ---------------------------------------------------------------------------


def test_main_stamps_only_resolved_entries(verify_release, monkeypatch, capsys, tmp_path):
    path = _write_watchlist(
        tmp_path,
        [
            _entry("Black Doves"),
            _entry("Fauda S5"),
            {"title": "Old News", "notified": True, "expected": "2025"},
        ],
    )
    _patch_urlopen(
        monkeypatch,
        {
            "q=Black+Doves": _search(_show("Black Doves", premiered="2026-08-18")),
            "q=Fauda": urllib.error.URLError(TimeoutError("timed out")),
        },
    )
    code, out, _ = _run_main(verify_release, monkeypatch, capsys, path)

    assert code == 0
    payload = json.loads(out)
    assert payload["stats"] == {
        "entries": 2,
        "resolved": 1,
        "released": 0,
        "unreleased": 1,
        "unknown": 1,
        "skipped_over_cap": 0,
    }

    written = json.loads(path.read_text())
    assert written["schema_version"] == verify_release.WATCHLIST_SCHEMA_VERSION
    assert written["tracking"][0]["last_checked"] == "2026-08-17"
    assert written["tracking"][0]["last_verdict"] == "unreleased"
    # The timed-out lookup leaves no stamp — an outage must not mute it.
    assert "last_checked" not in written["tracking"][1]
    assert "last_verdict" not in written["tracking"][1]
    # A notified entry is out of scope entirely.
    assert "last_checked" not in written["tracking"][2]


def test_main_stamps_released_verdict(verify_release, monkeypatch, capsys, tmp_path):
    path = _write_watchlist(tmp_path, [_entry("Black Doves")])
    _patch_urlopen(
        monkeypatch,
        {
            "/search/shows": _search(
                _show("Black Doves", premiered="2026-08-01", web_channel="Netflix")
            )
        },
    )
    code, out, _ = _run_main(verify_release, monkeypatch, capsys, path)

    assert code == 0
    payload = json.loads(out)
    assert payload["schema_version"] == verify_release.OUTPUT_SCHEMA_VERSION
    assert payload["results"][0]["verdict"] == "released"
    assert payload["checked_at"] == "2026-08-17T09:30:00+00:00"
    written = json.loads(path.read_text())
    assert written["tracking"][0]["last_verdict"] == "released"
    # `notified` stays the skill's to flip, after the alert lands.
    assert written["tracking"][0]["notified"] is False


def test_main_leaves_file_untouched_when_nothing_resolved(
    verify_release, monkeypatch, capsys, tmp_path
):
    path = _write_watchlist(tmp_path, [_entry("Black Doves")])
    before = path.read_text()
    _patch_urlopen(monkeypatch, {"/search/shows": urllib.error.URLError("down")})
    code, out, _ = _run_main(verify_release, monkeypatch, capsys, path)
    assert code == 0
    assert json.loads(out)["stats"]["resolved"] == 0
    assert path.read_text() == before


def test_main_is_a_no_op_when_nothing_is_unnotified(verify_release, monkeypatch, capsys, tmp_path):
    path = _write_watchlist(tmp_path, [{"title": "Done", "notified": True}])
    before = path.read_text()
    _patch_urlopen(monkeypatch, {})
    code, out, _ = _run_main(verify_release, monkeypatch, capsys, path)
    assert code == 0
    assert json.loads(out)["stats"]["entries"] == 0
    assert path.read_text() == before


def test_main_reports_entries_over_the_cap(verify_release, monkeypatch, capsys, tmp_path):
    entries = [_entry(f"Show {i}") for i in range(verify_release.MAX_ENTRIES + 3)]
    path = _write_watchlist(tmp_path, entries)
    _patch_urlopen(monkeypatch, {"/search/shows": []})
    code, out, err = _run_main(verify_release, monkeypatch, capsys, path)

    assert code == 0
    payload = json.loads(out)
    assert payload["stats"]["entries"] == verify_release.MAX_ENTRIES
    assert payload["stats"]["skipped_over_cap"] == 3
    assert "deferred to the next run" in err


def test_prioritize_puts_the_least_recently_resolved_first(verify_release):
    entries = [
        _entry("Checked Today", last_checked="2026-08-17"),
        _entry("Never Checked"),
        _entry("Checked Last Month", last_checked="2026-07-01"),
        _entry("Bad Stamp", last_checked=20260817),
    ]
    order = [e["title"] for e in verify_release._prioritize(entries)]
    # Unstamped and unusable-stamp entries lead, in watchlist order;
    # then oldest resolution first.
    assert order == ["Never Checked", "Bad Stamp", "Checked Last Month", "Checked Today"]


def test_capped_runs_rotate_instead_of_starving_the_tail(
    verify_release, monkeypatch, capsys, tmp_path
):
    """Two consecutive capped runs. Taking the first MAX_ENTRIES by file
    order every time would re-resolve the same leading entries forever
    and never reach the tail."""
    overflow = 3
    titles = [f"Show {i:02d}" for i in range(verify_release.MAX_ENTRIES + overflow)]
    path = _write_watchlist(tmp_path, [_entry(t) for t in titles])
    _patch_urlopen(monkeypatch, {"/search/shows": []})  # every title resolves to unknown

    _, first_out, first_err = _run_main(verify_release, monkeypatch, capsys, path)
    first = {r["title"] for r in json.loads(first_out)["results"]}
    assert len(first) == verify_release.MAX_ENTRIES
    deferred = set(titles) - first
    assert len(deferred) == overflow
    for title in deferred:
        assert title in first_err

    _, second_out, _ = _run_main(verify_release, monkeypatch, capsys, path)
    second = {r["title"] for r in json.loads(second_out)["results"]}
    # Every entry the first run deferred leads the second one.
    assert deferred <= second
    # And two runs cover the whole watchlist.
    assert first | second == set(titles)


@pytest.mark.parametrize("version", [2, 0, -1, "1", 1.5, True])
def test_main_leaves_a_record_it_did_not_author_untouched(
    verify_release, monkeypatch, capsys, tmp_path, version
):
    """The owner writes only the shape it authored. Stamping v1 fields
    onto a record at another version — and rewriting the marker to 1 —
    would silently downgrade it into a shape nothing understands."""
    path = tmp_path / "watchlist.json"
    path.write_text(
        json.dumps({"schema_version": version, "tracking": [_entry("Black Doves")]}),
        encoding="utf-8",
    )
    before = path.read_text()
    _patch_urlopen(
        monkeypatch,
        {
            "/search/shows": _search(
                _show("Black Doves", premiered="2026-08-01", web_channel="Netflix")
            )
        },
    )
    code, out, err = _run_main(verify_release, monkeypatch, capsys, path)

    assert code == 0
    payload = json.loads(out)
    # The verdict still comes back — a released show must still notify.
    assert payload["results"][0]["verdict"] == "released"
    assert "is not 1" in payload["write_skipped"]
    assert "is not 1" in err
    assert path.read_text() == before


@pytest.mark.parametrize("version", [None, 1])
def test_main_stamps_a_record_at_its_own_version(
    verify_release, monkeypatch, capsys, tmp_path, version
):
    record = {"tracking": [_entry("Black Doves")]}
    if version is not None:
        record["schema_version"] = version
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    _patch_urlopen(
        monkeypatch,
        {
            "/search/shows": _search(
                _show("Black Doves", premiered="2026-08-01", web_channel="Netflix")
            )
        },
    )
    _, out, _ = _run_main(verify_release, monkeypatch, capsys, path)

    assert "write_skipped" not in json.loads(out)
    written = json.loads(path.read_text())
    assert written["schema_version"] == 1
    assert written["tracking"][0]["last_verdict"] == "released"


def test_main_does_not_warn_about_version_when_there_is_nothing_to_write(
    verify_release, monkeypatch, capsys, tmp_path
):
    """No unnotified entries means no stamping either way — a version
    warning there would be noise on every quiet night."""
    path = tmp_path / "watchlist.json"
    path.write_text(
        json.dumps({"schema_version": 2, "tracking": [{"title": "Done", "notified": True}]}),
        encoding="utf-8",
    )
    _patch_urlopen(monkeypatch, {})
    code, out, err = _run_main(verify_release, monkeypatch, capsys, path)
    assert code == 0
    assert "write_skipped" not in json.loads(out)
    assert err == ""


def test_main_warns_but_succeeds_when_the_write_fails(
    verify_release, monkeypatch, capsys, tmp_path
):
    """A released show must still be notified when the bookkeeping write
    fails, so the write error is a warning on stdout + stderr, not a
    non-zero exit that aborts the skill."""
    locked = tmp_path / "locked"
    locked.mkdir()
    path = locked / "watchlist.json"
    path.write_text(json.dumps({"tracking": [_entry("Black Doves")]}), encoding="utf-8")
    _patch_urlopen(
        monkeypatch,
        {
            "/search/shows": _search(
                _show("Black Doves", premiered="2026-08-01", web_channel="Netflix")
            )
        },
    )
    os.chmod(locked, 0o500)
    try:
        code, out, err = _run_main(verify_release, monkeypatch, capsys, path)
    finally:
        os.chmod(locked, 0o700)

    assert code == 0
    payload = json.loads(out)
    assert payload["results"][0]["verdict"] == "released"
    assert "could not write" in payload["write_error"]
    assert "could not write" in err


def test_main_errors_on_missing_file(verify_release, monkeypatch, capsys, tmp_path):
    code, out, _ = _run_main(verify_release, monkeypatch, capsys, tmp_path / "missing.json")
    assert code == 1
    assert "cannot read" in json.loads(out)["error"]


def test_main_errors_on_malformed_json(verify_release, monkeypatch, capsys, tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text("{not json", encoding="utf-8")
    code, out, _ = _run_main(verify_release, monkeypatch, capsys, path)
    assert code == 1
    assert "not valid JSON" in json.loads(out)["error"]


def test_main_handles_an_empty_file(verify_release, monkeypatch, capsys, tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text("", encoding="utf-8")
    _patch_urlopen(monkeypatch, {})
    code, out, _ = _run_main(verify_release, monkeypatch, capsys, path)
    assert code == 0
    assert json.loads(out)["stats"]["entries"] == 0
