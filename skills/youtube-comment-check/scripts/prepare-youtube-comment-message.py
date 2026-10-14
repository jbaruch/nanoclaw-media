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

import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
FETCH_COMMAND = (
    sys.executable,
    str(SCRIPT_DIR / "fetch-youtube-comments.py"),
    "--channel-id",
    "UCZ8-VX2SiAIBE7guw7NG-Sg",
    "--days",
    "7",
    "--cursor",
    "/workspace/group/state/youtube-comment-check-cursor.json",
    "--max-days",
    "35",
)
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


def _child_failure(stage: str, returncode: int, stderr: bytes) -> HandoffResult:
    diagnostic = stderr
    if not diagnostic:
        diagnostic = (
            f"prepare-youtube-comment-message: {stage} exited without a diagnostic\n".encode()
        )
    return HandoffResult(returncode=returncode or 1, stdout=b"", stderr=diagnostic)


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
        return _child_failure("fetcher", 2, f"prepare-youtube-comment-message: {exc}\n".encode())

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
        return _child_failure("composer", 2, f"prepare-youtube-comment-message: {exc}\n".encode())

    if composed.returncode != 0:
        return _child_failure("composer", composed.returncode, composed.stderr)

    return HandoffResult(
        returncode=0,
        stdout=composed.stdout,
        stderr=fetched.stderr + composed.stderr,
    )


def main() -> int:
    result = handoff(FETCH_COMMAND, COMPOSER_COMMAND)
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
