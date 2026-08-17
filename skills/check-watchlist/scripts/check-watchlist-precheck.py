#!/usr/bin/env python3
"""Precheck for `tessl__check-watchlist`.

Decides whether the daily 09:30 UTC fire should wake the LLM at all by
reading `/workspace/group/watchlist.json` and date-gating the entries
where `notified` is false.

`notified: false` is the *permanent* steady state of a tracked-but-
unreleased show (SKILL.md Step 3: "If not yet released: Stay completely
silent. Do not update the file."). So gating on `notified: false` alone
woke the agent every single fire for shows years from release — pure
noise (jbaruch/nanoclaw-media#2). Instead we parse each unnotified
entry's `expected` release window and wake only when one is due within
`LEAD`.

Wake reasons:
  - `release_due` — at least one unnotified show's release window is
    within `LEAD` days, or its `expected` value can't be parsed (a
    bad/missing date is treated conservatively as due so we never miss
    a real release), AND that entry is not inside its recheck backoff.

No-wake reasons:
  - `all_future`           — every unnotified show's release window is
    beyond the lead. The steady state for far-off tracked shows.
  - `within_recheck_backoff` — every otherwise-due entry was resolved
    recently enough that re-asking today would repeat the same answer.
  - `all_notified`         — no entry has `notified: false`.
  - `file_missing`         — watchlist.json absent (matches Step 1's
    "exit silently" contract).
  - `file_unreadable`      — IO error / non-UTF-8 / malformed JSON
    (silent skip per `jbaruch/nanoclaw#516` spec — the agent has
    nothing useful to do with a broken file).
  - `tracking_missing`     — JSON parsed but root has no `tracking`
    list (treat as empty).

Fuzzy `expected` formats anchored to a release-window start date:
  - ISO date   `2026-06-18` → that day
  - Quarter    `2026-Q3`    → first day of the quarter (Q1→Jan, Q2→Apr,
                              Q3→Jul, Q4→Oct)
  - Month      `2026-10`    → first day of the month
  - Bare year  `2026`       → Jan 1 of that year
  - anything else / missing → un-parseable → conservative wake

Recheck backoff (jbaruch/nanoclaw-media#67)
-------------------------------------------
A coarse `expected` is due for a long time: a bare year anchors to Jan 1
and stays inside the window for the rest of that year, so date-gating
alone woke the agent nightly for the same four never-due titles. The
window still opens on the anchor — narrowing it would blind the check to
an early release — but a re-ask is rate-limited by how precise the
`expected` value is (`_RECHECK_INTERVALS`): a dated entry is re-asked
daily, a bare year monthly.

The backoff reads the `last_checked` / `last_verdict` stamps
`verify-release.py` writes back after each resolved entry (contract:
skills/check-watchlist/state-schema.md). Entries it never resolved carry
no stamp and stay due. A `last_verdict` of `released` is never
suppressed — that alert has not been delivered while `notified` is still
false.

This precheck reads `schema_version` `<= SUPPORTED_SCHEMA_VERSION`, an
absent stamp included (legacy pre-v1, same shape). A record stamped
newer means this reader is lagging: it has no usable prior state, so it
ignores every versioned field and date-gates alone. That fallback wakes,
never silences — a reader too old to read the stamps must not suppress
an alert on their strength.

When wakes happen, `data` carries `due_count` and `titles` so the
agent's first turn doesn't re-read the file.

Per `coding-policy: file-hygiene`: always exit 0 with valid JSON on
stdout. The scheduler reads the JSON's `wake_agent` boolean to decide
whether to invoke the LLM.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DEFAULT_WATCHLIST_PATH = "/workspace/group/watchlist.json"

# How far ahead of a release window we start waking. A release rarely
# lands exactly on its announced date, so a week of lead lets the agent
# catch an early drop without waking for shows still quarters away.
LEAD = timedelta(days=7)

# Highest watchlist.json `schema_version` whose versioned fields this
# reader understands (contract:
# skills/check-watchlist/state-schema.md). An absent stamp is legacy
# pre-v1 and reads as v1.
SUPPORTED_SCHEMA_VERSION = 1

_QUARTER_START_MONTH = {1: 1, 2: 4, 3: 7, 4: 10}
_QUARTER_RE = re.compile(r"(\d{4})-[Qq]([1-4])")
_MONTH_RE = re.compile(r"(\d{4})-(0[1-9]|1[0-2])")
_YEAR_RE = re.compile(r"\d{4}")

# How long a resolved entry stays answered, by the precision of its
# `expected` value. A dated release is re-asked every fire; a bare year
# (or an `expected` we can't parse) is re-asked monthly, which is what
# stops the same never-due set from waking the agent 365 nights a year
# without narrowing the window it is checked in.
_RECHECK_INTERVALS = {
    "day": timedelta(days=1),
    "month": timedelta(days=7),
    "quarter": timedelta(days=14),
    "year": timedelta(days=30),
    "unparseable": timedelta(days=30),
}


def _parse_expected(expected: object) -> tuple[str, date | None]:
    """(precision, window start) for a fuzzy `expected` value.

    Precision is `day`, `month`, `quarter`, `year`, or `unparseable`;
    the window start is the earliest plausible release date, None when
    the value can't be anchored.
    """
    if not isinstance(expected, str):
        return "unparseable", None
    value = expected.strip()
    try:
        return "day", date.fromisoformat(value)
    except ValueError:
        pass
    quarter = _QUARTER_RE.fullmatch(value)
    if quarter:
        year = int(quarter.group(1))
        return "quarter", date(year, _QUARTER_START_MONTH[int(quarter.group(2))], 1)
    month = _MONTH_RE.fullmatch(value)
    if month:
        return "month", date(int(month.group(1)), int(month.group(2)), 1)
    if _YEAR_RE.fullmatch(value):
        return "year", date(int(value), 1, 1)
    return "unparseable", None


def _window_start(expected: object) -> date | None:
    """Earliest plausible release date for a fuzzy `expected` value.

    Returns None when the value can't be anchored to a date; callers
    treat that as conservatively due and wake.
    """
    return _parse_expected(expected)[1]


def _recheck_interval(expected: object) -> timedelta:
    return _RECHECK_INTERVALS[_parse_expected(expected)[0]]


def _prior_state_readable(payload: dict) -> bool:
    """Whether this reader may interpret the record's versioned fields.

    A record stamped newer than `SUPPORTED_SCHEMA_VERSION` belongs to a
    writer this reader does not know; per stateful-artifacts it is no
    usable prior state, not something to migrate or guess at."""
    version = payload.get("schema_version")
    if version is None:
        return True
    return (
        isinstance(version, int)
        and not isinstance(version, bool)
        and version <= SUPPORTED_SCHEMA_VERSION
    )


def _next_recheck(entry: dict, today: date) -> date | None:
    """Date this entry becomes re-askable, or None when it is askable
    now. `released` is never suppressed — while `notified` is false that
    alert has not been delivered."""
    if entry.get("last_verdict") not in ("unreleased", "unknown"):
        return None
    stamp = entry.get("last_checked")
    if not isinstance(stamp, str):
        return None
    try:
        checked_on = date.fromisoformat(stamp.strip())
    except ValueError:
        return None
    next_due = checked_on + _recheck_interval(entry.get("expected"))
    return next_due if next_due > today else None


def decide(now_utc: datetime, watchlist_path: Path) -> dict:
    try:
        text = watchlist_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"wake_agent": False, "data": {"reason": "file_missing"}}
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "wake_agent": False,
            "data": {"reason": "file_unreadable", "error": str(exc)},
        }

    try:
        payload = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        return {
            "wake_agent": False,
            "data": {"reason": "file_unreadable", "error": f"JSON malformed: {exc}"},
        }

    if not isinstance(payload, dict):
        return {"wake_agent": False, "data": {"reason": "tracking_missing"}}

    tracking = payload.get("tracking")
    if not isinstance(tracking, list):
        return {"wake_agent": False, "data": {"reason": "tracking_missing"}}

    unnotified = [
        entry for entry in tracking if isinstance(entry, dict) and entry.get("notified") is False
    ]
    if not unnotified:
        return {
            "wake_agent": False,
            "data": {"reason": "all_notified", "tracking_count": len(tracking)},
        }

    today = now_utc.date()
    cutoff = today + LEAD
    prior_state_readable = _prior_state_readable(payload)
    due_titles: list[str] = []
    due_count = 0
    windows: list[date] = []
    rechecks: list[date] = []
    for entry in unnotified:
        title = entry.get("title")
        window = _window_start(entry.get("expected"))
        if window is not None and window > cutoff:
            windows.append(window)
            continue
        next_recheck = _next_recheck(entry, today) if prior_state_readable else None
        if next_recheck is not None:
            rechecks.append(next_recheck)
            continue
        due_count += 1
        if isinstance(title, str):
            due_titles.append(title)

    if due_count:
        due: dict = {
            "reason": "release_due",
            "due_count": due_count,
            "titles": due_titles,
            "lead_days": LEAD.days,
        }
        if not prior_state_readable:
            due["prior_state"] = "unreadable_schema_version"
        return {"wake_agent": True, "data": due}

    data: dict = {
        "reason": "within_recheck_backoff" if rechecks else "all_future",
        "unnotified_count": len(unnotified),
        "lead_days": LEAD.days,
    }
    if rechecks:
        data["backoff_count"] = len(rechecks)
        data["next_recheck"] = min(rechecks).isoformat()
    if windows:
        data["nearest_window"] = min(windows).isoformat()
    return {"wake_agent": False, "data": data}


def main() -> int:
    watchlist_path = Path(os.environ.get("CHECK_WATCHLIST_PATH", DEFAULT_WATCHLIST_PATH))
    now_utc = datetime.now(timezone.utc)
    payload = decide(now_utc, watchlist_path)
    sys.stdout.write(json.dumps(payload) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
