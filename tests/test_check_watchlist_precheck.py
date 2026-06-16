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
  - Fuzzy `expected` parsing: ISO date / `YYYY-Qn` / bare `YYYY`.
  - main() always exits 0 with valid JSON on stdout.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_REL = "skills/check-watchlist/scripts/check-watchlist-precheck.py"

# Frozen clock matching the jbaruch/nanoclaw-media#2 repro (verified
# 2026-06-12). LEAD is 7 days, so the cutoff is 2026-06-19.
NOW = datetime(2026, 6, 12, tzinfo=timezone.utc)


@pytest.fixture
def precheck():
    spec = importlib.util.spec_from_file_location(
        "check_watchlist_precheck_under_test", REPO_ROOT / SCRIPT_REL
    )
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
        ("2026-06-18", "2026-06-18"),
        ("2026-Q1", "2026-01-01"),
        ("2026-Q2", "2026-04-01"),
        ("2026-Q3", "2026-07-01"),
        ("2026-Q4", "2026-10-01"),
        ("2026-q4", "2026-10-01"),  # lowercase tolerated
        ("  2027  ", "2027-01-01"),  # whitespace tolerated
        ("2027", "2027-01-01"),
    ],
)
def test_window_start_parses_fuzzy_formats(precheck, value, expected_iso):
    assert precheck._window_start(value).isoformat() == expected_iso


@pytest.mark.parametrize("value", ["TBA", "summer 2026", "", None, 2026, "2026-13-40"])
def test_window_start_returns_none_for_unparseable(precheck, value):
    assert precheck._window_start(value) is None


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
                {"title": "MobLand Season 2", "notified": False, "expected": "2026-Q4"},
                {"title": "Unforgotten Season 7", "notified": False, "expected": "2026-Q4"},
                {"title": "Slow Horses Season 6", "notified": False, "expected": "2026-Q3"},
                {"title": "The Day of the Jackal S2", "notified": False, "expected": "2027"},
            ]
        },
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "all_future"
    assert result["data"]["unnotified_count"] == 4
    # Soonest window is Slow Horses' Q3 (2026-07-01).
    assert result["data"]["nearest_window"] == "2026-07-01"


def test_decide_wakes_when_iso_date_within_lead(precheck, tmp_path):
    """`I Will Find You` is 6 days out (2026-06-18) on the repro date."""
    path = _write(
        tmp_path,
        {
            "tracking": [
                {"title": "MobLand Season 2", "notified": False, "expected": "2026-Q4"},
                {"title": "I Will Find You", "notified": False, "expected": "2026-06-18"},
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
        {"tracking": [{"title": "Edge", "notified": False, "expected": "2026-06-20"}]},
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "all_future"


def test_decide_wakes_on_cutoff_boundary(precheck, tmp_path):
    """A window landing exactly on the cutoff (now + lead) wakes."""
    path = _write(
        tmp_path,
        {"tracking": [{"title": "Boundary", "notified": False, "expected": "2026-06-19"}]},
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
        {"tracking": [{"title": "Way Out", "notified": False, "expected": "2027"}]},
    )
    result = precheck.decide(NOW, path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "all_future"
    assert result["data"]["nearest_window"] == "2027-01-01"


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
        {"tracking": [{"notified": False, "expected": "2026-06-18"}]},
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
                {"title": "Future Q4", "notified": False, "expected": "2026-Q4"},
                {"title": "Due Soon", "notified": False, "expected": "2026-06-18"},
                {"title": "Already Sent", "notified": True, "expected": "2026-06-18"},
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
                {"title": "Real Show", "notified": False, "expected": "2026-06-18"},
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
