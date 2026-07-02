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
    return {"asin": asin, "title": title, "author": author}


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


def test_seconds_to_duration_formats_hh_mm(csv_append):
    """Pure helper: 5400 seconds → "01:30:00"; non-numeric → "" """
    module, _ = csv_append
    assert module.seconds_to_duration(5400) == "01:30:00"
    assert module.seconds_to_duration(0) == ""
    assert module.seconds_to_duration("0") == ""
    assert module.seconds_to_duration(-5400) == ""
    assert module.seconds_to_duration(None) == ""
    assert module.seconds_to_duration("not-a-number") == ""


def test_map_book_reads_actual_tool_output_keys(csv_append, monkeypatch, capsys):
    """Field names must match the audible_backup tool output (issue #4)."""
    module, csv_path = csv_append
    books = [
        {
            "asin": "ASIN777",
            "title": "Mapped Book",
            "author": "Real Author",
            "narrated_by": "Real Narrator",
            "genre": "Sci-Fi",
            "rating_average": "4.7",
            "rating_count": "1234",
            "image_url": "https://img.example/cover.jpg",
            "series_name": "Real Series",
            "series_sequence": "2",
            "duration": "07:45:00",
            "seconds": 27900,
            "read_status": "Reading",
        }
    ]
    _run(module, monkeypatch, capsys, {"books": books})
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    row = rows[0]
    assert row["Author"] == "Real Author"
    assert row["Narrated By"] == "Real Narrator"
    assert row["Genre"] == "Sci-Fi"
    assert row["Ave. Rating"] == "4.7"
    assert row["Rating Count"] == "1234"
    assert row["Image URL"] == "https://img.example/cover.jpg"
    assert row["Series Name"] == "Real Series"
    assert row["Duration"] == "07:45:00"
    assert row["Read Status"] == "Reading"


def test_map_book_maps_issue10_fields(csv_append, monkeypatch, capsys):
    """Previously hardcoded/ignored fields come from the tool output (issue #10)."""
    module, csv_path = csv_append
    books = [
        {
            "asin": "ASIN900",
            "title": "Long Winded Title: A Novel",
            "title_short": "Long Winded Title",
            "key": "KEY900",
            "product_id": "PROD900",
            "language": "german",
            "region": "DE",
            "abridged": "true",
            "ayce": "true",
            "info_link": "https://www.audible.com/pd/ASIN900",
            "summary": "A summary, with commas.",
            "description": "A description.",
            "publisher": "Acme Audio",
            "copyright": "©2026 Acme",
            "author_link": "https://www.audible.com/author/Some+Author/A1",
            "series_link": "/series/Real-Series/S1",
            "filename": "Long_Winded_Title.m4b",
            "files": ["/library/books/a.m4b", "/library/books/a.jpg"],
            "user_id": "user-1",
        }
    ]
    _run(module, monkeypatch, capsys, {"books": books})
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    row = rows[0]
    assert row["Short Title"] == "Long Winded Title"
    assert row["Key"] == "KEY900"
    assert row["Product ID"] == "PROD900"
    assert row["Language"] == "german"
    assert row["Region"] == "DE"
    assert row["Abridged"] == "true"
    assert row["AYCE"] == "true"
    assert row["Book URL"] == "https://www.audible.com/pd/ASIN900"
    assert row["Summary"] == "A summary, with commas."
    assert row["Description"] == "A description."
    assert row["Publisher"] == "Acme Audio"
    assert row["Copyright"] == "©2026 Acme"
    assert row["Author URL"] == "https://www.audible.com/author/Some+Author/A1"
    assert row["Series URL"] == "/series/Real-Series/S1"
    assert row["File name"] == "Long_Winded_Title.m4b"
    assert row["File Paths"] == "/library/books/a.m4b; /library/books/a.jpg"
    assert row["User ID"] == "user-1"


def test_map_book_issue10_fallbacks(csv_append, monkeypatch, capsys):
    """Absent issue-#10 fields keep the previous defaults and fallbacks."""
    module, csv_path = csv_append
    _run(module, monkeypatch, capsys, {"books": [_book("ASIN901", "Only Title")]})
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    row = rows[0]
    assert row["Short Title"] == "Only Title"
    assert row["Key"] == "ASIN901"
    assert row["Product ID"] == "ASIN901"
    assert row["Language"] == "english"
    assert row["Region"] == "US"
    assert row["Abridged"] == "false"
    assert row["AYCE"] == "false"
    assert row["File Paths"] == ""


def test_duration_falls_back_to_seconds(csv_append, monkeypatch, capsys):
    """Without a pre-formatted `duration`, derive HH:MM:00 from `seconds`."""
    module, csv_path = csv_append
    books = [{"asin": "ASIN778", "title": "Seconds Only", "seconds": 5400}]
    _run(module, monkeypatch, capsys, {"books": books})
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["Duration"] == "01:30:00"


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
