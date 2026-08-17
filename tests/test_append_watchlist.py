"""Tests for skills/recommend-shows/scripts/append-watchlist.py.

Locks down the documented contract per `coding-policy: testing-standards`:

  - Candidates arrive as a JSON array on stdin; an empty array is a
    valid no-op, not an error.
  - `notified` is written by the script and rejected in the input —
    delivery state belongs to the owner skill.
  - Every contract field (`title`, `platform`, `expected`, `reason`,
    `added`) is required and must be a non-empty string; `added` is a
    canonical `YYYY-MM-DD` date.
  - `expected` must be a format the release precheck can anchor
    (`YYYY-MM-DD`, `YYYY-Qn`, `YYYY-MM`, `YYYY`); anything else is
    refused, since an unanchored window becomes a nightly wake.
  - A malformed `tracking` on an existing record is read-only: a
    non-owner writer does not repair another skill's state. Only a
    record it creates gets an initialized list.
  - Duplicates are detected on the casefolded, whitespace-collapsed
    title and skipped rather than added twice.
  - Version rules for a NON-OWNER writer: create a record stamped
    version 1, append only at version 1, refuse an unstamped or
    otherwise-versioned record with the file untouched. It never
    migrates — that is the owner's job.
  - Every failure exits 1 with an actionable `error` on stdout, a
    diagnostic on stderr, and adds nothing.
"""

from __future__ import annotations

import io
import json
import os

import pytest

SCHEMA = 1


def _candidate(title, **extra):
    entry = {
        "title": title,
        "platform": "Netflix",
        "expected": "2026-Q4",
        "reason": "Matches his taste",
        "added": "2026-08-17",
    }
    entry.update(extra)
    return entry


def _run(module, monkeypatch, capsys, path, candidates):
    monkeypatch.setenv("CHECK_WATCHLIST_PATH", str(path))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(candidates)))
    code = module.main()
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


def _write(tmp_path, payload):
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Appending
# ---------------------------------------------------------------------------


def test_creates_a_stamped_record_when_the_file_is_absent(
    append_watchlist, monkeypatch, capsys, tmp_path
):
    """Authoring its own record is not migration — this is the one case
    where a non-owner writer stamps the version."""
    path = tmp_path / "watchlist.json"
    code, out = _run(append_watchlist, monkeypatch, capsys, path, [_candidate("New Show")])
    assert code == 0
    assert out["created"] is True
    assert out["added"] == ["New Show"]
    written = json.loads(path.read_text())
    assert written["schema_version"] == SCHEMA
    assert written["tracking"][0]["title"] == "New Show"


def test_appends_to_a_version_1_record(append_watchlist, monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, {"schema_version": SCHEMA, "tracking": [_candidate("Existing")]})
    code, out = _run(append_watchlist, monkeypatch, capsys, path, [_candidate("New Show")])
    assert code == 0
    assert out["created"] is False
    assert out["tracking_count"] == 2
    assert [e["title"] for e in json.loads(path.read_text())["tracking"]] == [
        "Existing",
        "New Show",
    ]


def test_sets_notified_false_on_every_new_entry(append_watchlist, monkeypatch, capsys, tmp_path):
    path = tmp_path / "watchlist.json"
    _run(append_watchlist, monkeypatch, capsys, path, [_candidate("New Show")])
    assert json.loads(path.read_text())["tracking"][0]["notified"] is False


def test_carries_only_the_contract_fields(append_watchlist, monkeypatch, capsys, tmp_path):
    path = tmp_path / "watchlist.json"
    _run(
        append_watchlist,
        monkeypatch,
        capsys,
        path,
        [_candidate("New Show", nonsense="drop me", last_verdict="released")],
    )
    written = json.loads(path.read_text())["tracking"][0]
    assert set(written) == {"title", "platform", "expected", "reason", "added", "notified"}


