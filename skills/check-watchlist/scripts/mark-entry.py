#!/usr/bin/env python3
"""Apply one watchlist bookkeeping mutation for `tessl__check-watchlist`.

SKILL.md used to direct the agent to read watchlist.json, find the
entry, edit the fields, and write the file back by hand, once per
delivered show plus a rollback path when a send failed. That is
deterministic work with known inputs and outputs — `coding-policy:
script-delegation` puts it in a script, and only here is it testable
against the failure orders that matter (a send that succeeded with a
write that did not, and a rollback write that fails in turn).

One invocation performs exactly one mutation on exactly one entry.
The request is a JSON file, never command-line text: a show title is
data from the wider web, and interpolating it into a shell command
would let an apostrophe break the invocation and `$(...)` do worse.
The caller writes the file, then names its path:

  mark-entry.py --input <path>

  {"title": "<title>", "action": "released", "released": "YYYY-MM-DD"}
      Delivered: `notified: true` + `released: <date>`.
  {"title": "<title>", "action": "cancelled"}
      Cancelled before release: `notified: true` + `cancelled: true`.
  {"title": "<title>", "action": "clear_stamps"}
      A send that failed: drop `last_checked`/`last_verdict` so the
      precheck's backoff cannot suppress the retry of an alert that was
      never delivered. `notified` is left false.

Entry matching is exact on the title after casefolding and whitespace
collapse — the same rule verify-release.py uses to resolve a title, so
the two agree on what "the entry named X" means. No match, or more than
one, is an error rather than a guess.

Version handling matches the owner contract in
skills/check-watchlist/state-schema.md: an unstamped record (legacy
pre-v1) is stamped on write, a record already at
`WATCHLIST_SCHEMA_VERSION` keeps it, and any other version is refused
untouched.

Idempotent: re-marking an entry that already carries the target state
reports `already_marked` and exits 0 without rewriting the file, so a
retried run cannot corrupt bookkeeping.

Environment
-----------
  CHECK_WATCHLIST_PATH — watchlist.json path (default
    /workspace/group/watchlist.json), shared with the other scripts.

Output
------
Single-line JSON on stdout.
  exit 0: {"status": "marked" | "already_marked" | "stamps_cleared"
              | "stamps_absent",
           "title": "<title>", "schema_version": 1}
  exit 1: {"error": "<what failed and what to do about it>"}
Exit 1 means the mutation did NOT land: the caller must surface the
error and stop rather than continue delivering shows it cannot record.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

DEFAULT_WATCHLIST_PATH = "/workspace/group/watchlist.json"

# Owner-side record version, matching verify-release.py. Any shape
# change bumps both plus skills/check-watchlist/state-schema.md.
WATCHLIST_SCHEMA_VERSION = 1

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip().casefold()


def _fail(message: str) -> int:
    """Structured payload on stdout for the skill, actionable diagnostic
    on stderr for the operator reading `task_run_logs`."""
    print(json.dumps({"error": message}))
    sys.stderr.write(f"mark-entry: {message}\n")
    return 1


def _load(path: Path) -> tuple[Any, str | None]:
    """(payload, error message)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, (
            f"{path} does not exist — nothing to mark; check the watchlist mount before rerunning"
        )
    except (OSError, UnicodeDecodeError) as exc:
        return None, (
            f"cannot read {path}: {exc} — restore the file or fix its read permissions, then rerun"
        )
    try:
        payload = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        return None, (
            f"{path} is not valid JSON: {exc} — repair or restore valid JSON at that path, "
            f"then rerun"
        )
    if not isinstance(payload, dict):
        return None, f"{path} root is not a JSON object — restore a valid watchlist record"
    return payload, None


def _version_error(payload: dict, path: Path) -> str | None:
    version = payload.get("schema_version")
    if version is None:
        return None
    if isinstance(version, int) and not isinstance(version, bool):
        if version == WATCHLIST_SCHEMA_VERSION:
            return None
    return (
        f"{path} is at schema_version {version!r}, not {WATCHLIST_SCHEMA_VERSION} — "
        f"this skill does not implement that shape and will not write to it; "
        f"upgrade the skill or restore a version {WATCHLIST_SCHEMA_VERSION} record"
    )


ACTIONS = ("released", "cancelled", "clear_stamps")


