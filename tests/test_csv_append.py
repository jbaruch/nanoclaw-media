"""Baseline tests for audible-backup/scripts/csv-append.py.

Locks down the documented contract per `coding-policy: testing-standards`:

  - Reads JSON `{books: [...]}` from stdin
  - Dedups by ASIN against existing CSV rows AND within the input batch
  - On a brand-new or empty CSV, writes the header row before data
  - Holds `flock(LOCK_EX)` on a sibling lock file across the
    read-check-write cycle
  - Outputs JSON `{appended, skipped_existing, csv_total, books}`
"""

import csv
import json


class _FakeStdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


def _run(module, monkeypatch, capsys, payload):
    monkeypatch.setattr("sys.argv", ["csv-append.py"])
    monkeypatch.setattr("sys.stdin", _FakeStdin(json.dumps(payload)))
    code = 0
    try:
        result = module.main()
        code = 0 if result is None else int(result)
    except SystemExit as exc:
        code = 0 if exc.code is None else int(exc.code)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _book(asin, title="Some Book", author="Some Author"):
    return {"asin": asin, "title": title, "authors": author}


def test_empty_books_returns_zero_appended(csv_append, monkeypatch, capsys):
    module, csv_path = csv_append
    code, out, _ = _run(module, monkeypatch, capsys, {"books": []})
    assert code == 0
    payload = json.loads(out)
    assert payload == {"appended": 0, "skipped_existing": 0, "books": []}
    # No CSV created for an empty batch.
    assert not csv_path.exists()


def test_first_run_writes_header_then_data(csv_append, monkeypatch, capsys):
    module, csv_path = csv_append
    books = [_book("ASIN001", "Title One", "Author A")]
    code, out, _ = _run(module, monkeypatch, capsys, {"books": books})
    assert code == 0
    payload = json.loads(out)
    assert payload["appended"] == 1
    # Re-read via DictReader: header must be parseable.
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["ASIN"] == "ASIN001"
    assert rows[0]["Title"] == "Title One"
    assert rows[0]["Author"] == "Author A"


def test_dedup_against_existing_csv(csv_append, monkeypatch, capsys):
    module, csv_path = csv_append
    # First run lands the row.
    _run(module, monkeypatch, capsys, {"books": [_book("ASIN001")]})
    # Second run with the same ASIN — should skip.
    code, out, _ = _run(module, monkeypatch, capsys, {"books": [_book("ASIN001")]})
    assert code == 0
    payload = json.loads(out)
    assert payload["appended"] == 0
    assert payload["skipped_existing"] == 1


def test_dedup_within_input_batch(csv_append, monkeypatch, capsys):
    """An ASIN appearing twice in a single input payload collapses to one row."""
    module, csv_path = csv_append
    books = [_book("ASIN001", "First Variant"), _book("ASIN001", "Second Variant")]
    code, out, _ = _run(module, monkeypatch, capsys, {"books": books})
    assert code == 0
    payload = json.loads(out)
    assert payload["appended"] == 1
    assert payload["skipped_existing"] == 1
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    # First-seen wins per the script's `for b in books` order.
    assert rows[0]["Title"] == "First Variant"


def test_minutes_to_duration_formats_hh_mm(csv_append):
    """Pure helper: 90 minutes → "01:30:00"; non-numeric → "" """
    module, _ = csv_append
    assert module.minutes_to_duration(90) == "01:30:00"
    assert module.minutes_to_duration(0) == ""
    assert module.minutes_to_duration(None) == ""
    assert module.minutes_to_duration("not-a-number") == ""


def test_purchase_date_iso_strips_time(csv_append, monkeypatch, capsys):
    """A datetime-shaped purchase_date drops everything after `T`."""
    module, csv_path = csv_append
    books = [{"asin": "ASIN042", "title": "Dated", "purchase_date": "2026-04-30T12:34:56Z"}]
    _run(module, monkeypatch, capsys, {"books": books})
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["Purchase Date"] == "2026-04-30"


def test_books_without_asin_skipped(csv_append, monkeypatch, capsys):
    """ASIN-less entries fall out of the dedup loop's `if asin and asin not in seen` guard."""
    module, csv_path = csv_append
    books = [{"title": "No ASIN"}, _book("ASIN001")]
    _run(module, monkeypatch, capsys, {"books": books})
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["ASIN"] == "ASIN001"
