"""Contract tests for the youtube-comment-check notification."""

from pathlib import Path

SKILL_PATH = Path(__file__).resolve().parents[1] / "skills" / "youtube-comment-check" / "SKILL.md"


def _report_step() -> str:
    skill = SKILL_PATH.read_text()
    start = skill.index("## Step 2 — Report new comments")
    end = skill.index("## Step 3 — Advance the success cursor", start)
    return skill[start:end]


def test_report_identifies_youtube_comments_before_video_blocks() -> None:
    step = _report_step()
    header = "🎬 <b>New YouTube comment(s)</b> ({comment_count})"

    assert header in step
    assert step.index(header) < step.index("video title + link")
    assert "Follow the header with a blank line" in step
