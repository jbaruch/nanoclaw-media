"""Tests for skills/entertainment-sync/scripts/precheck-entertainment-sync.py."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_REL = "skills/entertainment-sync/scripts/precheck-entertainment-sync.py"


@pytest.fixture
def precheck():
    spec = importlib.util.spec_from_file_location(
        "precheck_entertainment_sync_under_test", REPO_ROOT / SCRIPT_REL
    )
    assert spec is not None and spec.loader is not None, "cannot load module spec"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_decide_wakes_when_cursor_absent(precheck, tmp_path):
    cursor = tmp_path / "missing.json"
    now = datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)
    result = precheck.decide(now, cursor)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "no_cursor"


def test_decide_no_wake_within_cadence(precheck, tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"schema_version": 1, "last_run": "2026-04-30T03:00:00Z"}))
    now = datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)
    result = precheck.decide(now, cursor)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "within_cadence"


def test_decide_wakes_when_cadence_elapsed(precheck, tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"schema_version": 1, "last_run": "2026-04-23T03:00:00Z"}))
    now = datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)
    result = precheck.decide(now, cursor)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "cadence_elapsed"


def test_decide_wakes_at_weekly_near_miss(precheck, tmp_path):
    # nanoclaw-admin#353: the cursor stamps at run completion, so the next
    # same-time weekly fire lands a few minutes short of 168h (~167.8h here).
    # With the cap below the cron interval this MUST wake; a 168h cap skipped
    # forever. Guards against the cap regressing back to the weekly interval.
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"schema_version": 1, "last_run": "2026-04-25T03:12:00Z"}))
    now = datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)
    result = precheck.decide(now, cursor)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "cadence_elapsed"


def test_decide_wakes_on_future_timestamp(precheck, tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"schema_version": 1, "last_run": "2030-01-01T00:00:00Z"}))
    now = datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)
    result = precheck.decide(now, cursor)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "cursor_future"


def test_decide_wakes_on_unparseable_iso(precheck, tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"schema_version": 1, "last_run": "garbage"}))
    now = datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)
    result = precheck.decide(now, cursor)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "cursor_unparseable"


def test_decide_wakes_on_naive_datetime(precheck, tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"schema_version": 1, "last_run": "2026-04-30T03:00:00"}))
    now = datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)
    result = precheck.decide(now, cursor)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "cursor_naive_datetime"


def test_decide_wakes_on_unsupported_schema(precheck, tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"schema_version": 99, "last_run": "2026-04-30T03:00:00Z"}))
    now = datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)
    result = precheck.decide(now, cursor)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "cursor_error"


def test_decide_wakes_on_malformed_json(precheck, tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text("{not valid")
    now = datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)
    result = precheck.decide(now, cursor)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "cursor_error"


def test_decide_wakes_when_last_run_missing(precheck, tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"schema_version": 1}))
    now = datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)
    result = precheck.decide(now, cursor)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "cursor_error"


def test_decide_cursor_permission_denied_fails_open(precheck, tmp_path):
    locked_dir = tmp_path / "locked"
    locked_dir.mkdir()
    cursor = locked_dir / "cursor.json"
    cursor.write_text(json.dumps({"schema_version": 1, "last_run": "2026-04-30T03:00:00Z"}))
    os.chmod(locked_dir, 0o000)
    try:
        now = datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)
        result = precheck.decide(now, cursor)
        assert result["wake_agent"] is True
        assert result["data"]["reason"] == "cursor_error"
    finally:
        os.chmod(locked_dir, 0o700)


def test_main_emits_json_and_exits_zero_on_no_cursor(tmp_path):
    cursor = tmp_path / "cursor.json"
    env = {**os.environ, "ENTERTAINMENT_SYNC_CURSOR": str(cursor)}
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
    assert payload["data"]["reason"] == "no_cursor"
