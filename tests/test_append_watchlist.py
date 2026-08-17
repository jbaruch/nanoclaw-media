"""Tests for skills/recommend-shows/scripts/append-watchlist.py.

Locks down the documented contract per `coding-policy: testing-standards`:

  - Candidates arrive as a JSON array in the file named by `--input`,
    never as command-line text: a title carrying an apostrophe or a
    command substitution is ordinary data. An empty array is a valid
    no-op, not an error.
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
    version 1 only when the file is absent, append only at an integer
    version 1 (a boolean stamp is not 1, despite `True == 1`), refuse an
    unstamped, otherwise-versioned, empty, or malformed record with the
    file untouched. It never migrates — that is the owner's job.
  - Every failure exits 1 with an actionable `error` on stdout, a
    diagnostic on stderr, and adds nothing.
"""

from __future__ import annotations

import json
import os
from datetime import date

import pytest

SCHEMA = 1

# This script does no date arithmetic — `expected` and `added` are
# format-validated only. Fixtures are still built from one fixed
# reference rather than future literals, so nothing here reads as a
# date against the real clock.
_REF = date(2026, 8, 17)
_YEAR = _REF.year


def _candidate(title, **extra):
    entry = {
        "title": title,
        "platform": "Netflix",
        "expected": f"{_YEAR}-Q4",
        "reason": "Matches his taste",
        "added": _REF.isoformat(),
    }
    entry.update(extra)
    return entry


def _run(module, monkeypatch, capsys, path, candidates, *, tmp_path=None):
    """`candidates` is the JSON array the skill writes; a raw string is
    passed through so malformed-input cases can be exercised."""
    monkeypatch.setenv("CHECK_WATCHLIST_PATH", str(path))
    input_path = (tmp_path or path.parent) / "candidates.json"
    input_path.write_text(
        candidates if isinstance(candidates, str) else json.dumps(candidates), encoding="utf-8"
    )
    code = module.main(["--input", str(input_path)])
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
            "tracking": [{"title": "Done", "notified": True, "released": "2024-01-01"}],
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


@pytest.mark.parametrize("version", [2, 0, -1, "1", 1.5, True])
def test_an_unsupported_version_is_read_only(
    append_watchlist, monkeypatch, capsys, tmp_path, version
):
    path = _write(tmp_path, {"schema_version": version, "tracking": []})
    before = path.read_text()
    code, out = _run(append_watchlist, monkeypatch, capsys, path, [_candidate("New Show")])
    assert code == 1
    assert f"schema_version {version!r}" in out["error"]
    assert path.read_text() == before


@pytest.mark.parametrize("content", ["", "   \n"])
def test_an_existing_empty_file_is_refused(
    append_watchlist, monkeypatch, capsys, tmp_path, content
):
    """An empty file is state some writer left behind, not absence.
    Creating a record over it would overwrite the owner's state."""
    path = tmp_path / "watchlist.json"
    path.write_text(content, encoding="utf-8")
    code, out = _run(append_watchlist, monkeypatch, capsys, path, [_candidate("New Show")])
    assert code == 1
    assert "exists but is empty" in out["error"]
    assert path.read_text() == content


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


@pytest.mark.parametrize(
    "expected",
    [_REF.isoformat(), f"{_YEAR}-Q4", f"{_YEAR}-q1", f"{_YEAR}-10", f"{_YEAR}"],
)
def test_anchorable_expected_values_are_accepted(
    append_watchlist, monkeypatch, capsys, tmp_path, expected
):
    path = tmp_path / "watchlist.json"
    code, _ = _run(
        append_watchlist, monkeypatch, capsys, path, [_candidate("New Show", expected=expected)]
    )
    assert code == 0


@pytest.mark.parametrize(
    "expected",
    ["TBA", "summer", f"{_YEAR}-13", f"{_YEAR}-1", f"{_YEAR}-13-40", f"late {_YEAR}"],
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


@pytest.mark.parametrize(
    "added",
    [f"17-08-{_YEAR}", f"{_YEAR}-8-17", f"{_YEAR}-13-40", "yesterday"],
)
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


def test_a_malformed_input_file_is_refused(append_watchlist, monkeypatch, capsys, tmp_path):
    code, out = _run(
        append_watchlist, monkeypatch, capsys, tmp_path / "watchlist.json", "{not json"
    )
    assert code == 1
    assert "is not valid JSON" in out["error"]


def test_a_missing_input_file_is_refused(append_watchlist, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("CHECK_WATCHLIST_PATH", str(tmp_path / "watchlist.json"))
    code = append_watchlist.main(["--input", str(tmp_path / "absent.json")])
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
    ],
)
def test_shell_hostile_titles_round_trip(append_watchlist, monkeypatch, capsys, tmp_path, title):
    """The candidates are a JSON file, never command-line text — an
    apostrophe in a title is ordinary data."""
    path = tmp_path / "watchlist.json"
    code, out = _run(append_watchlist, monkeypatch, capsys, path, [_candidate(title)])
    assert code == 0
    assert out["added"] == [title]
    assert json.loads(path.read_text())["tracking"][0]["title"] == title


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
        # The input file has to live outside the locked directory.
        code, out = _run(
            append_watchlist, monkeypatch, capsys, path, [_candidate("New Show")], tmp_path=tmp_path
        )
    finally:
        os.chmod(locked, 0o700)
    assert code == 1
    assert "no show was added" in out["error"]
    assert json.loads(path.read_text())["tracking"] == []
