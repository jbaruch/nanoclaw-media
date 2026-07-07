#!/usr/bin/env python3
"""
Append new Audible books to books-library.csv.

Reads JSON from stdin (audible_backup tool output).
Appends only successfully backed-up books: entries whose `status` is
present and not "ok" (failed download/decrypt) are excluded and counted
in `skipped_failed`; entries without a `status` field pass through.
Deduplicates by ASIN against existing CSV rows AND within the input
payload (duplicate ASINs appearing twice in a single `books` array
collapse to one row). On a brand-new or empty target CSV, the CSV
header row is written before data so the next run's `csv.DictReader`
can parse it correctly.
Outputs a JSON summary to stdout: {appended, skipped_existing,
skipped_failed, csv_total, books} — all fields present on every run,
including no-op runs with zero eligible books.

Concurrency: read-existing → check-new → append is wrapped in an advisory
exclusive file lock (fcntl.flock) so two simultaneous runs can't both see the
same "existing" set and double-write the same books.
"""

import csv
import fcntl
import json
import os
import sys

CSV_PATH = os.environ.get("BOOKS_CSV", "/workspace/group/books-library.csv")


def _lock_path_for(csv_path):
    """Return the sibling lock-file path for a given CSV target.

    Derived per-call so `append_books_locked(csv_path=...)` locks the
    file it's actually writing to — not a global LOCK_PATH tied to
    CSV_PATH. Tests that point at an alternative output location get
    a correctly-scoped lock; production keeps locking
    `<CSV_PATH>.lock`.
    """
    return csv_path + ".lock"


HEADERS = [
    "Key",
    "Title",
    "Author",
    "Narrated By",
    "Purchase Date",
    "Duration",
    "Release Date",
    "Ave. Rating",
    "Genre",
    "Series Name",
    "Series Sequence",
    "Product ID",
    "ASIN",
    "Book URL",
    "Summary",
    "Description",
    "Rating Count",
    "Publisher",
    "Short Title",
    "Copyright",
    "Author URL",
    "File name",
    "Series URL",
    "Abridged",
    "Language",
    "PDF URL",
    "Image URL",
    "Region",
    "File Paths",
    "AYCE",
    "Read Status",
    "User ID",
    "Audible (AAX)",
    "MP3",
    "Image",
    "M4B",
    "PDF",
]


def seconds_to_duration(secs):
    """Convert seconds (int) to HH:MM:00 format; non-positive or
    non-numeric input → ""."""
    try:
        secs = int(secs)
    except (ValueError, TypeError):
        return ""
    if secs <= 0:
        return ""
    mins = secs // 60
    h = mins // 60
    m = mins % 60
    return f"{h:02d}:{m:02d}:00"


def bool_str(val):
    """Normalize a tool boolean (lowercase string bool, or bool) to
    the OpenAudible lowercase string convention; empty/missing → "false"."""
    if val in (None, ""):
        return "false"
    return str(val).lower()


def map_book(book):
    """Map audible_backup JSON book to CSV row dict."""
    row = {h: "" for h in HEADERS}

    row["ASIN"] = book.get("asin", "")
    row["Title"] = book.get("title", "")
    row["Short Title"] = book.get("title_short") or book.get("title", "")
    row["Author"] = book.get("author", "")
    row["Narrated By"] = book.get("narrated_by", "")
    row["Genre"] = book.get("genre", "")
    row["Ave. Rating"] = str(book.get("rating_average", ""))
    row["Rating Count"] = str(book.get("rating_count", ""))
    row["Image URL"] = book.get("image_url", "")
    row["Series Name"] = book.get("series_name", "")
    row["Series Sequence"] = book.get("series_sequence", "")
    # Tool output provides `duration` pre-formatted (HH:MM:SS) — passed
    # through verbatim; fall back to deriving HH:MM:00 from `seconds`
    # when absent.
    row["Duration"] = book.get("duration") or seconds_to_duration(book.get("seconds"))
    # Tool provides these; hardcoded values remain only as fallbacks
    # for payloads that omit them
    row["Language"] = book.get("language") or "english"
    row["Region"] = book.get("region") or "US"
    row["Abridged"] = bool_str(book.get("abridged"))
    row["AYCE"] = bool_str(book.get("ayce"))

    row["Book URL"] = book.get("info_link", "")
    row["Summary"] = book.get("summary", "")
    row["Description"] = book.get("description", "")
    row["Publisher"] = book.get("publisher", "")
    row["Copyright"] = book.get("copyright", "")
    row["Author URL"] = book.get("author_link", "")
    row["Series URL"] = book.get("series_link", "")

    # Populated by the download step; empty on dry-run payloads
    row["File name"] = book.get("filename", "")
    row["File Paths"] = "; ".join(book.get("files") or [])
    row["User ID"] = book.get("user_id", "")

    # Purchase date: extract date part if datetime
    pd = book.get("purchase_date", "")
    if pd and "T" in str(pd):
        pd = str(pd).split("T")[0]
    row["Purchase Date"] = str(pd)

    row["Release Date"] = str(book.get("release_date", ""))

    # Read status: tool output uses OpenAudible convention
    # ("Unread" / "Reading" / "Finished") — pass through as-is
    row["Read Status"] = book.get("read_status", "")

    # M4B path from download
    row["M4B"] = book.get("m4b_path", "")

    # Key and Product ID may be empty until OpenAudible/enrich fills
    # them; ASIN stays as the fallback
    row["Key"] = book.get("key") or book.get("asin", "")
    row["Product ID"] = book.get("product_id") or book.get("asin", "")

    return row


