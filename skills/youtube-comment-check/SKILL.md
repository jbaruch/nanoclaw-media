---
name: youtube-comment-check
description: "Weekly fetch of recent comments on Baruch's YouTube channel; sends a per-video summary if new comments appear, silent otherwise. Triggers: 'check youtube comments', 'youtube comment check', 'fetch youtube comments', 'review channel comments'."
cadence: "45 10 * * 0"
# agentModel renewal: no scanner tracks this pin. Re-check it on each new Claude model release and bump it in its own PR.
agentModel: "claude-haiku-4-5-20251001"
script: "scripts/precheck-youtube-comment-check.py"
---

# YouTube Comment Check

**Every step below is mandatory. Execute them in order. Do not skip, reorder, or abbreviate any step.**

Run this check silently. Report only if new comments appear or a tool error surfaces.

The fire-time precheck (`scripts/precheck-youtube-comment-check.py`) gates wake-ups by the cadence cap (value in the precheck's `CADENCE`, set intentionally below the weekly cron interval) on a filesystem cursor. See `references/cadence-rationale.md` for why a comment-count delta gate (querying YouTube in the precheck) was rejected in favour of cadence-only.

## Step 1 — Prepare recent comments from Baruch's channel

The preparation script uses the native YouTube Data API v3 (`YOUTUBE_API_KEY`). Channel ID: `UCZ8-VX2SiAIBE7guw7NG-Sg`. It fetches comment threads since the last successful run, falling back to 7 days on first run and bounding the lookback to 35 days:

```bash
python3 /home/node/.claude/skills/tessl__youtube-comment-check/scripts/prepare-youtube-comment-message.py
```

The preparation script owns the fixed fetch invocation and the byte-preserving fetch-to-composer handoff. See `scripts/prepare-youtube-comment-message.py` for its process contract.

Stdout (exit 0): `{"comment_count": N, "message": "<html>" | null}`. `message` is null when `comment_count == 0`.

On non-zero exit, surface the script's stderr verbatim via `mcp__nanoclaw__send_message` and stop. Do not send a digest or run Step 3.

## Step 2 — Report new comments

Parse the Step 1 stdout object. If `message` is a non-empty string, send that exact string via `mcp__nanoclaw__send_message`. Do not reformat or decode it.

On invalid Step 1 stdout or `mcp__nanoclaw__send_message` failure, surface the error verbatim and stop. Do not run Step 3.

If `message` is null or empty, do not send. Proceed immediately to Step 3.

## Step 3 — Advance the success cursor

Run Step 3 only after Steps 1 and 2 succeed:

```bash
python3 /home/node/.claude/skills/tessl__youtube-comment-check/scripts/stamp-cursor.py
```

The script atomic-writes `/workspace/group/state/youtube-comment-check-cursor.json` with `{"schema_version": 1, "last_run": "<now UTC ISO Z>"}`. The precheck reads this file's `last_run` and gates on the cadence cap (value in the precheck). Stdout is `{"status": "stamped", "last_run": "<iso>", "cursor_path": "<path>"}`.

## Step 4 — Silence

If nothing was reported in Steps 1-3, output nothing (wrap in `<internal>`). Finish here.