def test_an_empty_array_is_a_no_op(append_watchlist, monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, {"schema_version": SCHEMA, "tracking": [_candidate("Existing")]})
    before = path.read_text()
    code, out = _run(append_watchlist, monkeypatch, capsys, path, [])
    assert code == 0
    assert out["added"] == []
    assert path.read_text() == before


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("given", ["Existing", "existing", "  EXISTING  "])
def test_duplicates_are_skipped_not_added_twice(
    append_watchlist, monkeypatch, capsys, tmp_path, given
):
    path = _write(tmp_path, {"schema_version": SCHEMA, "tracking": [_candidate("Existing")]})
    code, out = _run(append_watchlist, monkeypatch, capsys, path, [_candidate(given)])
    assert code == 0
    assert out["added"] == []
    assert out["skipped_duplicates"] == [given.strip()]
    assert out["tracking_count"] == 1


def test_duplicates_within_one_batch_are_skipped(append_watchlist, monkeypatch, capsys, tmp_path):
    path = tmp_path / "watchlist.json"
    code, out = _run(
        append_watchlist,
        monkeypatch,
        capsys,
        path,
        [_candidate("Same"), _candidate("same"), _candidate("Other")],
    )
    assert code == 0
    assert out["added"] == ["Same", "Other"]
    assert out["skipped_duplicates"] == ["same"]


def test_a_duplicate_against_a_notified_entry_is_still_skipped(
    append_watchlist, monkeypatch, capsys, tmp_path
):
    """Re-adding a show already delivered would resurrect the alert."""
    path = _write(
        tmp_path,
        {
            "schema_version": SCHEMA,
            "tracking": [{"title": "Done", "notified": True, "released": "2026-01-01"}],
        },
    )
    code, out = _run(append_watchlist, monkeypatch, capsys, path, [_candidate("Done")])
    assert code == 0
    assert out["added"] == []
    assert out["skipped_duplicates"] == ["Done"]


# ---------------------------------------------------------------------------
# Version rules for a non-owner writer
# ---------------------------------------------------------------------------


def test_an_unstamped_record_is_read_only(append_watchlist, monkeypatch, capsys, tmp_path):
    """Stamping an existing record would be migration, which belongs to
    the owner skill."""
    path = _write(tmp_path, {"tracking": [_candidate("Existing")]})
    before = path.read_text()
    code, out = _run(append_watchlist, monkeypatch, capsys, path, [_candidate("New Show")])
    assert code == 1
    assert "does not migrate" in out["error"]
    assert "check-watchlist" in out["error"]
    assert path.read_text() == before


@pytest.mark.parametrize("version", [2, 0, "1", 1.5])
def test_an_unsupported_version_is_read_only(
    append_watchlist, monkeypatch, capsys, tmp_path, version
):
    path = _write(tmp_path, {"schema_version": version, "tracking": []})
    before = path.read_text()
    code, out = _run(append_watchlist, monkeypatch, capsys, path, [_candidate("New Show")])
    assert code == 1
    assert f"schema_version {version!r}" in out["error"]
    assert path.read_text() == before


