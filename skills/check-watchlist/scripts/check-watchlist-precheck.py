#!/usr/bin/env python3
"""Precheck for `tessl__check-watchlist`.

Decides whether the daily 09:30 UTC fire should wake the LLM at all by
reading `/workspace/group/watchlist.json` and counting entries where
`notified` is false.

Wake reasons:
  - `unnotified_present` — at least one tracked show has `notified: false`.

No-wake reasons:
  - `file_missing`         — watchlist.json absent (matches Step 1's
    "exit silently" contract).
  - `file_unreadable`      — IO error / non-UTF-8 / malformed JSON
    (silent skip per `jbaruch/nanoclaw#516` spec — the agent has
    nothing useful to do with a broken file).
  - `tracking_missing`     — JSON parsed but root has no `tracking`
    list (treat as empty).
  - `all_notified`         — every entry already has `notified: true`.

When wakes happen, `data` carries `unnotified_count` and `titles` so the
agent's first turn doesn't re-read the file.

Per `coding-policy: file-hygiene`: always exit 0 with valid JSON on
stdout. The scheduler reads the JSON's `wake_agent` boolean to decide
whether to invoke the LLM.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEFAULT_WATCHLIST_PATH = "/workspace/group/watchlist.json"


def decide(watchlist_path: Path) -> dict:
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

    titles = [entry.get("title") for entry in unnotified if isinstance(entry.get("title"), str)]
    return {
        "wake_agent": True,
        "data": {
            "reason": "unnotified_present",
            "unnotified_count": len(unnotified),
            "titles": titles,
        },
    }


def main() -> int:
    watchlist_path = Path(os.environ.get("CHECK_WATCHLIST_PATH", DEFAULT_WATCHLIST_PATH))
    payload = decide(watchlist_path)
    sys.stdout.write(json.dumps(payload) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
