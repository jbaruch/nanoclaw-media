"""Outcome tests for youtube-comment-check digest composition.

Invokes `compose-youtube-comment-message.py` as a subprocess with
`shell=False` and the fetch-result JSON on stdin. Assertions cover the
rendered message, quiet path, and fail-closed diagnostics — not SKILL.md
prose order.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSER = REPO_ROOT / "skills/youtube-comment-check/scripts/compose-youtube-comment-message.py"
SKILL_PATH = REPO_ROOT / "skills/youtube-comment-check/SKILL.md"
SENTINEL_NAME = "composer-did-not-execute"


def _run(
    payload: object | str | bytes, *, tmp_path: Path | None = None
) -> subprocess.CompletedProcess:
    if isinstance(payload, bytes):
        raw_input: str | bytes = payload
        text = False
    elif isinstance(payload, str):
        raw_input = payload
        text = True
    else:
        raw_input = json.dumps(payload)
        text = True
    cwd = str(tmp_path) if tmp_path is not None else str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, str(COMPOSER)],
        input=raw_input,
        capture_output=True,
        text=text,
        shell=False,
        timeout=10,
        cwd=cwd,
    )


def _two_video_three_comment() -> dict:
    return {
        "window_days": 7,
        "window_source": "default",
        "comment_count": 3,
        "videos": [
            {
                "id": "vid1",
                "title": "Kotlin Coroutines",
                "url": "https://www.youtube.com/watch?v=vid1",
                "comments": [
                    {
                        "author": "Alice",
                        "text": "great talk",
                        "published_at": "2026-06-15T11:00:00Z",
                    },
                    {
                        "author": "Bob",
                        "text": "thanks!",
                        "published_at": "2026-06-15T11:01:00Z",
                    },
                ],
            },
            {
                "id": "vid2",
                "title": "Gradle Tips",
                "url": "https://www.youtube.com/watch?v=vid2",
                "comments": [
                    {
                        "author": "Carol",
                        "text": "subscribed",
                        "published_at": "2026-06-15T11:02:00Z",
                    }
                ],
            },
        ],
    }


def test_two_video_three_comment_digest_is_stable() -> None:
    proc = _run(_two_video_three_comment())
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    expected = (
        "🎬 <b>New YouTube comment(s)</b> (3)\n"
        "\n"
        "<b>Kotlin Coroutines</b>\n"
        "https://www.youtube.com/watch?v=vid1\n"
        "Alice: great talk\n"
        "Bob: thanks!\n"
        "\n"
        "<b>Gradle Tips</b>\n"
        "https://www.youtube.com/watch?v=vid2\n"
        "Carol: subscribed"
    )
    assert payload == {"comment_count": 3, "message": expected}
    assert payload["message"].count("🎬 <b>New YouTube comment(s)</b>") == 1
    header, body = payload["message"].split("\n\n", 1)
    assert header == "🎬 <b>New YouTube comment(s)</b> (3)"
    assert body.startswith("<b>Kotlin Coroutines</b>\n")


def test_escapes_untrusted_fields_and_truncates_before_escaping(tmp_path: Path) -> None:
    raw_text = "a" * 90 + "<tag>&x" + "b" * 20
    fixture = {
        "comment_count": 1,
        "videos": [
            {
                "title": 'Title <b> & "quotes" `ticks` $() ; \nnewline',
                "url": "https://www.youtube.com/watch?v=vidx",
                "comments": [
                    {
                        "author": "Eve <script> & ` $() ;",
                        "text": raw_text,
                    }
                ],
            }
        ],
    }
    proc = _run(fixture, tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    message = payload["message"]
    assert payload["comment_count"] == 1
    assert message.count("🎬 <b>New YouTube comment(s)</b> (1)") == 1
    assert "&lt;b&gt;" in message
    assert "&lt;tag&gt;" in message
    assert "&amp;" in message
    assert "<script>" not in message
    assert "&lt;b&gt;New YouTube" not in message
    truncated = raw_text[:100]
    assert html_escaped_comment(truncated) in message
    assert html_escaped_comment(raw_text) not in message
    assert not (tmp_path / SENTINEL_NAME).exists()


def html_escaped_comment(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def test_unicode_truncation_is_code_points_before_escaping() -> None:
    raw_text = "你" * 101
    fixture = {
        "comment_count": 1,
        "videos": [
            {
                "title": "Talk",
                "url": "https://www.youtube.com/watch?v=vidu",
                "comments": [{"author": "Ming", "text": raw_text}],
            }
        ],
    }
    proc = _run(fixture)
    assert proc.returncode == 0, proc.stderr
    message = json.loads(proc.stdout)["message"]
    assert "Ming: " + ("你" * 100) in message
    assert "Ming: " + ("你" * 101) not in message


def test_one_comment_keeps_comment_s_wording() -> None:
    fixture = {
        "comment_count": 1,
        "videos": [
            {
                "title": "One",
                "url": "https://www.youtube.com/watch?v=vid1",
                "comments": [{"author": "Ada", "text": "hi"}],
            }
        ],
    }
    proc = _run(fixture)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["comment_count"] == 1
    assert payload["message"].startswith("🎬 <b>New YouTube comment(s)</b> (1)\n\n")
    assert payload["message"].count("🎬 <b>New YouTube comment(s)</b>") == 1


def test_zero_comments_exits_zero_with_no_sendable_message() -> None:
    proc = _run({"comment_count": 0, "videos": []})
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload == {"comment_count": 0, "message": None}


def test_malformed_json_exits_nonzero_with_empty_stdout() -> None:
    proc = _run("{not json")
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "stdin is not valid JSON" in proc.stderr
    assert "{not json" not in proc.stderr


def test_invalid_shapes_name_path_and_omit_raw_values() -> None:
    cases = [
        (True, "$"),
        ({"comment_count": True, "videos": []}, "$.comment_count"),
        ({"comment_count": -1, "videos": []}, "$.comment_count"),
        ({"comment_count": 0}, "$.videos"),
        (
            {
                "comment_count": 1,
                "videos": [
                    {
                        "title": "T",
                        "url": "https://www.youtube.com/watch?v=v",
                        "comments": [],
                    }
                ],
            },
            "$.comment_count",
        ),
        (
            {
                "comment_count": 1,
                "videos": {
                    "title": "T",
                    "url": "https://www.youtube.com/watch?v=v",
                    "comments": [{"author": "A", "text": "x"}],
                },
            },
            "$.videos",
        ),
        (
            {
                "comment_count": 1,
                "videos": [
                    {
                        "title": 1,
                        "url": "https://www.youtube.com/watch?v=v",
                        "comments": [{"author": "A", "text": "x"}],
                    }
                ],
            },
            "$.videos[0].title",
        ),
        (
            {
                "comment_count": 1,
                "videos": [
                    {
                        "title": "T",
                        "url": "https://www.youtube.com/watch?v=v",
                        "comments": {"author": "A", "text": "x"},
                    }
                ],
            },
            "$.videos[0].comments",
        ),
        (
            {
                "comment_count": 1,
                "videos": [
                    {
                        "title": "T",
                        "url": "https://www.youtube.com/watch?v=v",
                        "comments": [{"author": "A", "text": ["x"]}],
                    }
                ],
            },
            "$.videos[0].comments[0].text",
        ),
    ]
    attacker = "<script>alert(1)</script>"
    for payload, path in cases:
        if payload is True:
            proc = _run(True)
        else:
            proc = _run(payload)
        assert proc.returncode != 0, path
        assert proc.stdout == ""
        assert path in proc.stderr
        assert attacker not in proc.stderr
        dumped = json.dumps(payload)
        if "<" in dumped:
            assert dumped not in proc.stderr


def test_skill_step2_invokes_composer_and_routes_nonempty_message() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    start = skill.index("## Step 2 — Report new comments")
    end = skill.index("## Step 3 — Advance the success cursor", start)
    step2 = skill[start:end]
    step3 = skill[end : skill.index("## Step 4 — Silence", end)]
    composer = "scripts/compose-youtube-comment-message.py"
    assert composer in step2
    assert "mcp__nanoclaw__send_message" in step2
    assert "message" in step2
    assert composer not in step3
    assert "stamp-cursor.py" in step3
    assert "Do NOT advance the cursor" in step2
    assert "🎬" not in step2
