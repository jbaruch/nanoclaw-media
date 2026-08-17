"""Tests for skills/check-watchlist/scripts/mark-entry.py.

Locks down the documented contract per `coding-policy: testing-standards`:

  - `--released` sets `notified: true` + `released`; `--cancelled` sets
    `notified: true` + `cancelled: true`; `--clear-stamps` drops
    `last_checked`/`last_verdict` and leaves `notified` false.
  - Exactly one mutation per invocation; the three are mutually
    exclusive and one is required.
  - Entry matching is exact on the casefolded, whitespace-collapsed
    title — the same rule verify-release.py resolves titles by. No
    match, or more than one, is an error rather than a guess.
  - Version handling follows the owner contract: an unstamped record is
    stamped on write, `WATCHLIST_SCHEMA_VERSION` is preserved, any
    other version is refused with the file untouched.
  - Idempotent: re-marking an already-marked entry reports
    `already_marked`, exits 0, and does not rewrite the file.
  - Every failure exits 1 with an actionable `error` on stdout and
    leaves the file untouched — exit 1 means the mutation did NOT land,
    which is what lets the skill stop instead of delivering shows it
    cannot record.
"""

from __future__ import annotations

import json
import os

import pytest


def _entry(title, **extra):
    entry = {"title": title, "notified": False, "expected": "2026", "platform": "Netflix"}
    entry.update(extra)
    return entry


def _write(tmp_path, payload):
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run(module, monkeypatch, capsys, path, argv):
    monkeypatch.setenv("CHECK_WATCHLIST_PATH", str(path))
    code = module.main(argv)
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


# ---------------------------------------------------------------------------
# The three mutations
# ---------------------------------------------------------------------------


def test_released_marks_notified_and_date(mark_entry, monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, {"schema_version": 1, "tracking": [_entry("Black Doves")]})
    code, out = _run(
        mark_entry,
        monkeypatch,
        capsys,
        path,
        ["--title", "Black Doves", "--released", "2026-08-01"],
    )
    assert code == 0
    assert out["status"] == "marked"
    written = json.loads(path.read_text())["tracking"][0]
    assert written["notified"] is True
    assert written["released"] == "2026-08-01"


def test_cancelled_marks_notified_and_flag(mark_entry, monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, {"schema_version": 1, "tracking": [_entry("Gone Show")]})
    code, out = _run(mark_entry, monkeypatch, capsys, path, ["--title", "Gone Show", "--cancelled"])
    assert code == 0
    assert out["status"] == "marked"
    written = json.loads(path.read_text())["tracking"][0]
    assert written["notified"] is True
    assert written["cancelled"] is True
    assert "released" not in written


def test_clear_stamps_drops_both_and_keeps_notified_false(
    mark_entry, monkeypatch, capsys, tmp_path
):
    """The failed-send rollback: without this the precheck's backoff
    suppresses the retry of an alert that was never delivered."""
    path = _write(
        tmp_path,
        {
            "schema_version": 1,
            "tracking": [_entry("Black Doves", last_checked="2026-08-17", last_verdict="unknown")],
        },
    )
    code, out = _run(
        mark_entry, monkeypatch, capsys, path, ["--title", "Black Doves", "--clear-stamps"]
    )
    assert code == 0
    assert out["status"] == "stamps_cleared"
    written = json.loads(path.read_text())["tracking"][0]
    assert "last_checked" not in written
    assert "last_verdict" not in written
    assert written["notified"] is False


def test_clear_stamps_on_an_unstamped_entry_is_a_no_op(mark_entry, monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, {"schema_version": 1, "tracking": [_entry("Black Doves")]})
    before = path.read_text()
    code, out = _run(
        mark_entry, monkeypatch, capsys, path, ["--title", "Black Doves", "--clear-stamps"]
    )
    assert code == 0
    assert out["status"] == "stamps_absent"
    assert path.read_text() == before


def test_only_the_named_entry_changes(mark_entry, monkeypatch, capsys, tmp_path):
    path = _write(
        tmp_path,
        {
            "schema_version": 1,
            "tracking": [_entry("First"), _entry("Second"), _entry("Third")],
        },
    )
    _run(mark_entry, monkeypatch, capsys, path, ["--title", "Second", "--released", "2026-08-01"])
    tracking = json.loads(path.read_text())["tracking"]
    assert [e["notified"] for e in tracking] == [False, True, False]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_remarking_the_same_release_is_idempotent(mark_entry, monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, {"schema_version": 1, "tracking": [_entry("Black Doves")]})
    argv = ["--title", "Black Doves", "--released", "2026-08-01"]
    _run(mark_entry, monkeypatch, capsys, path, argv)
    after_first = path.read_text()
    code, out = _run(mark_entry, monkeypatch, capsys, path, argv)
    assert code == 0
    assert out["status"] == "already_marked"
    assert path.read_text() == after_first


def test_remarking_a_cancellation_is_idempotent(mark_entry, monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, {"schema_version": 1, "tracking": [_entry("Gone Show")]})
    argv = ["--title", "Gone Show", "--cancelled"]
    _run(mark_entry, monkeypatch, capsys, path, argv)
    after_first = path.read_text()
    code, out = _run(mark_entry, monkeypatch, capsys, path, argv)
    assert out["status"] == "already_marked"
    assert path.read_text() == after_first


def test_a_different_release_date_overwrites(mark_entry, monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, {"schema_version": 1, "tracking": [_entry("Black Doves")]})
    _run(
        mark_entry,
        monkeypatch,
        capsys,
        path,
        ["--title", "Black Doves", "--released", "2026-08-01"],
    )
    code, out = _run(
        mark_entry,
        monkeypatch,
        capsys,
        path,
        ["--title", "Black Doves", "--released", "2026-08-02"],
    )
    assert code == 0
    assert out["status"] == "marked"
    assert json.loads(path.read_text())["tracking"][0]["released"] == "2026-08-02"


