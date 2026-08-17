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

Keep verification bounded and in-process. **Never spawn a subagent, and never reach the network with `curl`, `fetch_markdown`, or any page fetch.**

## Step 1 — Read watchlist

Read `/workspace/group/watchlist.json`. If the file doesn't exist, exit silently.

Note which shows have `notified: false`. An empty `tracking` array is not an exit: Step 2 still runs and stamps an unstamped record.

Proceed immediately to Step 2.

## Step 2 — Verify release status

Run the in-container verifier:

```bash
python3 /home/node/.claude/skills/tessl__check-watchlist/scripts/verify-release.py
```

It resolves a bounded batch of unnotified entries against a structured source under a hard wall-clock budget, stamps `last_checked`/`last_verdict` on the entries it resolved, and prints one JSON line: `results[]` of `{title, verdict, detail, premiere_date, platform, checked}` plus `stats`. Bounds, source, and matching rule: `skills/check-watchlist/scripts/verify-release.py` module docstring.

Act only on the entries in `results[]`. Entries counted by a non-zero `stats.skipped_over_cap` need no action here. Do not search for them in Step 3 and do not treat them as not-released.

Per verdict:
- **`released`** — the show is out on the platform the entry tracks. Carry `premiere_date` and `platform` into Step 4.
- **`unreleased`** — not out yet. Stay silent; the script already recorded the check.
- **`unknown`** — unresolved. Goes to Step 3. A `premiere_date` on a `platform_mismatch` or `platform_unverified` result is **never** grounds for an alert.

On a non-zero exit or an `{"error": ...}` payload, surface the script's stdout/stderr verbatim via `mcp__nanoclaw__send_message` and finish here.

A `write_skipped` field means the record is at a `schema_version` this skill cannot write. Surface it verbatim, deliver nothing, mark nothing, and finish here.

A `write_error` field is a warning, not a failure. Surface it verbatim via `mcp__nanoclaw__send_message` now, then continue — a run with no released shows sends nothing later to carry it.

An empty `results[]` means nothing needed checking. Finish here.

Proceed immediately to Step 3.

## Step 3 — Resolve unknowns

For each `unknown` result, run **one** `WebSearch` call, at most 3 across the whole run. Report any beyond the third as unresolved; do not silently drop them. Skip results whose `detail` is `title_missing`:

```
"[title]" release date [current year] [next year] [expected year, if different] streaming
```

Derive the years from the run date in UTC — never hardcode them.

Use `WebSearch` only. Do not open pages, do not spawn a subagent, do not shell out. Never substitute another tool for it. **If the `WebSearch` tool is not available in this container, skip this step entirely** and treat every `unknown` as not released.

Classify each searched title:
- **Released** — the search clearly shows it is out on the platform the entry tracks. Carry it into Step 4.
- **Cancelled** — the search clearly shows it was cancelled before release. Carry it into Step 5.
- **Not released** — everything else, a thin or ambiguous result included. Stay silent.

Proceed immediately to Step 4.

## Step 4 — Deliver each released show

Take the released shows one at a time. Never batch the bookkeeping to the end of the run.

Compose a short notification message (Telegram HTML format):
```
📺 <b>[Title]</b> is now available on [Platform]!
[1 sentence why Baruch will like it, from the `reason` field]
```

Send it via `mcp__nanoclaw__send_message` (standalone, not a reply — this is a proactive alert), then record the delivery. Write the request with the `Write` tool to `/tmp/check-watchlist-mark.json`, never by interpolating the title into a shell command:

```json
{"title": "<title>", "action": "released", "released": "<YYYY-MM-DD>"}
```

```bash
python3 /home/node/.claude/skills/tessl__check-watchlist/scripts/mark-entry.py \
  --input /tmp/check-watchlist-mark.json
```

Use the verifier's `premiere_date`, today's UTC date when it is absent. Exit 0 means the mutation landed. Every field the script edits, its matching rule, and its version handling: `skills/check-watchlist/scripts/mark-entry.py` module docstring.

**If the send fails**, rewrite the request with `"action": "clear_stamps"` and no `released`, run the same command, surface the send error verbatim, then continue with the remaining released shows.

**If either script call exits non-zero**, surface its `error` verbatim along with every title already delivered this run. Send no further shows and finish the skill here.

Once every released show is delivered and recorded, proceed immediately to Step 5.

## Step 5 — Mark cancelled shows

For each title Step 3 classified as cancelled, write the request to `/tmp/check-watchlist-mark.json` with the `Write` tool and run the script:

```json
{"title": "<title>", "action": "cancelled"}
```

```bash
python3 /home/node/.claude/skills/tessl__check-watchlist/scripts/mark-entry.py \
  --input /tmp/check-watchlist-mark.json
```

Do NOT notify Baruch. On the first non-zero exit, surface the script's `error` verbatim, mark no further titles, and finish the skill there. If Step 3 classified no title as cancelled, do nothing here.

Finish here. Shows that are not out stay silent and untouched beyond the verifier's own `last_checked` stamp.

## Notes
- The precheck date-gates the daily fire so this skill only wakes when a tracked show's release window is plausibly due, and rate-limits re-asking about an entry already resolved — see `skills/check-watchlist/scripts/check-watchlist-precheck.py` module docstring for the wake/no-wake contract, lead window, and backoff intervals.
- Field contract for the entries both scripts read and write: `skills/check-watchlist/state-schema.md`.
- Only notify for actual releases, not renewals or trailers
- New season announced without air date = "not yet released"
- Silence is default; only speak when a show is available
