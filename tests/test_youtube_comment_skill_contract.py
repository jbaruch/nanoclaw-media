"""Contract tests for the youtube-comment-check notification."""

from pathlib import Path

SKILL_PATH = Path(__file__).resolve().parents[1] / "skills" / "youtube-comment-check" / "SKILL.md"


def _report_step() -> str:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    start = skill.index("## Step 2 — Report new comments")
    end = skill.index("## Step 3 — Advance the success cursor", start)
    return skill[start:end]


def test_report_identifies_youtube_comments_before_video_blocks() -> None:
    step = _report_step()
    report_paragraph = step.split("\n\n", 2)[1]
    header = "🎬 <b>New YouTube comment(s)</b> (N)"
    header_index = report_paragraph.index(header)
    count_index = report_paragraph.index("`comment_count`", header_index)
    blank_line_index = report_paragraph.index("blank line", header_index)
    video_block_index = report_paragraph.index("video title + link")
    header_directive = report_paragraph[:header_index]
    count_binding = report_paragraph[header_index:count_index]

    assert step.count(header) == 1
    assert "{comment_count}" not in report_paragraph
    assert "exactly one" in header_directive
    assert "header line" in header_directive
    assert "fetch result" in count_binding
    assert "total" in count_binding
    assert header_index < count_index < blank_line_index < video_block_index
