---
name: entertainment-sync
description: "Weekly entertainment refresh: pulls Trakt watch history, checks watchlist for releases, runs show + book recs, syncs Audible purchases. Triggers: 'entertainment sync', 'weekly entertainment', 'sync trakt audible recs', 'refresh entertainment'."
cadence: "0 10 * * 0"
# agentModel renewal: no scanner tracks this pin. Re-check it on each new Claude model release and bump it in its own PR.
agentModel: "claude-sonnet-5-5"
script: "scripts/precheck-entertainment-sync.py"
---

# Entertainment Sync

Process steps in order. Do not skip ahead.

A failed step does not stop the run: surface it and continue. Track each step as ran or failed for Step 6.

A step failed when its script exits non-zero or its `Skill(...)` invocation fails, and also when the inner skill reports an operational error itself: it surfaced an error message, or its own closing line reports an error instead of a clean or surfaced run.

Run silently. Each inner skill handles its own surfacing. The wrapper adds no surfaces beyond cadence-cursor management.

Fire-time precheck (`scripts/precheck-entertainment-sync.py`) gates wake-ups by the weekly cadence cap. See `references/cadence-rationale.md`.

## Step 1 — Refresh Trakt watch history

Run the in-container fetch script (routes through the OneCLI gateway, writes `/workspace/group/trakt-history.json` on success):

```bash
python3 /home/node/.claude/skills/tessl__trakt-watch-history/scripts/trakt-watch-history.py
```

Silent on success. Report on error (stdout `{"error": ...}` / non-zero exit) or `total_shows: 0` (if sync hasn't run yet, skip silently).

If the script fails, surface its stdout/stderr verbatim via `mcp__nanoclaw__send_message`, mark Step 1 failed, and proceed to Step 2. Otherwise mark Step 1 ran and proceed to Step 2.

## Step 2 — Check watchlist

Invoke `Skill(skill: "tessl__check-watchlist")`. Inner skill notifies on released shows; silent otherwise.

If invocation fails, surface verbatim via `mcp__nanoclaw__send_message` and mark Step 2 failed. Proceed to Step 3.

## Step 3 — Show recommendations

Invoke `Skill(skill: "tessl__recommend-shows")` even when Step 1 failed. Sends on good matches; silent otherwise.

If invocation fails, surface via `mcp__nanoclaw__send_message` and mark Step 3 failed. Proceed to Step 4.

## Step 4 — Audible backup

Invoke `Skill(skill: "tessl__audible-backup")`. Inner skill reports new books or errors; silent otherwise.

If invocation fails, surface via `mcp__nanoclaw__send_message` and mark Step 4 failed. Proceed to Step 5.

## Step 5 — Book recommendations

Invoke `Skill(skill: "tessl__recommend-books")`. Sends on good matches; silent otherwise.

If invocation fails, surface via `mcp__nanoclaw__send_message` and mark Step 5 failed. Proceed to Step 6.

## Step 6 — Summarize the run

Run it with every step that ran cleanly:

```bash
python3 /home/node/.claude/skills/tessl__entertainment-sync/scripts/summarize-run.py \
  --date "$(date -u +%F)" --failed none
```

- Replace `none` with the failed step numbers as a comma list when any step failed, e.g. `--failed 1,3`
- Append `--surfaced` when any inner skill sent the operator something

Stdout: `{"advance_cursor": bool, "internal": "<internal>…</internal>"}`. Proceed to Step 7.

Exit 2 (bad date or step list): surface stderr inside a short Russian sentence, skip Step 7, and emit `<internal>entertainment-sync ran <utc_date>: partial — summary failed</internal>` as your final turn text. Finish here.

## Step 7 — Advance the success cursor

Run only when Step 6 returned `"advance_cursor": true`; otherwise proceed to Step 8 and leave the cursor unchanged.

```bash
python3 /home/node/.claude/skills/tessl__entertainment-sync/scripts/stamp-cursor.py
```

Atomic-writes `/workspace/group/state/entertainment-sync-cursor.json` with `{"schema_version": 1, "last_run": "<now UTC ISO Z>"}`. Stdout: `{"status": "stamped", "last_run": "<iso>", "cursor_path": "<path>"}`. Proceed to Step 8.

## Step 8 — Observable silence

Emit Step 6's `internal` line verbatim as your final turn text. The wrapper adds no user-facing output. Finish here.
