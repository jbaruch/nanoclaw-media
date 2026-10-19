#!/usr/bin/env python3
"""Aggregate an entertainment-sync run into its cursor decision and closing line.

Usage:
    summarize-run.py --date YYYY-MM-DD --failed STEPS [--surfaced]

`--failed` lists the failed step numbers (1-5) as a comma list, or `none`;
every other step counts as ran. `--surfaced` means at least one inner skill
sent the operator something.

Stdout, one JSON line:
    {"advance_cursor": bool, "internal": "<internal>entertainment-sync ran ...</internal>"}

`advance_cursor` is true only when no step failed. `internal` is the exact
line Step 8 emits: `clean` or `surfaced` for a full run, otherwise
`partial — ran <steps>; failed <steps>`.

`--date` must be the canonical `YYYY-MM-DD` form.

Exit 0 on success; 2 on a bad date or step list.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date

STEPS = (1, 2, 3, 4, 5)
CANONICAL_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def summarize(run_date: str, failed: list[int], surfaced: bool) -> dict:
    ran = [step for step in STEPS if step not in failed]
    if not failed:
        status = "surfaced" if surfaced else "clean"
    else:
        ran_list = ", ".join(str(step) for step in ran) or "none"
        failed_list = ", ".join(str(step) for step in sorted(failed))
        status = f"partial — ran {ran_list}; failed {failed_list}"
    return {
        "advance_cursor": not failed,
        "internal": f"<internal>entertainment-sync ran {run_date}: {status}</internal>",
    }


def parse_failed(raw: str) -> list[int]:
    if raw.strip() == "none":
        return []
    steps = []
    for part in raw.split(","):
        step = int(part.strip())
        if step not in STEPS:
            raise ValueError(f"step {step} is not one of {STEPS}")
        if step not in steps:
            steps.append(step)
    return steps


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Summarize an entertainment-sync run.")
    parser.add_argument("--date", required=True)
    parser.add_argument("--failed", required=True)
    parser.add_argument("--surfaced", action="store_true")
    args = parser.parse_args(argv[1:])
    try:
        if not CANONICAL_DATE.fullmatch(args.date):
            raise ValueError(f"--date {args.date!r} is not YYYY-MM-DD")
        date.fromisoformat(args.date)
        failed = parse_failed(args.failed)
    except ValueError as exc:
        sys.stderr.write(
            f"summarize-run: {exc}; pass --date YYYY-MM-DD and --failed as a comma list "
            "of steps 1-5 or `none`\n"
        )
        return 2
    print(json.dumps(summarize(args.date, failed, args.surfaced), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