# ---------------------------------------------------------------------------
# Title matching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("given", ["black doves", "  Black   Doves  ", "BLACK DOVES"])
def test_title_match_is_normalized(mark_entry, monkeypatch, capsys, tmp_path, given):
    path = _write(tmp_path, {"schema_version": 1, "tracking": [_entry("Black Doves")]})
    code, _ = _run(mark_entry, monkeypatch, capsys, path, ["--title", given, "--cancelled"])
    assert code == 0
    assert json.loads(path.read_text())["tracking"][0]["notified"] is True


def test_no_match_is_an_error_not_a_guess(mark_entry, monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, {"schema_version": 1, "tracking": [_entry("Black Doves")]})
    before = path.read_text()
    code, out = _run(mark_entry, monkeypatch, capsys, path, ["--title", "Blak Dovs", "--cancelled"])
    assert code == 1
    assert "no watchlist entry titled" in out["error"]
    assert path.read_text() == before


def test_an_ambiguous_title_is_an_error(mark_entry, monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, {"schema_version": 1, "tracking": [_entry("Twin"), _entry("  twin ")]})
    before = path.read_text()
    code, out = _run(mark_entry, monkeypatch, capsys, path, ["--title", "Twin", "--cancelled"])
    assert code == 1
    assert "de-duplicate" in out["error"]
    assert path.read_text() == before


def test_malformed_entries_do_not_crash_the_match(mark_entry, monkeypatch, capsys, tmp_path):
    path = _write(
        tmp_path,
        {"schema_version": 1, "tracking": ["nope", None, {"title": None}, _entry("Real")]},
    )
    code, _ = _run(mark_entry, monkeypatch, capsys, path, ["--title", "Real", "--cancelled"])
    assert code == 0


# ---------------------------------------------------------------------------
# Version handling
# ---------------------------------------------------------------------------


def test_a_legacy_record_is_stamped_on_write(mark_entry, monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, {"tracking": [_entry("Black Doves")]})
    code, _ = _run(
        mark_entry,
        monkeypatch,
        capsys,
        path,
        ["--title", "Black Doves", "--released", "2026-08-01"],
    )
    assert code == 0
    assert json.loads(path.read_text())["schema_version"] == 1


@pytest.mark.parametrize("version", [2, 0, -1, "1", 1.5, True])
def test_an_unsupported_version_is_refused_untouched(
    mark_entry, monkeypatch, capsys, tmp_path, version
):
    path = _write(tmp_path, {"schema_version": version, "tracking": [_entry("Black Doves")]})
    before = path.read_text()
    code, out = _run(
        mark_entry,
        monkeypatch,
        capsys,
        path,
        ["--title", "Black Doves", "--released", "2026-08-01"],
    )
    assert code == 1
    assert "does not implement that shape" in out["error"]
    assert path.read_text() == before


# ---------------------------------------------------------------------------
# Failure paths — exit 1 always means the mutation did not land
# ---------------------------------------------------------------------------


def test_missing_file_is_an_actionable_error(mark_entry, monkeypatch, capsys, tmp_path):
    code, out = _run(
        mark_entry,
        monkeypatch,
        capsys,
        tmp_path / "missing.json",
        ["--title", "Black Doves", "--cancelled"],
    )
    assert code == 1
    assert "does not exist" in out["error"]
    assert "watchlist mount" in out["error"]


def test_malformed_json_is_an_actionable_error(mark_entry, monkeypatch, capsys, tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text("{not json", encoding="utf-8")
    code, out = _run(mark_entry, monkeypatch, capsys, path, ["--title", "X", "--cancelled"])
    assert code == 1
    assert "repair or restore valid JSON" in out["error"]


def test_a_non_object_root_is_an_error(mark_entry, monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, [_entry("Black Doves")])
    code, out = _run(
        mark_entry, monkeypatch, capsys, path, ["--title", "Black Doves", "--cancelled"]
    )
    assert code == 1
    assert "root is not a JSON object" in out["error"]


def test_missing_tracking_list_is_an_error(mark_entry, monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, {"schema_version": 1})
    code, out = _run(mark_entry, monkeypatch, capsys, path, ["--title", "X", "--cancelled"])
    assert code == 1
    assert "no `tracking` list" in out["error"]


def test_an_unwritable_file_reports_that_nothing_landed(mark_entry, monkeypatch, capsys, tmp_path):
    """The skill stops on a non-zero exit, so the message has to say the
    mutation did not happen rather than leaving it ambiguous."""
    locked = tmp_path / "locked"
    locked.mkdir()
    path = locked / "watchlist.json"
    path.write_text(
        json.dumps({"schema_version": 1, "tracking": [_entry("Black Doves")]}), encoding="utf-8"
    )
    os.chmod(locked, 0o500)
    try:
        code, out = _run(
            mark_entry,
            monkeypatch,
            capsys,
            path,
            ["--title", "Black Doves", "--released", "2026-08-01"],
        )
    finally:
        os.chmod(locked, 0o700)
    assert code == 1
    assert "did NOT land" in out["error"]
    assert json.loads(path.read_text())["tracking"][0]["notified"] is False


# ---------------------------------------------------------------------------
# Argument contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--title", "X"],  # no action
        ["--released", "2026-08-01"],  # no title
        ["--title", "X", "--cancelled", "--clear-stamps"],  # two actions
        ["--title", "X", "--released", "2026-08-01", "--cancelled"],
    ],
)
def test_invalid_argument_combinations_are_rejected(mark_entry, argv):
    with pytest.raises(SystemExit) as exit_info:
        mark_entry.main(argv)
    assert exit_info.value.code == 2