def _read_request(input_path: Path) -> tuple[dict | None, str | None]:
    """(request, error). The request carries the title as data, so no
    show title ever reaches a shell."""
    try:
        text = input_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"--input {input_path} does not exist — write the request JSON there first"
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"cannot read --input {input_path}: {exc} — fix its permissions, then rerun"
    try:
        request = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"--input {input_path} is not valid JSON: {exc}"
    if not isinstance(request, dict):
        return None, (f"--input {input_path} must hold a JSON object with `title` and `action`")
    title = request.get("title")
    if not isinstance(title, str) or not title.strip():
        return None, f"--input {input_path} has no usable `title`"
    action = request.get("action")
    if action not in ACTIONS:
        return None, (
            f"--input {input_path} has `action` {action!r} — use one of {', '.join(ACTIONS)}"
        )
    if action == "released":
        released = request.get("released")
        if not isinstance(released, str):
            return None, (
                f"--input {input_path} has action `released` but no `released` date — "
                f"pass the verifier's `premiere_date`, or today's UTC date when it is absent"
            )
        released_error = _released_error(released, input_path)
        if released_error is not None:
            return None, released_error
    return request, None


def _released_error(value: str, input_path: Path) -> str | None:
    """`released` is a `YYYY-MM-DD` date in the record contract, so a
    free-form string never reaches the file. The message names the
    request field the caller has to edit, not a flag: `released` arrives
    inside the `--input` file."""
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return (
            f"--input {input_path} has `released` {value!r}, which is not a "
            f"YYYY-MM-DD date — pass the verifier's `premiere_date`, or today's "
            f"UTC date when it is absent"
        )
    if parsed.isoformat() != value:
        return (
            f"--input {input_path} has `released` {value!r}, which is not canonical "
            f"YYYY-MM-DD — pass {parsed.isoformat()!r}"
        )
    return None


def _find(payload: dict, title: str, path: Path) -> tuple[dict | None, str | None]:
    tracking = payload.get("tracking")
    if not isinstance(tracking, list):
        return None, f"{path} has no `tracking` list — nothing to mark"
    wanted = _normalize(title)
    matches = [
        entry
        for entry in tracking
        if isinstance(entry, dict)
        and isinstance(entry.get("title"), str)
        and _normalize(entry["title"]) == wanted
    ]
    if not matches:
        return None, f"no watchlist entry titled {title!r} — check the title against the file"
    if len(matches) > 1:
        return None, (
            f"{len(matches)} watchlist entries are titled {title!r} — "
            f"de-duplicate the file before marking"
        )
    return matches[0], None


def _write(path: Path, payload: dict) -> str | None:
    """Atomic write: PID-suffixed temp beside the destination, then
    os.replace, so a crash mid-write never truncates watchlist.json."""
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError as exc:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        return (
            f"could not write {path}: {type(exc).__name__}: {exc} — the mutation did NOT land; "
            f"fix the destination's permissions or free space, then rerun"
        )
    return None


def _apply(entry: dict, request: dict) -> tuple[str, bool]:
    """(status, changed)."""
    action = request["action"]
    if action == "clear_stamps":
        had = entry.pop("last_checked", None) is not None
        had = entry.pop("last_verdict", None) is not None or had
        return ("stamps_cleared" if had else "stamps_absent", had)
    if action == "cancelled":
        if entry.get("notified") is True and entry.get("cancelled") is True:
            return "already_marked", False
        entry["notified"] = True
        entry["cancelled"] = True
        return "marked", True
    if entry.get("notified") is True and entry.get("released") == request["released"]:
        return "already_marked", False
    entry["notified"] = True
    entry["released"] = request["released"]
    return "marked", True


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        required=True,
        metavar="PATH",
        help="JSON file holding {title, action, released?}",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    path = Path(os.environ.get("CHECK_WATCHLIST_PATH", DEFAULT_WATCHLIST_PATH))

    request, request_error = _read_request(Path(args.input))
    if request is None:
        return _fail(request_error or f"cannot read --input {args.input}")

    payload, load_error = _load(path)
    if payload is None:
        return _fail(load_error or f"cannot load {path}")

    version_error = _version_error(payload, path)
    if version_error is not None:
        return _fail(version_error)

    title = request["title"]
    entry, find_error = _find(payload, title, path)
    if entry is None:
        return _fail(find_error or f"no watchlist entry titled {title!r}")

    status, changed = _apply(entry, request)
    if changed:
        payload["schema_version"] = WATCHLIST_SCHEMA_VERSION
        write_error = _write(path, payload)
        if write_error is not None:
            return _fail(write_error)

    print(
        json.dumps(
            {
                "status": status,
                "title": title,
                "schema_version": WATCHLIST_SCHEMA_VERSION,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
