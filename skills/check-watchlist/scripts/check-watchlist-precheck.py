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
    a real release).

No-wake reasons:
  - `all_future`           — every unnotified show's release window is
    beyond the lead. The steady state for far-off tracked shows.
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
  - Bare year  `2026`       → Jan 1 of that year
  - anything else / missing → un-parseable → conservative wake

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

_QUARTER_START_MONTH = {1: 1, 2: 4, 3: 7, 4: 10}
_QUARTER_RE = re.compile(r"(\d{4})-[Qq]([1-4])")
_YEAR_RE = re.compile(r"\d{4}")


def _window_start(expected: object) -> date | None:
    """Earliest plausible release date for a fuzzy `expected` value.

    Returns None when the value can't be anchored to a date; callers
    treat that as conservatively due and wake.
    """
    if not isinstance(expected, str):
        return None
    value = expected.strip()
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    quarter = _QUARTER_RE.fullmatch(value)
    if quarter:
        year = int(quarter.group(1))
        month = _QUARTER_START_MONTH[int(quarter.group(2))]
        return date(year, month, 1)
    if _YEAR_RE.fullmatch(value):
        return date(int(value), 1, 1)
    return None


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

    cutoff = now_utc.date() + LEAD
    due_titles: list[str] = []
    due_count = 0
    windows: list[date] = []
    for entry in unnotified:
        title = entry.get("title")
        window = _window_start(entry.get("expected"))
        if window is None or window <= cutoff:
            due_count += 1
            if isinstance(title, str):
                due_titles.append(title)
        else:
            windows.append(window)

    if due_count:
        return {
            "wake_agent": True,
            "data": {
                "reason": "release_due",
                "due_count": due_count,
                "titles": due_titles,
                "lead_days": LEAD.days,
            },
        }

    data = {
        "reason": "all_future",
        "unnotified_count": len(unnotified),
        "lead_days": LEAD.days,
    }
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
