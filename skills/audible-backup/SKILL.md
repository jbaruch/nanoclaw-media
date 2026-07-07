---
name: audible-backup
description: Back up new Audible audiobook purchases, decrypt to M4B, and append to books-library.csv. Use on "audible backup", "check new audiobooks", "sync audible library", or from weekly scheduled task.
---

# Audible Backup Skill

Process steps in order. Do not skip ahead.

## Step 1 — Run audible backup dry-run

Call the `mcp__nanoclaw__audible_backup` MCP tool with `dryRun: true`.

Parse the response. If `new_books` is 0 or `books` array is empty, report "No new Audible purchases" and stop.

If the tool errors (auth failure, Docker issue), report the error and stop.

Otherwise proceed immediately to Step 2.

## Step 2 — Download new books

If Step 1 found new books, call `mcp__nanoclaw__audible_backup` with `dryRun: false` to download and decrypt them.

Parse the response. Note which books succeeded (`status: "ok"`) and which failed.

Proceed immediately to Step 3.

## Step 3 — Append to CSV

Pipe the full backup response JSON to the CSV append script:

```bash
echo '<JSON>' | python3 /home/node/.claude/skills/tessl__audible-backup/scripts/csv-append.py
```

The script appends only books with `status: "ok"` (failed downloads are excluded and counted in `skipped_failed`) and deduplicates by ASIN. Outputs JSON summary with `appended` count.

Proceed immediately to Step 4.

## Step 4 — Report results

Send a message summarizing what was downloaded. Format:

```
<b>Audible Backup</b>

• <b>Title</b> by Author (Series Name #N) — HH:MM
• <b>Title</b> by Author — HH:MM

N new books added to library (total: M).
```

Durations in the message are HH:MM — drop the seconds from the tool's
HH:MM:SS `duration` value.

If nothing new → silence (for scheduled runs). If user-initiated → "No new purchases."

Finish here — the skill is complete.

## Field Mapping Reference

The full CSV column ↔ JSON field mapping table lives at:

```text
skills/audible-backup/field-mapping.md
```