def partition_by_status(books):
    """Split books into (ok, failed) by per-book `status`.

    The backup tool marks each downloaded book with `status: "ok"` or a
    failure status. A book without a `status` field counts as ok so
    dry-run payloads (which carry no status) keep working.
    """
    ok, failed = [], []
    for book in books:
        if book.get("status", "ok") == "ok":
            ok.append(book)
        else:
            failed.append(book)
    return ok, failed


def get_existing_asins(csv_path):
    """Read existing ASINs from CSV."""
    asins = set()
    if not os.path.exists(csv_path):
        return asins
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            asin = row.get("ASIN", "").strip()
            if asin:
                asins.add(asin)
    return asins


def append_books_locked(csv_path, books):
    """
    Append new book rows to CSV under an exclusive advisory lock.
    Returns (appended_list, existing_before_count, skipped_count).
    """
    # Use a sibling lock file — per csv_path, not a global — so tests or
    # alternative output paths get their own lock and don't contend
    # with readers of the CSV itself. flock is held for the entire
    # read-check-write window.
    lock_path = _lock_path_for(csv_path)
    # Mode "a" instead of "w": "w" would truncate the lockfile on every
    # run (wasted syscall, and a reader that opened the lockfile for
    # any reason would see zero bytes mid-run). The file content is
    # irrelevant — flock only needs a valid fd on the lockable inode —
    # so "a" (or "a+") leaves the file alone and is idempotent across
    # concurrent openers.
    with open(lock_path, "a") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            existing = get_existing_asins(csv_path)
            # Dedup against BOTH the existing CSV and the input itself so
            # an ASIN that appears twice in `books` lands only once. The
            # pre-existing filter (no lock at the time) checked against
            # `existing` alone, so duplicate ASINs repeated within a
            # single `books` payload would both pass the check and both
            # get written in a single run. Adding the lock alone would
            # not have fixed this intra-input case, so it's addressed
            # here as part of the same write path.
            seen = set(existing)
            new_books = []
            for b in books:
                asin = b.get("asin")
                if asin and asin not in seen:
                    seen.add(asin)
                    new_books.append(b)

            appended = []
            if new_books:
                # If the CSV doesn't exist yet OR is empty, write the
                # header before data rows. Without this, the first-ever
                # run creates a header-less file: next-run
                # csv.DictReader treats row 0 as the header, row 1's
                # ASIN lands in the "Key" column, and dedup quietly
                # fails. Checked inside the lock so two concurrent
                # first-run attempts can't both write headers.
                needs_header = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
                with open(csv_path, "a", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=HEADERS, quoting=csv.QUOTE_ALL)
                    if needs_header:
                        writer.writeheader()
                    for book in new_books:
                        row = map_book(book)
                        writer.writerow(row)
                        appended.append(
                            {
                                "asin": row["ASIN"],
                                "title": row["Title"],
                                "author": row["Author"],
                                "series": row["Series Name"],
                            }
                        )

            skipped = len(books) - len(appended)
            return appended, len(existing), skipped
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def main():
    data = json.load(sys.stdin)

    # `or []` guards a literal `"books": null` in the payload, which
    # data.get() would pass through and partition_by_status() would
    # then try to iterate.
    books, failed = partition_by_status(data.get("books") or [])

    # No early return for an empty eligible list: the locked path handles
    # it without touching the CSV (no header, no rows, no CSV creation —
    # only the sibling lock file is opened) and still yields the existing
    # count, so `csv_total` is present in the no-op case too and the
    # output contract stays uniform.
    appended, existing_count, skipped = append_books_locked(CSV_PATH, books)

    print(
        json.dumps(
            {
                "appended": len(appended),
                "skipped_existing": skipped,
                "skipped_failed": len(failed),
                "csv_total": existing_count + len(appended),
                "books": appended,
            }
        )
    )


if __name__ == "__main__":
    main()
