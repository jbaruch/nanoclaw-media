#!/usr/bin/env python3
"""Precheck for `tessl__youtube-comment-check`.

Filesystem cadence cap. Mirrors the slice-1 (`nightly-undated-task-sweep`)
contract: read `<state_dir>/youtube-comment-check-cursor.json`; wake the
agent when the cursor is missing, malformed, schema-mismatched, or older
than CADENCE; otherwise emit `wake_agent: false` so the weekly cron fires
for free.

Comment-count-delta gating via the YouTube tool was rejected for the same
reason slice 1 rejected a Tasks-API gate (no precedent in this plugin, OAuth
refresh fragility, fail-open leaks during 5xx incidents). See
`references/cadence-rationale.md`.

Wake reasons:
  - `no_cursor`              — first run / cursor file absent.
  - `cursor_error`           — read / parse / schema failure (fail-open).
  - `cursor_unparseable`     — `last_run` ISO unparseable.
  - `cursor_naive_datetime`  — `last_run` ISO has no Z / offset.
  - `cursor_future`          — `last_run` is later than `now_utc`.
  - `cadence_elapsed`        — `last_run` older than CADENCE.

No-wake reason:
  - `within_cadence`         — last_run < CADENCE ago.

Per `coding-policy: file-hygiene`: always exit 0 with valid JSON on
stdout. The scheduler treats non-zero exit as `wake_agent: false`,
which would silently suppress the check during the exact incidents
the gate is meant to surface.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 6d, not the 168h weekly cron interval: the cursor stamps at run completion,
# so a 168h cap perpetually near-misses (next same-time fire age ~167.8h < 168h)
# and skips forever. The 24h slack absorbs run latency + DST. jbaruch/nanoclaw#803,
# jbaruch/nanoclaw-admin#353.
CADENCE = timedelta(days=6)
DEFAULT_CURSOR_PATH = "/workspace/group/state/youtube-comment-check-cursor.json"
SUPPORTED_SCHEMA_VERSION = 1


def _parse_iso(value: str) -> datetime | None:
    """Parse ISO-8601 with trailing `Z` or explicit offset."""
    try:
        normalised = value[:-1] + "+00:00" if value.endswith("Z") else value
        return datetime.fromisoformat(normalised)
    except (TypeError, ValueError):
        return None


def _read_cursor(path: Path) -> tuple[str | None, str | None]:
    """Return `(last_run_iso, error)`.

    `(None, None)` — cursor file missing / empty (first run).
    `(iso, None)`  — happy path; `iso` is the raw value to be parsed.
    `(None, err)`  — read or schema error; caller fails open.

    Read attempted directly rather than guarded by `Path.exists()`:
    `exists()` returns False on permission-denied / IO errors, which
    would mis-route an unreadable cursor down the no_cursor branch
    (still a wake, so wake_agent stays true — but the diagnostic
    silently drops to "first run" and the operator never sees the
    cursor_error reason/error text needed to triage the outage).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"cursor read failed: {exc}"
    except UnicodeDecodeError as exc:
        return None, f"cursor not valid UTF-8: {exc}"
    if not text.strip():
        return None, None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"cursor JSON malformed: {exc}"
    if not isinstance(data, dict):
        return None, f"cursor root is {type(data).__name__}; expected object"
    schema = data.get("schema_version")
    if schema != SUPPORTED_SCHEMA_VERSION:
        return None, f"cursor schema_version={schema!r}; expected {SUPPORTED_SCHEMA_VERSION}"
    last_run = data.get("last_run")
    if not isinstance(last_run, str):
        return None, f"cursor last_run is {type(last_run).__name__}; expected string"
    return last_run, None


def decide(now_utc: datetime, cursor_path: Path) -> dict:
    """Pure decision function — frozen `now_utc` and the cursor path go
    in, the wake decision plus diagnostics come out."""
    last_run_iso, err = _read_cursor(cursor_path)

    if err is not None:
        return {
            "wake_agent": True,
            "data": {
                "reason": "cursor_error",
                "error": err,
                "cursor_path": str(cursor_path),
            },
        }

    if last_run_iso is None:
        return {
            "wake_agent": True,
            "data": {
                "reason": "no_cursor",
                "cursor_path": str(cursor_path),
            },
        }

    last_run = _parse_iso(last_run_iso)
    if last_run is None:
        return {
            "wake_agent": True,
            "data": {
                "reason": "cursor_unparseable",
                "last_run_raw": last_run_iso,
                "cursor_path": str(cursor_path),
            },
        }

    if last_run.tzinfo is None:
        return {
            "wake_agent": True,
            "data": {
                "reason": "cursor_naive_datetime",
                "last_run_raw": last_run_iso,
                "cursor_path": str(cursor_path),
            },
        }

    age = now_utc - last_run
    age_hours = age.total_seconds() / 3600.0

    if age < timedelta(0):
        return {
            "wake_agent": True,
            "data": {
                "reason": "cursor_future",
                "last_run": last_run_iso,
                "age_hours": round(age_hours, 2),
            },
        }

    if age >= CADENCE:
        return {
            "wake_agent": True,
            "data": {
                "reason": "cadence_elapsed",
                "last_run": last_run_iso,
                "age_hours": round(age_hours, 2),
                "cadence_hours": CADENCE.total_seconds() / 3600.0,
            },
        }

    return {
        "wake_agent": False,
        "data": {
            "reason": "within_cadence",
            "last_run": last_run_iso,
            "age_hours": round(age_hours, 2),
            "cadence_hours": CADENCE.total_seconds() / 3600.0,
        },
    }


def main() -> int:
    cursor_path = Path(os.environ.get("YOUTUBE_COMMENT_CHECK_CURSOR", DEFAULT_CURSOR_PATH))
    now_utc = datetime.now(timezone.utc)
    payload = decide(now_utc, cursor_path)
    sys.stdout.write(json.dumps(payload) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
