---
name: audible-backup
description: Back up new Audible audiobook purchases, decrypt to M4B, and append to books-library.csv. Use on "audible backup", "check new audiobooks", "sync audible library", or from weekly scheduled task.
---

**Every step below is mandatory. Execute them in order. Do not skip, reorder, or abbreviate any step.**

## Step 1: Run audible backup dry-run

Call the `mcp__nanoclaw__audible_backup` MCP tool with `dryRun: true`.

Parse the response. If `new_books` is 0 or `books` array is empty, report "No new Audible purchases" and stop.

If the tool errors (auth failure, Docker issue), report the error and stop.

## Step 2: Download new books

If Step 1 found new books, call `mcp__nanoclaw__audible_backup` with `dryRun: false` to download and decrypt them.

Parse the response. Note which books succeeded (`status: "ok"`) and which failed.

## Step 3: Append to CSV

For each book with `status: "ok"`, pipe the full backup response JSON to the CSV append script:

```bash
echo '<JSON>' | python3 /home/node/.claude/skills/tessl__audible-backup/scripts/csv-append.py
```

The script deduplicates by ASIN. Outputs JSON summary with `appended` count.

## Step 4: Report results

Send a message summarizing what was downloaded. Format:

```
<b>Audible Backup</b>

• <b>Title</b> by Author (Series Name #N) — HH:MM
• <b>Title</b> by Author — HH:MM

N new books added to library (total: M).
```

If nothing new → silence (for scheduled runs). If user-initiated → "No new purchases."

## Field Mapping Reference

| CSV Column | JSON Field | Transform |
|---|---|---|
| ASIN | asin | as-is |
| Title | title | as-is |
| Author | authors | as-is |
| Narrated By | narrators | as-is |
| Genre | genres | as-is |
| Ave. Rating | rating | as-is |
| Rating Count | num_ratings | as-is |
| Purchase Date | purchase_date | date part only |
| Release Date | release_date | as-is |
| Duration | runtime_length_min | min→HH:MM:00 |
| Series Name | series_title | may be absent |
| Series Sequence | series_sequence | may be absent |
| Image URL | cover_url | as-is |
| Read Status | is_finished | true→"Finished" |
| M4B | m4b_path | download path |
