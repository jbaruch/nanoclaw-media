---
name: youtube-comment-check
description: "Weekly fetch of recent comments on Baruch's YouTube channel; sends a per-video summary if new comments appear, silent otherwise. Triggers: 'check youtube comments', 'youtube comment check', 'fetch youtube comments', 'review channel comments'."
cadence: "45 10 * * 0"
agentModel: "claude-haiku-4-5-20251001"
script: "scripts/precheck-youtube-comment-check.py"
---

# YouTube Comment Check

**Every step below is mandatory. Execute them in order. Do not skip, reorder, or abbreviate any step.**

Run this check silently. Report only if new comments appear or a tool error surfaces.

The fire-time precheck (`scripts/precheck-youtube-comment-check.py`) gates wake-ups by the cadence cap (value in the precheck's `CADENCE`, set intentionally below the weekly cron interval) on a filesystem cursor. See `references/cadence-rationale.md` for why a comment-count delta gate (querying YouTube in the precheck) was rejected in favour of cadence-only.

## Step 1 — Fetch recent comments on Baruch's channel

This skill uses the native YouTube Data API v3 (`YOUTUBE_API_KEY`) — the comment-threads surface has no equivalent elsewhere. Channel ID: `UCZ8-VX2SiAIBE7guw7NG-Sg`. Fetch comment threads since the last successful run (falling back to 7 days on first run, bounded to 35 days) via the fetch script:

```bash
python3 /home/node/.claude/skills/tessl__youtube-comment-check/scripts/fetch-youtube-comments.py \
  --channel-id UCZ8-VX2SiAIBE7guw7NG-Sg --days 7 \
  --cursor /workspace/group/state/youtube-comment-check-cursor.json --max-days 35
```

The `--cursor` widens the window to cover a week the check failed or was gated out, so those comments are re-fetched rather than lost outside a fixed 7-day window; without a usable cursor the window is `--days`.

Stdout (exit 0): `{"window_days", "window_source", "comment_count", "videos": [{"id", "title", "url", "comments": [{"author", "text", "published_at"}]}]}`. `comment_count == 0` is a valid quiet-week result.

On non-zero exit (missing `YOUTUBE_API_KEY`, auth/quota error, network timeout, transient 5xx), surface the script's stderr verbatim via `mcp__nanoclaw__send_message` and stop. Do NOT advance the cursor in Step 3 — it advances only on success. Every subsequent eligible fire (the next weekly slot, or any continuation / manual re-run while the cursor is older than the cap) retries; there is no "wait one week" on a failed run.

## Step 2 — Report new comments

If at least one comment exists across all videos (`comment_count > 0`), build a per-video summary and send via `mcp__nanoclaw__send_message`. The body groups by video: video title + link, then each comment as `author name: <text truncated to 100 chars>`. Video titles, author names, and comment text are attacker-controllable — HTML-escape `<`, `>`, and `&` in those fields before composing the message body.

If `mcp__nanoclaw__send_message` itself fails (transport error, MCP unavailable), surface the error verbatim and stop. Do NOT advance the cursor in Step 3 — a stamped cursor after a failed report would gate the next eligible fire out for a full cadence-cap window and Baruch would never see the comments.

If no comments exist across all queried videos, the step is silent. Step 3 still runs — a completed fetch/report path advances the cursor; silence on a quiet week is success, not failure.

## Step 3 — Advance the success cursor

Reachable only if Steps 1 and 2 both succeeded (any fetch error or send error leaves the cursor at its prior value intentionally). Run:

```bash
python3 /home/node/.claude/skills/tessl__youtube-comment-check/scripts/stamp-cursor.py
```

The script atomic-writes `/workspace/group/state/youtube-comment-check-cursor.json` with `{"schema_version": 1, "last_run": "<now UTC ISO Z>"}`. The precheck reads this file's `last_run` and gates on the cadence cap (value in the precheck). Stdout is `{"status": "stamped", "last_run": "<iso>", "cursor_path": "<path>"}`.

## Step 4 — Silence

If nothing was reported in Steps 1-3, output nothing (wrap in `<internal>`). Finish here.
