"""Tests for skills/check-watchlist/scripts/check-watchlist-precheck.py.

Locks down the documented contract:

  - File missing → no-wake (file_missing).
  - File unreadable / malformed JSON / non-UTF-8 → no-wake
    (file_unreadable). Per jbaruch/nanoclaw#516, the agent has nothing
    useful to do with a broken file; silent skip is safe.
  - JSON valid but `tracking` not a list → no-wake (tracking_missing).
  - Every entry has `notified: true` → no-wake (all_notified).
  - At least one entry has `notified: false` → wake (unnotified_present)
    with `unnotified_count` and `titles` so the agent's first turn
    doesn't re-read the file.
  - main() always exits 0 with valid JSON on stdout.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_REL = "skills/check-watchlist/scripts/check-watchlist-precheck.py"


@pytest.fixture
def precheck():
    spec = importlib.util.spec_from_file_location(
        "check_watchlist_precheck_under_test", REPO_ROOT / SCRIPT_REL
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# decide() — pure decision function
# ---------------------------------------------------------------------------


def test_decide_no_wake_when_file_missing(precheck, tmp_path):
    result = precheck.decide(tmp_path / "missing.json")
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "file_missing"


def test_decide_no_wake_on_malformed_json(precheck, tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text("{not valid json")
    result = precheck.decide(path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "file_unreadable"
    assert "JSON malformed" in result["data"]["error"]


def test_decide_no_wake_on_non_utf8(precheck, tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_bytes(b"\xff\xfe\x00not-valid-utf-8")
    result = precheck.decide(path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "file_unreadable"


def test_decide_no_wake_on_empty_file(precheck, tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text("")
    result = precheck.decide(path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "tracking_missing"


def test_decide_no_wake_when_tracking_missing(precheck, tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps({"other_key": []}))
    result = precheck.decide(path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "tracking_missing"


def test_decide_no_wake_when_root_is_list(precheck, tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps([{"title": "a", "notified": False}]))
    result = precheck.decide(path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "tracking_missing"


def test_decide_no_wake_when_tracking_empty(precheck, tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps({"tracking": []}))
    result = precheck.decide(path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "all_notified"
    assert result["data"]["tracking_count"] == 0


def test_decide_no_wake_when_all_notified(precheck, tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text(
        json.dumps(
            {
                "tracking": [
                    {"title": "Show A", "notified": True},
                    {"title": "Show B", "notified": True},
                ]
            }
        )
    )
    result = precheck.decide(path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "all_notified"
    assert result["data"]["tracking_count"] == 2


def test_decide_wakes_when_one_unnotified(precheck, tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text(
        json.dumps(
            {
                "tracking": [
                    {"title": "Show A", "notified": True},
                    {"title": "Show B", "notified": False},
                    {"title": "Show C", "notified": True},
                ]
            }
        )
    )
    result = precheck.decide(path)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "unnotified_present"
    assert result["data"]["unnotified_count"] == 1
    assert result["data"]["titles"] == ["Show B"]


def test_decide_wakes_with_multiple_unnotified(precheck, tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text(
        json.dumps(
            {
                "tracking": [
                    {"title": "Legends", "notified": False},
                    {"title": "MobLand Season 2", "notified": False},
                    {"title": "Already Sent", "notified": True},
                ]
            }
        )
    )
    result = precheck.decide(path)
    assert result["wake_agent"] is True
    assert result["data"]["unnotified_count"] == 2
    assert set(result["data"]["titles"]) == {"Legends", "MobLand Season 2"}


def test_decide_skips_non_dict_entries(precheck, tmp_path):
    """Robustness: a string or null entry inside `tracking` shouldn't crash."""
    path = tmp_path / "watchlist.json"
    path.write_text(
        json.dumps(
            {
                "tracking": [
                    "not-a-dict",
                    None,
                    {"title": "Real Show", "notified": False},
                ]
            }
        )
    )
    result = precheck.decide(path)
    assert result["wake_agent"] is True
    assert result["data"]["unnotified_count"] == 1
    assert result["data"]["titles"] == ["Real Show"]


def test_decide_treats_missing_notified_as_already_handled(precheck, tmp_path):
    """`notified: false` is the only wake trigger — missing field stays silent."""
    path = tmp_path / "watchlist.json"
    path.write_text(
        json.dumps(
            {
                "tracking": [
                    {"title": "No Field", "expected": "2026"},
                    {"title": "Truthy", "notified": True},
                ]
            }
        )
    )
    result = precheck.decide(path)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "all_notified"


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
        result = precheck.decide(path)
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


def test_main_emits_json_when_unnotified(tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps({"tracking": [{"title": "Legends", "notified": False}]}))
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
    assert payload["data"]["unnotified_count"] == 1
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
