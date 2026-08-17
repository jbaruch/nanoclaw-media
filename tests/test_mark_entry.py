"""Tests for skills/check-watchlist/scripts/mark-entry.py.

Locks down the documented contract per `coding-policy: testing-standards`:

  - The request is a JSON file named by `--input`, never command-line
    text: a title carrying an apostrophe or a command substitution is
    ordinary data, not something a shell can act on.
  - `action: released` sets `notified: true` + `released`; `cancelled`
    sets `notified: true` + `cancelled: true`; `clear_stamps` drops
    `last_checked`/`last_verdict` and leaves `notified` false.
  - Exactly one mutation per invocation; an unknown or missing `action`,
    an unusable `title`, and a `released` action with no date are all
    refused with the file untouched.
  - Entry matching is exact on the casefolded, whitespace-collapsed
    title — the same rule verify-release.py resolves titles by. No
    match, or more than one, is an error rather than a guess.
  - Version handling follows the owner contract: an unstamped record is
    stamped on write, `WATCHLIST_SCHEMA_VERSION` is preserved, any
    other version is refused with the file untouched.
  - Idempotent: re-marking an already-marked entry reports
    `already_marked`, exits 0, and does not rewrite the file.
  - `released` must be a canonical `YYYY-MM-DD` date; a free-form
    string never reaches the record.
  - Every failure exits 1 with an actionable `error` on stdout, a
    diagnostic on stderr, and leaves the file untouched — exit 1 means the mutation did NOT land,
    which is what lets the skill stop instead of delivering shows it
    cannot record.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta

import pytest

# This script reads no clock — dates are pure data it format-validates
# and stores. Fixtures still come off one fixed reference so no literal
# reads as a date against the real clock.
_REF = date(2026, 8, 17)


def _before(days: int) -> str:
    return (_REF - timedelta(days=days)).isoformat()


def _entry(title, **extra):
    entry = {"title": title, "notified": False, "expected": "2026", "platform": "Netflix"}
    entry.update(extra)
    return entry


def _write(tmp_path, payload):
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run(module, monkeypatch, capsys, path, request, *, tmp_path=None):
    """`request` is the JSON object the skill writes; a raw string is
    passed through so malformed-input cases can be exercised."""
    monkeypatch.setenv("CHECK_WATCHLIST_PATH", str(path))
    request_path = (tmp_path or path.parent) / "mark-request.json"
    request_path.write_text(
        request if isinstance(request, str) else json.dumps(request), encoding="utf-8"
    )
    code = module.main(["--input", str(request_path)])
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
        {"title": "Black Doves", "action": "released", "released": _before(16)},
    )
    assert code == 0
    assert out["status"] == "marked"
    written = json.loads(path.read_text())["tracking"][0]
    assert written["notified"] is True
    assert written["released"] == _before(16)


def test_cancelled_marks_notified_and_flag(mark_entry, monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, {"schema_version": 1, "tracking": [_entry("Gone Show")]})
    code, out = _run(
        mark_entry, monkeypatch, capsys, path, {"title": "Gone Show", "action": "cancelled"}
    )
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
            "tracking": [
                _entry("Black Doves", last_checked=_REF.isoformat(), last_verdict="unknown")
            ],
        },
    )
    code, out = _run(
        mark_entry, monkeypatch, capsys, path, {"title": "Black Doves", "action": "clear_stamps"}
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
        mark_entry, monkeypatch, capsys, path, {"title": "Black Doves", "action": "clear_stamps"}
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
    _run(
        mark_entry,
        monkeypatch,
        capsys,
        path,
        {"title": "Second", "action": "released", "released": _before(16)},
    )
    tracking = json.loads(path.read_text())["tracking"]
    assert [e["notified"] for e in tracking] == [False, True, False]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_remarking_the_same_release_is_idempotent(mark_entry, monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, {"schema_version": 1, "tracking": [_entry("Black Doves")]})
    argv = {"title": "Black Doves", "action": "released", "released": _before(16)}
    _run(mark_entry, monkeypatch, capsys, path, argv)
    after_first = path.read_text()
    code, out = _run(mark_entry, monkeypatch, capsys, path, argv)
    assert code == 0
    assert out["status"] == "already_marked"
    assert path.read_text() == after_first


def test_remarking_a_cancellation_is_idempotent(mark_entry, monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, {"schema_version": 1, "tracking": [_entry("Gone Show")]})
    argv = {"title": "Gone Show", "action": "cancelled"}
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
        {"title": "Black Doves", "action": "released", "released": _before(16)},
    )
    code, out = _run(
        mark_entry,
        monkeypatch,
        capsys,
        path,
        {"title": "Black Doves", "action": "released", "released": _before(15)},
    )
    assert code == 0
    assert out["status"] == "marked"
    assert json.loads(path.read_text())["tracking"][0]["released"] == _before(15)


# ---------------------------------------------------------------------------
# Title matching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("given", ["black doves", "  Black   Doves  ", "BLACK DOVES"])
def test_title_match_is_normalized(mark_entry, monkeypatch, capsys, tmp_path, given):
    path = _write(tmp_path, {"schema_version": 1, "tracking": [_entry("Black Doves")]})
    code, _ = _run(mark_entry, monkeypatch, capsys, path, {"title": given, "action": "cancelled"})
    assert code == 0
    assert json.loads(path.read_text())["tracking"][0]["notified"] is True


def test_no_match_is_an_error_not_a_guess(mark_entry, monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, {"schema_version": 1, "tracking": [_entry("Black Doves")]})
    before = path.read_text()
    code, out = _run(
        mark_entry, monkeypatch, capsys, path, {"title": "Blak Dovs", "action": "cancelled"}
    )
    assert code == 1
    assert "no watchlist entry titled" in out["error"]
    assert path.read_text() == before


def test_an_ambiguous_title_is_an_error(mark_entry, monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, {"schema_version": 1, "tracking": [_entry("Twin"), _entry("  twin ")]})
    before = path.read_text()
    code, out = _run(
        mark_entry, monkeypatch, capsys, path, {"title": "Twin", "action": "cancelled"}
    )
    assert code == 1
    assert "de-duplicate" in out["error"]
    assert path.read_text() == before


def test_malformed_entries_do_not_crash_the_match(mark_entry, monkeypatch, capsys, tmp_path):
    path = _write(
        tmp_path,
        {"schema_version": 1, "tracking": ["nope", None, {"title": None}, _entry("Real")]},
    )
    code, _ = _run(mark_entry, monkeypatch, capsys, path, {"title": "Real", "action": "cancelled"})
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
        {"title": "Black Doves", "action": "released", "released": _before(16)},
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
        {"title": "Black Doves", "action": "released", "released": _before(16)},
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
        {"title": "Black Doves", "action": "cancelled"},
    )
    assert code == 1
    assert "does not exist" in out["error"]
    assert "watchlist mount" in out["error"]


def test_malformed_json_is_an_actionable_error(mark_entry, monkeypatch, capsys, tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text("{not json", encoding="utf-8")
    code, out = _run(mark_entry, monkeypatch, capsys, path, {"title": "X", "action": "cancelled"})
    assert code == 1
    assert "repair or restore valid JSON" in out["error"]


def test_a_non_object_root_is_an_error(mark_entry, monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, [_entry("Black Doves")])
    code, out = _run(
        mark_entry, monkeypatch, capsys, path, {"title": "Black Doves", "action": "cancelled"}
    )
    assert code == 1
    assert "root is not a JSON object" in out["error"]


def test_missing_tracking_list_is_an_error(mark_entry, monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, {"schema_version": 1})
    code, out = _run(mark_entry, monkeypatch, capsys, path, {"title": "X", "action": "cancelled"})
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
            {"title": "Black Doves", "action": "released", "released": _before(16)},
            # The request file has to live outside the locked directory.
            tmp_path=tmp_path,
        )
    finally:
        os.chmod(locked, 0o700)
    assert code == 1
    assert "did NOT land" in out["error"]
    assert json.loads(path.read_text())["tracking"][0]["notified"] is False


# ---------------------------------------------------------------------------
# Request contract — the title never reaches a shell
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "request_obj,needle",
    [
        ({"action": "cancelled"}, "no usable `title`"),
        ({"title": "  ", "action": "cancelled"}, "no usable `title`"),
        ({"title": "X"}, "`action`"),
        ({"title": "X", "action": "deleted"}, "`action`"),
        ({"title": "X", "action": "released"}, "no `released` date"),
        ({"title": "X", "action": "released", "released": 20260801}, "no `released` date"),
        ({"title": "X", "action": "released", "released": "yesterday"}, "not a YYYY-MM-DD date"),
        ({"title": "X", "action": "released", "released": "20260801"}, "not canonical YYYY-MM-DD"),
        ("[]", "must hold a JSON object"),
        ("{not json", "not valid JSON"),
    ],
)
def test_a_malformed_request_is_refused(
    mark_entry, monkeypatch, capsys, tmp_path, request_obj, needle
):
    path = _write(tmp_path, {"schema_version": 1, "tracking": [_entry("X")]})
    before = path.read_text()
    code, out = _run(mark_entry, monkeypatch, capsys, path, request_obj)
    assert code == 1
    assert needle in out["error"]
    assert path.read_text() == before


def test_a_bad_released_date_names_the_request_file_not_a_flag(
    mark_entry, monkeypatch, capsys, tmp_path
):
    """`released` arrives inside the `--input` file. Naming a `--released`
    flag sends the caller looking for an argument the script does not
    accept."""
    path = _write(tmp_path, {"schema_version": 1, "tracking": [_entry("X")]})
    code, out = _run(
        mark_entry,
        monkeypatch,
        capsys,
        path,
        {"title": "X", "action": "released", "released": "yesterday"},
    )
    assert code == 1
    assert "--released" not in out["error"]
    assert "--input" in out["error"]
    assert "`released`" in out["error"]


def test_a_missing_request_file_is_refused(mark_entry, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("CHECK_WATCHLIST_PATH", str(tmp_path / "watchlist.json"))
    code = mark_entry.main(["--input", str(tmp_path / "absent.json")])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert "does not exist" in out["error"]


@pytest.mark.parametrize(
    "title",
    [
        "It's Always Sunny in Philadelphia",
        'The "Quoted" Show',
        "Show; rm -rf /",
        "Show $(whoami)",
        "Show `id`",
        "Show & Co | grep",
        "Sh\\owback",
    ],
)
def test_shell_hostile_titles_round_trip(mark_entry, monkeypatch, capsys, tmp_path, title):
    """The request is a JSON file, never command-line text, so a title
    with an apostrophe or a command substitution is ordinary data."""
    path = _write(tmp_path, {"schema_version": 1, "tracking": [_entry(title)]})
    code, out = _run(mark_entry, monkeypatch, capsys, path, {"title": title, "action": "cancelled"})
    assert code == 0
    assert out["title"] == title
    assert json.loads(path.read_text())["tracking"][0]["cancelled"] is True