def test_an_empty_file_is_treated_as_a_new_record(append_watchlist, monkeypatch, capsys, tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text("", encoding="utf-8")
    code, out = _run(append_watchlist, monkeypatch, capsys, path, [_candidate("New Show")])
    assert code == 0
    assert out["created"] is True
    assert json.loads(path.read_text())["schema_version"] == SCHEMA


@pytest.mark.parametrize("tracking", [None, {}, "nope", 7])
def test_a_malformed_tracking_list_is_read_only(
    append_watchlist, monkeypatch, capsys, tmp_path, tracking
):
    """Repairing another skill's malformed state is not a non-owner
    writer's call — only a record it creates gets an initialized list."""
    record = {"schema_version": SCHEMA}
    if tracking is not None:
        record["tracking"] = tracking
    path = _write(tmp_path, record)
    before = path.read_text()
    code, out = _run(append_watchlist, monkeypatch, capsys, path, [_candidate("New Show")])
    assert code == 1
    assert "no usable `tracking` list" in out["error"]
    assert path.read_text() == before


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_notified_in_the_input_is_rejected(append_watchlist, monkeypatch, capsys, tmp_path):
    path = tmp_path / "watchlist.json"
    code, out = _run(
        append_watchlist, monkeypatch, capsys, path, [_candidate("New Show", notified=True)]
    )
    assert code == 1
    assert "belongs to check-watchlist" in out["error"]
    assert not path.exists()


@pytest.mark.parametrize("expected", ["2026-06-18", "2026-Q4", "2026-q1", "2026-10", "2026"])
def test_anchorable_expected_values_are_accepted(
    append_watchlist, monkeypatch, capsys, tmp_path, expected
):
    path = tmp_path / "watchlist.json"
    code, _ = _run(
        append_watchlist, monkeypatch, capsys, path, [_candidate("New Show", expected=expected)]
    )
    assert code == 0


@pytest.mark.parametrize(
    "expected", ["TBA", "summer 2026", "2026-13", "2026-1", "2026-13-40", "late 2026"]
)
def test_unanchorable_expected_values_are_refused(
    append_watchlist, monkeypatch, capsys, tmp_path, expected
):
    """An `expected` the precheck cannot parse turns the entry into a
    nightly wake — the failure jbaruch/nanoclaw-media#67 was made of."""
    path = tmp_path / "watchlist.json"
    code, out = _run(
        append_watchlist, monkeypatch, capsys, path, [_candidate("New Show", expected=expected)]
    )
    assert code == 1
    assert "release precheck cannot anchor" in out["error"]
    assert not path.exists()


@pytest.mark.parametrize("field", ["title", "platform", "expected", "reason", "added"])
def test_every_contract_field_is_required(append_watchlist, monkeypatch, capsys, tmp_path, field):
    """A half-populated entry is a shape the readers' contract does not
    describe."""
    path = tmp_path / "watchlist.json"
    candidate = _candidate("New Show")
    del candidate[field]
    code, out = _run(append_watchlist, monkeypatch, capsys, path, [candidate])
    assert code == 1
    assert f"no usable `{field}`" in out["error"]
    assert not path.exists()


@pytest.mark.parametrize("value", ["", "   ", 2026, None, ["x"], {"a": 1}])
def test_non_string_or_blank_fields_are_refused(
    append_watchlist, monkeypatch, capsys, tmp_path, value
):
    path = tmp_path / "watchlist.json"
    code, out = _run(
        append_watchlist, monkeypatch, capsys, path, [_candidate("New Show", platform=value)]
    )
    assert code == 1
    assert "no usable `platform`" in out["error"]
    assert not path.exists()


@pytest.mark.parametrize("added", ["17-08-2026", "2026-8-17", "2026-13-40", "yesterday"])
def test_a_malformed_added_date_is_refused(append_watchlist, monkeypatch, capsys, tmp_path, added):
    path = tmp_path / "watchlist.json"
    code, out = _run(
        append_watchlist, monkeypatch, capsys, path, [_candidate("New Show", added=added)]
    )
    assert code == 1
    assert "canonical YYYY-MM-DD" in out["error"]
    assert not path.exists()


@pytest.mark.parametrize("candidates", [[{"platform": "Netflix"}], [{"title": "  "}], ["nope"]])
def test_unusable_candidates_are_refused(
    append_watchlist, monkeypatch, capsys, tmp_path, candidates
):
    path = tmp_path / "watchlist.json"
    code, out = _run(append_watchlist, monkeypatch, capsys, path, candidates)
    assert code == 1
    assert "error" in out
    assert not path.exists()


def test_a_non_array_input_is_refused(append_watchlist, monkeypatch, capsys, tmp_path):
    path = tmp_path / "watchlist.json"
    code, out = _run(append_watchlist, monkeypatch, capsys, path, {"title": "Not An Array"})
    assert code == 1
    assert "JSON array" in out["error"]


def test_malformed_stdin_is_refused(append_watchlist, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("CHECK_WATCHLIST_PATH", str(tmp_path / "watchlist.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    code = append_watchlist.main()
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert "stdin is not valid JSON" in out["error"]


# ---------------------------------------------------------------------------
# Write failures
# ---------------------------------------------------------------------------


def test_an_unwritable_destination_reports_nothing_added(
    append_watchlist, monkeypatch, capsys, tmp_path
):
    locked = tmp_path / "locked"
    locked.mkdir()
    path = locked / "watchlist.json"
    path.write_text(json.dumps({"schema_version": SCHEMA, "tracking": []}), encoding="utf-8")
    os.chmod(locked, 0o500)
    try:
        code, out = _run(append_watchlist, monkeypatch, capsys, path, [_candidate("New Show")])
    finally:
        os.chmod(locked, 0o700)
    assert code == 1
    assert "no show was added" in out["error"]
    assert json.loads(path.read_text())["tracking"] == []
