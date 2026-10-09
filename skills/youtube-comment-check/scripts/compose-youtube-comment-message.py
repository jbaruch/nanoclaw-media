#!/usr/bin/env python3
"""Compose the YouTube comment digest from a fetch-result JSON object.

Reads one fetch-result object from stdin and writes one JSON object to
stdout. Pure transformation: no network, clock, cursor, sending, or
persistence.

Success (exit 0):
    {"comment_count": N, "message": "<html digest>" | null}

`comment_count == 0` is a quiet-week result: exit 0, `message` is null.

On malformed JSON or an invalid required shape: no stdout, an
actionable path-based diagnostic on stderr, non-zero exit.

Comment text is truncated to COMMENT_LIMIT Unicode code points before
HTML-escaping so the visible bound stays 100 characters without cutting
an escape entity. Untrusted display fields (title, author, text) escape
`<`, `>`, and `&` only (`html.escape(..., quote=False)`). The fixed
header markup and generated watch URL are left unchanged.
"""

from __future__ import annotations

import html
import json
import sys

COMMENT_LIMIT = 100


class ShapeError(ValueError):
    """Invalid fetch-result shape. `path` is a JSON pointer-style path."""

    def __init__(self, path: str, expected: str) -> None:
        self.path = path
        super().__init__(f"{path} {expected}")


def _require_str(value: object, path: str) -> str:
    if type(value) is not str:
        raise ShapeError(path, "must be a string")
    return value


def _format_video(video: dict, path: str) -> tuple[str, int]:
    title = _require_str(video.get("title"), f"{path}.title")
    url = _require_str(video.get("url"), f"{path}.url")
    comments = video.get("comments")
    if not isinstance(comments, list):
        raise ShapeError(f"{path}.comments", "must be an array")
    lines = [f"<b>{html.escape(title, quote=False)}</b>", url]
    rendered = 0
    for index, comment in enumerate(comments):
        if not isinstance(comment, dict):
            raise ShapeError(f"{path}.comments[{index}]", "must be an object")
        author = _require_str(comment.get("author"), f"{path}.comments[{index}].author")
        text = _require_str(comment.get("text"), f"{path}.comments[{index}].text")
        truncated = text[:COMMENT_LIMIT]
        lines.append(f"{html.escape(author, quote=False)}: {html.escape(truncated, quote=False)}")
        rendered += 1
    return "\n".join(lines), rendered


def compose(payload: object) -> dict:
    """Return the structured composer result for a parsed fetch-result object."""
    if not isinstance(payload, dict):
        raise ShapeError("$", "must be a JSON object")
    comment_count = payload.get("comment_count")
    if type(comment_count) is not int or comment_count < 0:
        raise ShapeError("$.comment_count", "must be a non-negative integer")
    videos = payload.get("videos")
    if not isinstance(videos, list):
        raise ShapeError("$.videos", "must be an array")
    blocks: list[str] = []
    rendered = 0
    for index, video in enumerate(videos):
        if not isinstance(video, dict):
            raise ShapeError(f"$.videos[{index}]", "must be an object")
        block, count = _format_video(video, f"$.videos[{index}]")
        rendered += count
        if count:
            blocks.append(block)
    if rendered != comment_count:
        raise ShapeError("$.comment_count", "must equal the number of comments")
    if comment_count == 0:
        return {"comment_count": 0, "message": None}
    header = f"🎬 <b>New YouTube comment(s)</b> ({comment_count})"
    return {"comment_count": comment_count, "message": header + "\n\n" + "\n\n".join(blocks)}


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        sys.stderr.write(
            "compose-youtube-comment-message: stdin is not valid UTF-8; "
            "pass one fetch-result object\n"
        )
        return 2
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        sys.stderr.write(
            "compose-youtube-comment-message: stdin is not valid JSON; "
            "pass one fetch-result object\n"
        )
        return 2
    try:
        result = compose(payload)
    except ShapeError as exc:
        sys.stderr.write(f"compose-youtube-comment-message: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
