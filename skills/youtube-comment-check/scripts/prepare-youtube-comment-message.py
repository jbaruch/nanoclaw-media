#!/usr/bin/env python3
"""Fetch and compose one YouTube comment digest.

This executable owns the byte-preserving boundary between the existing
fetcher and composer. It invokes both with fixed argument vectors and
passes the fetcher's stdout bytes directly to the composer's stdin.

Success (exit 0): the composer's JSON object on stdout.

Fetcher or composer failure: no stdout, the child diagnostic on stderr,
and a non-zero exit. The caller must not send or stamp the cursor after a
non-zero exit.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
# Same cursor contract as the precheck and stamp-cursor.py (state-schema.md):
# the env override must reach the fetcher, or it reads a different cursor
# than the one Step 3 stamps.
DEFAULT_CURSOR_PATH = "/workspace/group/state/youtube-comment-check-cursor.json"
CURSOR_ENV = "YOUTUBE_COMMENT_CHECK_CURSOR"


def build_fetch_command(cursor_path: str) -> tuple[str, ...]:
    """Fixed fetcher argv, reading the given cursor."""
    return (
        sys.executable,
        str(SCRIPT_DIR / "fetch-youtube-comments.py"),
        "--channel-id",
        "UCZ8-VX2SiAIBE7guw7NG-Sg",
        "--days",
        "7",
        "--cursor",
        cursor_path,
        "--max-days",
        "35",
    )


def resolve_cursor_path() -> str:
    """The cursor path the precheck and stamp-cursor.py also resolve."""
    return os.environ.get(CURSOR_ENV, DEFAULT_CURSOR_PATH)


COMPOSER_COMMAND = (
    sys.executable,
    str(SCRIPT_DIR / "compose-youtube-comment-message.py"),
)


@dataclass(frozen=True)
class HandoffResult:
    """Captured result of the fetch-to-composer handoff."""

    returncode: int
    stdout: bytes
    stderr: bytes


def _remedy(stage: str) -> str:
    script = (
        "fetch-youtube-comments.py" if stage == "fetcher" else "compose-youtube-comment-message.py"
    )
    return (
        f"verify {SCRIPT_DIR / script} is installed and runs "
        "(reinstall jbaruch/nanoclaw-media if it is missing), then retry the check"
    )


def _child_failure(stage: str, returncode: int, stderr: bytes) -> HandoffResult:
    diagnostic = stderr
    if not diagnostic:
        diagnostic = (
            f"prepare-youtube-comment-message: {stage} exited with status {returncode} and no "
            f"diagnostic — {_remedy(stage)}\n"
        ).encode()
    return HandoffResult(returncode=returncode or 1, stdout=b"", stderr=diagnostic)


def _launch_failure(stage: str, exc: OSError) -> HandoffResult:
    """The child could not start at all: say why and what to do."""
    message = (
        f"prepare-youtube-comment-message: could not start the {stage}: {exc} — {_remedy(stage)}\n"
    )
    return HandoffResult(returncode=2, stdout=b"", stderr=message.encode())


def handoff(fetch_command: Sequence[str], composer_command: Sequence[str]) -> HandoffResult:
    """Run fetch then compose, preserving fetch stdout byte-for-byte."""
    try:
        fetched = subprocess.run(
            fetch_command,
            capture_output=True,
            check=False,
            shell=False,
        )
    except OSError as exc:
        return _launch_failure("fetcher", exc)

    if fetched.returncode != 0:
        return _child_failure("fetcher", fetched.returncode, fetched.stderr)

    try:
        composed = subprocess.run(
            composer_command,
            input=fetched.stdout,
            capture_output=True,
            check=False,
            shell=False,
        )
    except OSError as exc:
        return _launch_failure("composer", exc)

    if composed.returncode != 0:
        return _child_failure("composer", composed.returncode, composed.stderr)

    return HandoffResult(
        returncode=0,
        stdout=composed.stdout,
        stderr=fetched.stderr + composed.stderr,
    )


def main() -> int:
    result = handoff(build_fetch_command(resolve_cursor_path()), COMPOSER_COMMAND)
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
