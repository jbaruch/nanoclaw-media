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

Note which shows have `notified: false` — those are the ones Step 2's verdicts will cover. An empty `tracking` array is not an exit: Step 2 still runs, and stamps a record that carries no `schema_version` yet.

Proceed immediately to Step 2.

## Step 2 — Verify release status

Run the in-container verifier:

```bash
python3 /home/node/.claude/skills/tessl__check-watchlist/scripts/verify-release.py
```

It resolves a bounded batch of unnotified entries against a structured source under a hard wall-clock budget, stamps `last_checked`/`last_verdict` on the entries it resolved, and prints one JSON line: `results[]` of `{title, verdict, detail, premiere_date, platform, checked}` plus `stats`. Bounds, source, and matching rule: `skills/check-watchlist/scripts/verify-release.py` module docstring.

Act only on the entries in `results[]`. A non-zero `stats.skipped_over_cap` counts entries the run did not reach; they carry no verdict and need no action here — they are unstamped, so the next fire leads with them. Do not search for them in Step 3 and do not treat them as not-released.

Per verdict:
- **`released`** — the show is out on the platform the entry tracks. Carry `premiere_date` and `platform` into Step 4.
- **`unreleased`** — not out yet. Stay silent; the script already recorded the check.
- **`unknown`** — unresolved. Goes to Step 3. Two details carry a premiere date that is **not** grounds for an alert: `platform_mismatch` (the show premiered on a service the entry doesn't track — an original-country airing ahead of the international drop) and `platform_unverified` (the source names no channel, so nothing corroborates the entry's platform).

On a non-zero exit or an `{"error": ...}` payload, surface the script's stdout/stderr verbatim via `mcp__nanoclaw__send_message` and finish here.

A `write_skipped` field means the record is at a `schema_version` this skill cannot write. Surface it verbatim and finish here — deliver nothing and mark nothing.

A `write_error` field is a warning, not a failure — the verdicts still hold. Continue, and mention it in Step 4's message.

An empty `results[]` means nothing needed checking. Finish here.

Proceed immediately to Step 3.

## Step 3 — Resolve unknowns

For each `unknown` result, run **one** `WebSearch` call, at most 3 across the whole run (report any beyond the third as unresolved, do not silently drop them). Skip results whose `detail` is `title_missing` — those entries carry no title to search for, and the verifier already recorded them:

```
"[title]" release date [current year] [next year] [expected year, if different] streaming
```

Derive the years from the run date in UTC — never hardcode them.

Use `WebSearch` only. Do not open pages, do not spawn a subagent, do not shell out. **If the `WebSearch` tool is not available in this container, skip this step entirely** — treat every `unknown` as not released and stay silent. Never substitute another tool for it.

Classify each searched title:
- **Released** — the search clearly shows it is out on the platform the entry tracks. Carry it into Step 4.
- **Cancelled** — the search clearly shows it was cancelled before release. Carry it into Step 5.
- **Not released** — everything else, a thin or ambiguous result included. Stay silent.

Proceed immediately to Step 4.

## Step 4 — Deliver each released show

Take the released shows one at a time. For each, in order: compose, send, mark, write. Never batch the writes to the end of the run.

- Compose a short notification message (Telegram HTML format):
  ```
  📺 <b>[Title]</b> is now available on [Platform]!
  [1 sentence why Baruch will like it, from the `reason` field]
  ```
- Send via `mcp__nanoclaw__send_message` (standalone, not a reply — this is a proactive alert)
- Set `notified: true` and add `"released": "YYYY-MM-DD"` (the verifier's `premiere_date`, today's UTC date when absent)
- Read the full watchlist.json, update only that entry, write the complete file back

Once that write succeeds, start the next released show. Reach Step 5 only when every released show has been delivered and written.

**If the send fails:** leave `notified` false, delete that entry's `last_checked` and `last_verdict`, write the file back, surface the send error verbatim, then continue with the remaining released shows.

**If that rollback write also fails:** surface both errors verbatim, naming the entry whose stamps are still on disk. Send no further shows and finish the skill here.

**If the send succeeds but the write fails:** surface the write error verbatim, naming every title already delivered this run and stating that their `notified` flags did not persist, so the next fire may repeat those alerts. Send no further shows and finish the skill here — Step 5's write would fail the same way, and each extra send would add another unrecorded alert.

Once every released show is done, proceed immediately to Step 5.

## Step 5 — Mark cancelled shows

For each title Step 3 classified as cancelled: set `notified: true`, add `"cancelled": true`, write the file back. Do NOT notify Baruch — a cancelled show is not actionable. If no title was classified cancelled, do nothing here.

Finish here. Shows that are not out stay silent and untouched beyond the verifier's own `last_checked` stamp.

## Notes
- The precheck date-gates the daily fire so this skill only wakes when a tracked show's release window is plausibly due, and rate-limits re-asking about an entry already resolved — see `skills/check-watchlist/scripts/check-watchlist-precheck.py` module docstring for the wake/no-wake contract, lead window, and backoff intervals.
- Field contract for the entries both scripts read and write: `skills/check-watchlist/state-schema.md`.
- Only notify for actual releases, not renewals or trailers
- New season announced without air date = "not yet released"
- Silence is default; only speak when a show is available
