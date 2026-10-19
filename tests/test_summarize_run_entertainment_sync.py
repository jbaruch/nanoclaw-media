"""Tests for skills/entertainment-sync/scripts/summarize-run.py (#92 review)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills/entertainment-sync/scripts/summarize-run.py"


@pytest.fixture
def summarize_run() -> Any:
    spec = importlib.util.spec_from_file_location("summarize_run_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(module, capsys, *args: str) -> tuple[int, str, str]:
    code = module.main(["summarize-run.py", *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize(
    ("surfaced", "status"), [(False, "clean"), (True, "surfaced")], ids=["clean", "surfaced"]
)
def test_a_full_run_advances_the_cursor(summarize_run, capsys, surfaced, status):
    args = ["--date", "2026-10-18", "--failed", "none"] + (["--surfaced"] if surfaced else [])
    code, out, _ = _run(summarize_run, capsys, *args)
    assert code == 0
    assert json.loads(out) == {
        "advance_cursor": True,
        "internal": f"<internal>entertainment-sync ran 2026-10-18: {status}</internal>",
    }


def test_a_trakt_failure_still_reports_the_steps_that_ran(summarize_run, capsys):
    code, out, _ = _run(summarize_run, capsys, "--date", "2026-10-18", "--failed", "1")
    assert code == 0
    assert json.loads(out) == {
        "advance_cursor": False,
        "internal": "<internal>entertainment-sync ran 2026-10-18: "
        "partial — ran 2, 3, 4, 5; failed 1</internal>",
    }


def test_failed_steps_are_sorted_and_deduplicated(summarize_run, capsys):
    code, out, _ = _run(summarize_run, capsys, "--date", "2026-10-18", "--failed", "4,1,4")
    assert code == 0
    assert "partial — ran 2, 3, 5; failed 1, 4" in json.loads(out)["internal"]


@pytest.mark.parametrize(
    "args",
    [
        ["--date", "10/18/2026", "--failed", "none"],
        ["--date", "20261018", "--failed", "none"],
        ["--date", "2026-W42-7", "--failed", "none"],
        ["--date", "2026-13-40", "--failed", "none"],
        ["--date", "2026-10-18", "--failed", "6"],
    ],
    ids=["bad-date", "compact-date", "iso-week-date", "impossible-date", "unknown-step"],
)
def test_bad_input_exits_2(summarize_run, capsys, args):
    code, out, err = _run(summarize_run, capsys, *args)
    assert code == 2
    assert out == ""
    assert "summarize-run:" in err
