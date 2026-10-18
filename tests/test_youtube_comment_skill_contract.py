"""Outcome tests for youtube-comment-check preparation and composition.

Invokes the composer and the executable fetch-to-composer handoff.
Assertions cover rendered output, byte preservation, quiet behavior, and
fail-closed diagnostics rather than using SKILL.md prose as the digest
oracle.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSER = REPO_ROOT / "skills/youtube-comment-check/scripts/compose-youtube-comment-message.py"
HANDOFF = REPO_ROOT / "skills/youtube-comment-check/scripts/prepare-youtube-comment-message.py"
SKILL_PATH = REPO_ROOT / "skills/youtube-comment-check/SKILL.md"
SENTINEL_NAME = "composer-did-not-execute"


def _load_handoff():
    spec = importlib.util.spec_from_file_location("youtube_comment_handoff_under_test", HANDOFF)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {HANDOFF}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_script(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")


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


def test_handoff_passes_fetch_stdout_bytes_to_composer(tmp_path: Path) -> None:
    handoff = _load_handoff()
    fetch = tmp_path / "fetch.py"
    inspect_stdin = tmp_path / "inspect-stdin.py"
    raw = '{"comment_count":1,"videos":[],"text":"€ ` $() ; & < >\\n"}\n'.encode()
    _write_script(fetch, f"import sys\nsys.stdout.buffer.write({raw!r})\n")
    _write_script(
        inspect_stdin,
        "import json\n"
        "import sys\n"
        "data = sys.stdin.buffer.read()\n"
        "sys.stdout.write(json.dumps({'hex': data.hex()}) + '\\n')\n",
    )

    result = handoff.handoff(
        (sys.executable, str(fetch)),
        (sys.executable, str(inspect_stdin)),
    )

    assert result.returncode == 0
    assert result.stderr == b""
    assert json.loads(result.stdout) == {"hex": raw.hex()}


def test_handoff_success_returns_exact_composer_output(tmp_path: Path) -> None:
    handoff = _load_handoff()
    fetch = tmp_path / "fetch.py"
    fixture = _two_video_three_comment()
    raw = (json.dumps(fixture, ensure_ascii=False) + "\n").encode()
    _write_script(fetch, f"import sys\nsys.stdout.buffer.write({raw!r})\n")

    result = handoff.handoff(
        (sys.executable, str(fetch)),
        (sys.executable, str(COMPOSER)),
    )
    composed = _run(fixture)

    assert result.returncode == 0
    assert result.stderr == b""
    assert composed.returncode == 0
    assert result.stdout == composed.stdout.encode()


def test_fetch_failure_emits_no_sendable_payload_or_stamp(tmp_path: Path) -> None:
    handoff = _load_handoff()
    fetch = tmp_path / "fetch.py"
    composer = tmp_path / "composer.py"
    composer_ran = tmp_path / "composer-ran"
    cursor = tmp_path / "cursor.json"
    _write_script(fetch, "import sys\nsys.stderr.write('fetch failed\\n')\nsys.exit(7)\n")
    _write_script(composer, f"from pathlib import Path\nPath({str(composer_ran)!r}).touch()\n")

    result = handoff.handoff(
        (sys.executable, str(fetch)),
        (sys.executable, str(composer)),
    )

    assert result.returncode == 7
    assert result.stdout == b""
    assert result.stderr == b"fetch failed\n"
    assert not composer_ran.exists()
    assert not cursor.exists()


def test_composer_failure_emits_no_sendable_payload_or_stamp(tmp_path: Path) -> None:
    handoff = _load_handoff()
    fetch = tmp_path / "fetch.py"
    composer = tmp_path / "composer.py"
    cursor = tmp_path / "cursor.json"
    _write_script(fetch, "import sys\nsys.stdout.write('{bad json')\n")
    _write_script(
        composer,
        "import sys\n"
        "assert sys.stdin.buffer.read() == b'{bad json'\n"
        "sys.stderr.write('compose failed\\n')\n"
        "sys.exit(9)\n",
    )

    result = handoff.handoff(
        (sys.executable, str(fetch)),
        (sys.executable, str(composer)),
    )

    assert result.returncode == 9
    assert result.stdout == b""
    assert result.stderr == b"compose failed\n"
    assert not cursor.exists()


def test_skill_runs_handoff_before_send_and_stamp() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    step1_start = skill.index("## Step 1 — Prepare recent comments")
    step2_start = skill.index("## Step 2 — Report new comments", step1_start)
    step1 = skill[step1_start:step2_start]
    end = skill.index("## Step 3 — Advance the success cursor", step2_start)
    step2 = skill[step2_start:end]
    step3 = skill[end : skill.index("## Step 4 — Silence", end)]
    handoff = "scripts/prepare-youtube-comment-message.py"
    assert handoff in step1
    assert "Do not send a digest or run Step 3" in step1
    assert "scripts/compose-youtube-comment-message.py" not in step2
    assert "mcp__nanoclaw__send_message" in step2
    assert "message" in step2
    assert handoff not in step3
    assert "stamp-cursor.py" in step3
    assert "Do not run Step 3" in step2
    assert "🎬" not in step2


def test_fetcher_reads_the_overridden_cursor(monkeypatch, tmp_path: Path) -> None:
    """#82 review: the YOUTUBE_COMMENT_CHECK_CURSOR override reaches the
    fetcher, so it reads the same cursor Step 3 stamps."""
    handoff = _load_handoff()
    cursor = tmp_path / "cursor.json"
    monkeypatch.setenv("YOUTUBE_COMMENT_CHECK_CURSOR", str(cursor))
    command = handoff.build_fetch_command(handoff.resolve_cursor_path())
    assert command[command.index("--cursor") + 1] == str(cursor)


def test_fetcher_defaults_to_the_documented_cursor(monkeypatch) -> None:
    handoff = _load_handoff()
    monkeypatch.delenv("YOUTUBE_COMMENT_CHECK_CURSOR", raising=False)
    command = handoff.build_fetch_command(handoff.resolve_cursor_path())
    assert command[command.index("--cursor") + 1] == (
        "/workspace/group/state/youtube-comment-check-cursor.json"
    )


def test_silent_child_failure_names_status_and_next_step(tmp_path: Path) -> None:
    """#82 review: a child that dies without stderr still yields an
    actionable diagnostic carrying its exit status."""
    handoff = _load_handoff()
    silent = tmp_path / "silent.py"
    silent.write_text("import sys\nsys.exit(7)\n", encoding="utf-8")
    result = handoff.handoff((sys.executable, str(silent)), (sys.executable, "-c", "pass"))
    assert result.returncode == 7
    assert result.stdout == b""
    message = result.stderr.decode()
    assert "status 7" in message
    assert "retry the check" in message
