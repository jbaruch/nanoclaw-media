---
name: check-watchlist
description: Checks tracked upcoming TV shows in watchlist.json and sends a Telegram notification message via MCP when any have been released. Use when running nightly release checks, monitoring streaming release dates, or checking whether new episodes or shows from a watchlist are now available to watch. Fires nightly via its own scheduled_tasks row (post-#404).
cadence: "30 9 * * *"
agentModel: "claude-haiku-4-5-20251001"
script: "scripts/check-watchlist-precheck.py"
---

# Check Watchlist Skill

Process steps in order. Do not skip ahead.

Monitors `/workspace/group/watchlist.json` for upcoming shows and notifies when they release.

This skill runs inside a maintenance slot the host reaps after 300s without SDK events. Verification is therefore bounded and in-process: **never spawn a subagent, and never reach the network with `curl`, `fetch_markdown`, or any page fetch** — that is what killed the run mid-verification in `jbaruch/nanoclaw-media#67`, losing the alert and re-arming the same check the next night.

## Step 1 — Read watchlist

Read `/workspace/group/watchlist.json`. If the file doesn't exist or `tracking` array is empty, exit silently.

Filter to shows where `notified: false` only. Proceed immediately to Step 2.

## Step 2 — Verify release status

Run the in-container verifier:

```bash
python3 /home/node/.claude/skills/tessl__check-watchlist/scripts/verify-release.py
```

It resolves every unnotified entry against a structured source under a hard wall-clock budget, stamps `last_checked`/`last_verdict` on the entries it resolved, and prints one JSON line: `results[]` of `{title, verdict, detail, premiere_date, platform, checked}` plus `stats`. Bounds, source, and matching rule: `skills/check-watchlist/scripts/verify-release.py` module docstring.

Per verdict:
- **`released`** — the show is out on the platform the entry tracks. Carry `premiere_date` and `platform` into Step 4.
- **`unreleased`** — not out yet. Stay silent; the script already recorded the check.
- **`unknown`** — unresolved. Goes to Step 3. A `platform_mismatch` detail means the show premiered somewhere the entry doesn't track (an original-country airing ahead of the international drop) — the `premiere_date` and `platform` on that result are the other channel's, never grounds for an alert.

On a non-zero exit or an `{"error": ...}` payload, surface the script's stdout/stderr verbatim via `mcp__nanoclaw__send_message` and finish here. A `write_error` field is a warning, not a failure — the verdicts still hold, so continue and mention it in Step 4's message.

Proceed immediately to Step 3.

## Step 3 — Resolve unknowns

For each `unknown` result, run **one** `WebSearch` call, at most 3 across the whole run (report any beyond the third as unresolved, do not silently drop them):

```
"[title]" release date [current year] [next year] [expected year, if different] streaming
```

Derive the years from the run date in UTC — never hardcode them.

Use `WebSearch` only. Do not open pages, do not spawn a subagent, do not shell out. If the search doesn't clearly show the title is out, treat it as not released and stay silent — a missed night is recoverable, a killed run is not.

Proceed immediately to Step 4.

## Step 4 — Deliver each released show

For each released show, complete all four sub-steps before moving to the next one. Batching the writes to the end is what made a mid-run kill lose everything.

1. Compose a short notification message (Telegram HTML format):
   ```
   📺 <b>[Title]</b> is now available on [Platform]!
   [1 sentence why Baruch will like it, from the `reason` field]
   ```
2. Send via `mcp__nanoclaw__send_message` (standalone, not a reply — this is a proactive alert)
3. Set `notified: true` and add `"released": "YYYY-MM-DD"` (the verifier's `premiere_date`, today's UTC date when absent)
4. Read the full watchlist.json, update only that entry, write the complete file back

Proceed immediately to Step 5.

## Step 5 — Mark cancelled shows

For a show a Step 3 search shows was cancelled before release: set `notified: true`, add `"cancelled": true`, write the file back. Do NOT notify Baruch — a cancelled show is not actionable.

Finish here. Shows that are not out stay silent and untouched beyond the verifier's own `last_checked` stamp.

## Notes
- The precheck date-gates the daily fire so this skill only wakes when a tracked show's release window is plausibly due, and rate-limits re-asking about an entry already resolved — see `skills/check-watchlist/scripts/check-watchlist-precheck.py` module docstring for the wake/no-wake contract, lead window, and backoff intervals.
- Field contract for the entries both scripts read and write: `skills/check-watchlist/state-schema.md`.
- Only notify for actual releases, not renewals or trailers
- New season announced without air date = "not yet released"
- Silence is default; only speak when a show is available
