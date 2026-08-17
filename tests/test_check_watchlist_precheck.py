"""Tests for skills/check-watchlist/scripts/check-watchlist-precheck.py.

Locks down the documented contract:

  - File missing → no-wake (file_missing).
  - File unreadable / malformed JSON / non-UTF-8 → no-wake
    (file_unreadable). Per jbaruch/nanoclaw#516, the agent has nothing
    useful to do with a broken file; silent skip is safe.
  - JSON valid but `tracking` not a list → no-wake (tracking_missing).
  - No entry has `notified: false` → no-wake (all_notified).
  - Every unnotified show's release window is beyond the lead → no-wake
    (all_future). This is the steady state for far-off tracked shows and
    is the bug jbaruch/nanoclaw-media#2 fixes: `notified: false` alone is
    no longer a wake trigger.
  - At least one unnotified show is due within the lead window — or has
    an un-parseable `expected` (conservative wake) → wake (release_due)
    with `due_count` and `titles` so the agent's first turn doesn't
    re-read the file.
  - Fuzzy `expected` parsing: ISO date / `YYYY-Qn` / `YYYY-MM` / bare
    `YYYY`.
  - Recheck backoff (jbaruch/nanoclaw-media#67): an entry a verification
    run already resolved is re-asked no more often than its `expected`
    precision warrants, so a coarse window stops waking the agent
    nightly for a year. A `released` verdict is never suppressed — that
    alert is still undelivered while `notified` is false.
  - Schema gate: the backoff stamps are read only when the record's
    `schema_version` is absent (legacy pre-v1) or at/below
    `SUPPORTED_SCHEMA_VERSION`. A newer or malformed stamp is no usable
    prior state — date-gate alone, which wakes, never suppresses.
  - main() always exits 0 with valid JSON on stdout.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_REL = "skills/check-watchlist/scripts/check-watchlist-precheck.py"

# Frozen clock matching the jbaruch/nanoclaw-media#2 repro (verified
# 2026-06-12). LEAD is 7 days, so the cutoff is 2026-06-19.
NOW = datetime(2026, 6, 12, tzinfo=timezone.utc)
_TODAY = NOW.date()
# Fuzzy-format fixtures are built from the frozen reference's year, and
# dated ones from offsets against it, so no fixture is a future literal
# against the real clock. Fixed PAST literals stay literal, which
# `coding-policy: testing-standards` permits.
_YEAR = NOW.year
_NEXT_YEAR = _YEAR + 1


def _after(days: int) -> str:
    return (_TODAY + timedelta(days=days)).isoformat()


def _before(days: int) -> str:
    return (_TODAY - timedelta(days=days)).isoformat()


@pytest.fixture
def precheck():
    spec = importlib.util.spec_from_file_location(
        "check_watchlist_precheck_under_test", REPO_ROOT / SCRIPT_REL
    )
    if spec is None or spec.loader is None:
        raise ImportError("cannot load module spec")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(tmp_path, payload) -> Path:
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps(payload))
    return path


# ---------------------------------------------------------------------------
# _window_start() — fuzzy `expected` parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected_iso",
    [
        (_after(6), _after(6)),
        (f"{_YEAR}-Q1", f"{_YEAR}-01-01"),
        (f"{_YEAR}-Q2", f"{_YEAR}-04-01"),
        (f"{_YEAR}-Q3", f"{_YEAR}-07-01"),
        (f"{_YEAR}-Q4", f"{_YEAR}-10-01"),
        (f"{_YEAR}-q4", f"{_YEAR}-10-01"),  # lowercase tolerated
        (f"  {_NEXT_YEAR}  ", f"{_NEXT_YEAR}-01-01"),  # whitespace tolerated
        (f"{_NEXT_YEAR}", f"{_NEXT_YEAR}-01-01"),
        # YYYY-MM was un-parseable before jbaruch/nanoclaw-media#67 and
        # fell through to the conservative nightly wake.
        (f"{_YEAR}-10", f"{_YEAR}-10-01"),
        (f"{_YEAR}-01", f"{_YEAR}-01-01"),
        (f"{_YEAR}-12", f"{_YEAR}-12-01"),
    ],
)
def test_window_start_parses_fuzzy_formats(precheck, value, expected_iso):
    assert precheck._window_start(value).isoformat() == expected_iso


@pytest.mark.parametrize(
    "value",
    ["TBA", "summer", "", None, 2026, f"{_YEAR}-13-40", f"{_YEAR}-13", f"{_YEAR}-1", f"{_YEAR}-00"],
)
def test_window_start_returns_none_for_unparseable(precheck, value):
    assert precheck._window_start(value) is None


# ---------------------------------------------------------------------------
# _recheck_interval() — backoff scales with `expected` precision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,days",
    [
        (_after(6), 1),
        (f"{_YEAR}-10", 7),
        (f"{_YEAR}-Q4", 14),
        (f"{_YEAR}", 30),
        ("TBA", 30),
        (None, 30),
    ],
)
def test_recheck_interval_scales_with_precision(precheck, value, days):
    assert precheck._recheck_interval(value).days == days


# ---------------------------------------------------------------------------
# decide() — pure decision function (file/JSON guards)
# ---------------------------------------------------------------------------


def test_decide_no_wake_when_file_missing(precheck, tmp_path):
    result = precheck.decide(NOW, tmp_path / "missing.json")
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "file_missing"


def test_decide_no_wake_on_malformed_json(precheck, tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text("{not valid json")
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "file_unreadable"
    assert "JSON malformed" in result["data"]["error"]


def test_decide_no_wake_on_non_utf8(precheck, tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_bytes(b"\xff\xfe\x00not-valid-utf-8")
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "file_unreadable"


def test_decide_no_wake_on_empty_file(precheck, tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text("")
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "tracking_missing"


def test_decide_no_wake_when_tracking_missing(precheck, tmp_path):
    path = _write(tmp_path, {"other_key": []})
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "tracking_missing"


def test_decide_no_wake_when_root_is_list(precheck, tmp_path):
    path = _write(tmp_path, [{"title": "a", "notified": False}])
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "tracking_missing"


def test_decide_no_wake_when_tracking_empty(precheck, tmp_path):
    path = _write(tmp_path, {"tracking": []})
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "all_notified"
    assert result["data"]["tracking_count"] == 0


def test_decide_no_wake_when_all_notified(precheck, tmp_path):
    path = _write(
        tmp_path,
        {
            "tracking": [
                {"title": "Show A", "notified": True},
                {"title": "Show B", "notified": True},
            ]
        },
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "all_notified"
    assert result["data"]["tracking_count"] == 2


def test_decide_treats_missing_notified_as_already_handled(precheck, tmp_path):
    """`notified: false` is required — a missing field stays silent."""
    path = _write(
        tmp_path,
        {
            "tracking": [
                {"title": "No Field", "expected": "2026"},
                {"title": "Truthy", "notified": True},
            ]
        },
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "all_notified"


# ---------------------------------------------------------------------------
# decide() — date gating (the jbaruch/nanoclaw-media#2 fix)
# ---------------------------------------------------------------------------


def test_decide_no_wake_when_every_unnotified_is_future(precheck, tmp_path):
    """The #2 repro minus its one due title: the 4 far-future shows, all
    beyond the lead → no wake. (`I Will Find You`, 6 days out, is the
    fifth repro entry and is covered by the wake test below.)"""
    path = _write(
        tmp_path,
        {
            "tracking": [
                {"title": "MobLand Season 2", "notified": False, "expected": f"{_YEAR}-Q4"},
                {"title": "Unforgotten Season 7", "notified": False, "expected": f"{_YEAR}-Q4"},
                {"title": "Slow Horses Season 6", "notified": False, "expected": f"{_YEAR}-Q3"},
                {
                    "title": "The Day of the Jackal S2",
                    "notified": False,
                    "expected": f"{_NEXT_YEAR}",
                },
            ]
        },
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "all_future"
    assert result["data"]["unnotified_count"] == 4
    # Soonest window is Slow Horses' Q3 (2026-07-01).
    assert result["data"]["nearest_window"] == _after(19)


def test_decide_wakes_when_iso_date_within_lead(precheck, tmp_path):
    """`I Will Find You` is 6 days out (2026-06-18) on the repro date."""
    path = _write(
        tmp_path,
        {
            "tracking": [
                {"title": "MobLand Season 2", "notified": False, "expected": f"{_YEAR}-Q4"},
                {"title": "I Will Find You", "notified": False, "expected": _after(6)},
            ]
        },
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "release_due"
    assert result["data"]["due_count"] == 1
    assert result["data"]["titles"] == ["I Will Find You"]
    assert result["data"]["lead_days"] == 7


def test_decide_no_wake_when_iso_date_beyond_lead(precheck, tmp_path):
    """One day past the cutoff (2026-06-20 > 2026-06-19) stays asleep."""
    path = _write(
        tmp_path,
        {"tracking": [{"title": "Edge", "notified": False, "expected": _after(8)}]},
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "all_future"


def test_decide_wakes_on_cutoff_boundary(precheck, tmp_path):
    """A window landing exactly on the cutoff (now + lead) wakes."""
    path = _write(
        tmp_path,
        {"tracking": [{"title": "Boundary", "notified": False, "expected": _after(7)}]},
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is True
    assert result["data"]["titles"] == ["Boundary"]


def test_decide_wakes_on_already_passed_window(precheck, tmp_path):
    """A window already in the past (overdue release) still wakes."""
    path = _write(
        tmp_path,
        {"tracking": [{"title": "Overdue", "notified": False, "expected": "2026-01-01"}]},
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is True
    assert result["data"]["due_count"] == 1


def test_decide_wakes_on_current_bare_year(precheck, tmp_path):
    """A bare current year anchors to Jan 1, already passed → wake."""
    path = _write(
        tmp_path,
        {"tracking": [{"title": "Sometime 2026", "notified": False, "expected": "2026"}]},
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "release_due"


def test_decide_no_wake_on_far_bare_year(precheck, tmp_path):
    path = _write(
        tmp_path,
        {"tracking": [{"title": "Way Out", "notified": False, "expected": f"{_NEXT_YEAR}"}]},
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "all_future"
    assert result["data"]["nearest_window"] == f"{_NEXT_YEAR}-01-01"


def test_decide_wakes_conservatively_on_unparseable_expected(precheck, tmp_path):
    """Un-parseable `expected` → can't prove it's future → wake."""
    path = _write(
        tmp_path,
        {"tracking": [{"title": "Mystery", "notified": False, "expected": "TBA"}]},
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "release_due"
    assert result["data"]["titles"] == ["Mystery"]


def test_decide_wakes_conservatively_when_expected_missing(precheck, tmp_path):
    """No `expected` field at all → conservative wake (can't date-gate)."""
    path = _write(
        tmp_path,
        {"tracking": [{"title": "No Date", "notified": False}]},
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "release_due"
    assert result["data"]["due_count"] == 1


def test_decide_counts_due_entry_without_title(precheck, tmp_path):
    """A titleless due entry still wakes; `titles` just omits it."""
    path = _write(
        tmp_path,
        {"tracking": [{"notified": False, "expected": _after(6)}]},
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is True
    assert result["data"]["due_count"] == 1
    assert result["data"]["titles"] == []


def test_decide_reports_only_due_titles_in_mixed_set(precheck, tmp_path):
    path = _write(
        tmp_path,
        {
            "tracking": [
                {"title": "Future Q4", "notified": False, "expected": f"{_YEAR}-Q4"},
                {"title": "Due Soon", "notified": False, "expected": _after(6)},
                {"title": "Already Sent", "notified": True, "expected": _after(6)},
            ]
        },
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is True
    assert result["data"]["due_count"] == 1
    assert result["data"]["titles"] == ["Due Soon"]


def test_decide_skips_non_dict_entries(precheck, tmp_path):
    """Robustness: a string or null entry inside `tracking` shouldn't crash."""
    path = _write(
        tmp_path,
        {
            "tracking": [
                "not-a-dict",
                None,
                {"title": "Real Show", "notified": False, "expected": _after(6)},
            ]
        },
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is True
    assert result["data"]["due_count"] == 1
    assert result["data"]["titles"] == ["Real Show"]


def test_decide_no_wake_on_unreadable_directory(precheck, tmp_path):
    """`Path.exists()` returns False on permission-denied. Direct read +
    PermissionError catch ensures we route to file_unreadable, preserving
    the diagnostic instead of silently mis-routing to file_missing."""
    locked_dir = tmp_path / "locked"
    locked_dir.mkdir()
    path = locked_dir / "watchlist.json"
    path.write_text(json.dumps({"tracking": [{"title": "X", "notified": False}]}))
    os.chmod(locked_dir, 0o000)
    try:
        result = precheck.decide(NOW, path)
        assert result["wake_agent"] is False
        assert result["data"]["reason"] == "file_unreadable"
    finally:
        os.chmod(locked_dir, 0o700)


# ---------------------------------------------------------------------------
# decide() — recheck backoff (the jbaruch/nanoclaw-media#67 fix)
# ---------------------------------------------------------------------------


def test_decide_no_wake_when_due_entry_is_inside_backoff(precheck, tmp_path):
    """A bare year anchors to Jan 1 and stays due all year. Once a run
    has resolved it, re-asking is rate-limited to the 30-day bare-year
    interval instead of firing nightly."""
    path = _write(
        tmp_path,
        {
            "tracking": [
                {
                    "title": "Sometime 2026",
                    "notified": False,
                    "expected": "2026",
                    "last_checked": _before(2),
                    "last_verdict": "unreleased",
                }
            ]
        },
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "within_recheck_backoff"
    assert result["data"]["backoff_count"] == 1
    assert result["data"]["next_recheck"] == _after(28)


def test_decide_wakes_once_the_backoff_elapses(precheck, tmp_path):
    """42 days since the last resolution is past the 30-day bare-year
    interval — the window is still open, so ask again."""
    path = _write(
        tmp_path,
        {
            "tracking": [
                {
                    "title": "Sometime 2026",
                    "notified": False,
                    "expected": "2026",
                    "last_checked": _before(42),
                    "last_verdict": "unreleased",
                }
            ]
        },
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "release_due"


def test_decide_rechecks_a_dated_entry_every_fire(precheck, tmp_path):
    """Day-precision `expected` gets a 1-day interval: yesterday's
    resolution never suppresses today's fire."""
    path = _write(
        tmp_path,
        {
            "tracking": [
                {
                    "title": "I Will Find You",
                    "notified": False,
                    "expected": _after(6),
                    "last_checked": _before(1),
                    "last_verdict": "unreleased",
                }
            ]
        },
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is True
    assert result["data"]["titles"] == ["I Will Find You"]


def test_decide_never_suppresses_a_released_verdict(precheck, tmp_path):
    """`notified` is still false, so the alert has not been delivered —
    the backoff must not sit on it."""
    path = _write(
        tmp_path,
        {
            "tracking": [
                {
                    "title": "Out Now",
                    "notified": False,
                    "expected": "2026",
                    "last_checked": _before(1),
                    "last_verdict": "released",
                }
            ]
        },
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is True
    assert result["data"]["titles"] == ["Out Now"]


@pytest.mark.parametrize(
    "entry_extra",
    [
        {"last_checked": _before(1)},  # no verdict recorded
        {"last_checked": "yesterday", "last_verdict": "unreleased"},  # unparseable stamp
        {"last_checked": 20260611, "last_verdict": "unreleased"},  # wrong type
        {"last_verdict": "unreleased"},  # verdict without a date
    ],
)
def test_decide_wakes_when_the_stamp_is_unusable(precheck, tmp_path, entry_extra):
    """Only a complete, parseable stamp suppresses — anything else falls
    back to the conservative wake."""
    path = _write(
        tmp_path,
        {
            "tracking": [
                {"title": "Half Stamped", "notified": False, "expected": "2026", **entry_extra}
            ]
        },
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "release_due"


def test_decide_reports_only_unsuppressed_titles(precheck, tmp_path):
    path = _write(
        tmp_path,
        {
            "tracking": [
                {
                    "title": "Backed Off",
                    "notified": False,
                    "expected": "2026",
                    "last_checked": _before(1),
                    "last_verdict": "unknown",
                },
                {"title": "Never Checked", "notified": False, "expected": "2026"},
            ]
        },
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is True
    assert result["data"]["due_count"] == 1
    assert result["data"]["titles"] == ["Never Checked"]


def test_decide_no_wake_on_the_issue_67_nightly_loop(precheck, tmp_path):
    """The #67 repro: the four entries that flagged `release_due` every
    single night. The three bare years anchor to Jan 1 and stay in the
    window all year — they are now held by the backoff after last
    night's run resolved them. `Fauda S5`'s `2026-10` used to be
    un-parseable (conservative wake); it now parses and date-gates out
    on the window alone, before the backoff is consulted."""
    path = _write(
        tmp_path,
        {
            "tracking": [
                {
                    "title": "Black Doves S2",
                    "notified": False,
                    "expected": "2026",
                    "last_checked": _before(1),
                    "last_verdict": "unreleased",
                },
                {
                    "title": "Severance S3",
                    "notified": False,
                    "expected": "2026",
                    "last_checked": _before(1),
                    "last_verdict": "unreleased",
                },
                {
                    "title": "Presumed Innocent S2",
                    "notified": False,
                    "expected": "2026",
                    "last_checked": _before(1),
                    "last_verdict": "unknown",
                },
                {
                    "title": "Fauda S5",
                    "notified": False,
                    "expected": f"{_YEAR}-10",
                    "last_checked": _before(1),
                    "last_verdict": "unreleased",
                },
            ]
        },
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is False
    assert result["data"]["unnotified_count"] == 4
    assert result["data"]["reason"] == "within_recheck_backoff"
    assert result["data"]["backoff_count"] == 3
    assert result["data"]["next_recheck"] == _after(29)
    assert result["data"]["nearest_window"] == f"{_YEAR}-10-01"


def test_decide_reports_both_backoff_and_future_windows(precheck, tmp_path):
    path = _write(
        tmp_path,
        {
            "tracking": [
                {
                    "title": "Backed Off",
                    "notified": False,
                    "expected": "2026",
                    "last_checked": _before(1),
                    "last_verdict": "unreleased",
                },
                {"title": "Far Future", "notified": False, "expected": f"{_NEXT_YEAR}"},
            ]
        },
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "within_recheck_backoff"
    assert result["data"]["next_recheck"] == _after(29)
    assert result["data"]["nearest_window"] == f"{_NEXT_YEAR}-01-01"


def test_decide_ignores_stamps_on_far_future_entries(precheck, tmp_path):
    """A window beyond the lead is filtered before the backoff ever
    applies — the reason stays `all_future`."""
    path = _write(
        tmp_path,
        {
            "tracking": [
                {
                    "title": "Far Future",
                    "notified": False,
                    "expected": f"{_NEXT_YEAR}",
                    "last_checked": _before(1),
                    "last_verdict": "unreleased",
                }
            ]
        },
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "all_future"
    assert result["data"]["nearest_window"] == f"{_NEXT_YEAR}-01-01"


@pytest.mark.parametrize("version", [2, 99, 0, -1])
def test_decide_ignores_backoff_stamps_from_an_unsupported_schema(precheck, tmp_path, version):
    """Only the documented legacy case (no stamp) and the supported
    version qualify — a newer stamp belongs to a writer this reader
    doesn't know, an older one to a shape it was never written against.
    Both are no usable prior state (`coding-policy: stateful-artifacts`):
    the versioned fields go uninterpreted and date-gating decides, which
    wakes. A reader must never suppress an alert on the strength of
    fields it cannot interpret."""
    path = _write(
        tmp_path,
        {
            "schema_version": version,
            "tracking": [
                {
                    "title": "Sometime 2026",
                    "notified": False,
                    "expected": "2026",
                    "last_checked": _before(1),
                    "last_verdict": "unreleased",
                }
            ],
        },
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "release_due"
    assert result["data"]["prior_state"] == "unreadable_schema_version"


@pytest.mark.parametrize("version", [None, 1])
def test_decide_reads_stamps_at_or_below_the_supported_version(precheck, tmp_path, version):
    """An absent stamp is legacy pre-v1 with the same shape; v1 is this
    reader's own version. Both back off normally."""
    payload = {
        "tracking": [
            {
                "title": "Sometime 2026",
                "notified": False,
                "expected": "2026",
                "last_checked": _before(1),
                "last_verdict": "unreleased",
            }
        ]
    }
    if version is not None:
        payload["schema_version"] = version
    result = precheck.decide(NOW, _write(tmp_path, payload))
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "within_recheck_backoff"


@pytest.mark.parametrize("version", ["1", 1.5, True, [1]])
def test_decide_treats_a_malformed_schema_version_as_unreadable(precheck, tmp_path, version):
    """Anything that isn't a plain int is a stamp this reader can't
    reason about — take the waking fallback, not the suppressing one."""
    path = _write(
        tmp_path,
        {
            "schema_version": version,
            "tracking": [
                {
                    "title": "Sometime 2026",
                    "notified": False,
                    "expected": "2026",
                    "last_checked": _before(1),
                    "last_verdict": "unreleased",
                }
            ],
        },
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is True
    assert result["data"]["prior_state"] == "unreadable_schema_version"


def test_decide_omits_the_prior_state_marker_when_stamps_are_readable(precheck, tmp_path):
    path = _write(
        tmp_path,
        {
            "schema_version": 1,
            "tracking": [{"title": "Fresh", "notified": False, "expected": "2026"}],
        },
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is True
    assert "prior_state" not in result["data"]


def test_decide_still_date_gates_under_a_newer_schema(precheck, tmp_path):
    """The unreadable-version fallback is date-gating, not blanket
    waking — a far-future window stays asleep."""
    path = _write(
        tmp_path,
        {
            "schema_version": 2,
            "tracking": [{"title": "Far Future", "notified": False, "expected": f"{_NEXT_YEAR}"}],
        },
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "all_future"


# ---------------------------------------------------------------------------
# main() — JSON-on-stdout-and-exit-0 contract
# ---------------------------------------------------------------------------


def test_main_emits_json_and_exits_zero_when_file_missing(tmp_path):
    env = {**os.environ, "CHECK_WATCHLIST_PATH": str(tmp_path / "missing.json")}
    proc = subprocess.run(
        ["python3", str(REPO_ROOT / SCRIPT_REL)],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["wake_agent"] is False
    assert payload["data"]["reason"] == "file_missing"


def test_main_emits_json_when_release_due(tmp_path):
    """Un-parseable `expected` → conservative wake, deterministic regardless
    of the real wall clock main() reads."""
    path = tmp_path / "watchlist.json"
    path.write_text(
        json.dumps({"tracking": [{"title": "Legends", "notified": False, "expected": "TBA"}]})
    )
    env = {**os.environ, "CHECK_WATCHLIST_PATH": str(path)}
    proc = subprocess.run(
        ["python3", str(REPO_ROOT / SCRIPT_REL)],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["wake_agent"] is True
    assert payload["data"]["reason"] == "release_due"
    assert payload["data"]["due_count"] == 1
    assert payload["data"]["titles"] == ["Legends"]


def test_main_emits_json_even_when_corrupt(tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text("garbage")
    env = {**os.environ, "CHECK_WATCHLIST_PATH": str(path)}
    proc = subprocess.run(
        ["python3", str(REPO_ROOT / SCRIPT_REL)],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["wake_agent"] is False
    assert payload["data"]["reason"] == "file_unreadable"
