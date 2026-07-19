---
name: entertainment-sync
description: "Weekly entertainment refresh: pulls Trakt watch history, checks watchlist for releases, runs show + book recs, syncs Audible purchases. Triggers: 'entertainment sync', 'weekly entertainment', 'sync trakt audible recs', 'refresh entertainment'."
cadence: "0 10 * * 0"
agentModel: "claude-sonnet-4-6"
script: "scripts/precheck-entertainment-sync.py"
---

# Entertainment Sync

**Every step below is mandatory. Execute them in order. Do not skip, reorder, or abbreviate any step.**

Run silently. Each inner skill handles its own surfacing. The wrapper adds no surfaces beyond cadence-cursor management.

Fire-time precheck (`scripts/precheck-entertainment-sync.py`) gates wake-ups by the weekly cadence cap. See `references/cadence-rationale.md`.

## Step 1 — Refresh Trakt watch history

Run the in-container fetch script (routes through the OneCLI gateway, writes `/workspace/group/trakt-history.json` on success):

```bash
python3 /home/node/.claude/skills/tessl__trakt-watch-history/scripts/trakt-watch-history.py
```

Silent on success. Report on error (stdout `{"error": ...}` / non-zero exit) or `total_shows: 0` (if sync hasn't run yet, skip silently).

If the script fails, surface its stdout/stderr verbatim via `mcp__nanoclaw__send_message`, then emit `<internal>entertainment-sync exited step-1: fetch-fail</internal>` as your final turn text and stop. Do NOT advance the cursor. Otherwise proceed to Step 2.

## Step 2 — Check watchlist

Invoke `Skill(skill: "tessl__check-watchlist")`. Inner skill notifies on released shows; silent otherwise.

If invocation fails, surface verbatim via `mcp__nanoclaw__send_message`, then emit `<internal>entertainment-sync exited step-2: inner-skill-fail</internal>` as your final turn text and stop. Do NOT advance the cursor. Otherwise proceed to Step 3.

## Step 3 — Show recommendations

Invoke `Skill(skill: "tessl__recommend-shows")`. Uses `trakt-history.json` from Step 1. Sends on good matches; silent otherwise.

If invocation fails, surface via `mcp__nanoclaw__send_message`, then emit `<internal>entertainment-sync exited step-3: inner-skill-fail</internal>` as your final turn text and stop. Otherwise proceed to Step 4.

## Step 4 — Audible backup

Invoke `Skill(skill: "tessl__audible-backup")`. Inner skill reports new books or errors; silent otherwise.

If invocation fails, surface via `mcp__nanoclaw__send_message`, then emit `<internal>entertainment-sync exited step-4: inner-skill-fail</internal>` as your final turn text and stop. Otherwise proceed to Step 5.

## Step 5 — Book recommendations

Invoke `Skill(skill: "tessl__recommend-books")`. Sends on good matches; silent otherwise.

If invocation fails, surface via `mcp__nanoclaw__send_message`, then emit `<internal>entertainment-sync exited step-5: inner-skill-fail</internal>` as your final turn text and stop. Otherwise proceed to Step 6.

## Step 6 — Advance the success cursor

Reachable only if Steps 1-5 all succeeded. Run:

```bash
python3 /home/node/.claude/skills/tessl__entertainment-sync/scripts/stamp-cursor.py
```

Atomic-writes `/workspace/group/state/entertainment-sync-cursor.json` with `{"schema_version": 1, "last_run": "<now UTC ISO Z>"}`. Stdout: `{"status": "stamped", "last_run": "<iso>", "cursor_path": "<path>"}`. Proceed to Step 7.

## Step 7 — Observable silence

Emit exactly one `<internal>` line so the post-fire silent-success watchdog can read `task_run_logs.result` directly. Either `<internal>entertainment-sync ran <utc_date>: clean</internal>` (no inner skill surfaced) or `<internal>entertainment-sync ran <utc_date>: surfaced</internal>` (at least one did). `<utc_date>` is today's UTC date in `YYYY-MM-DD`. The wrapper adds no user-facing output. Finish here.
