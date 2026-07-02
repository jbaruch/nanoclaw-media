# Field Mapping Reference

How `skills/audible-backup/scripts/csv-append.py` maps `audible_backup` tool output JSON fields to
`books-library.csv` columns.

| CSV Column | JSON Field | Transform |
|---|---|---|
| ASIN | asin | as-is |
| Title | title | as-is |
| Author | author | as-is |
| Narrated By | narrated_by | as-is |
| Genre | genre | as-is |
| Ave. Rating | rating_average | as-is |
| Rating Count | rating_count | as-is |
| Purchase Date | purchase_date | date part only |
| Release Date | release_date | as-is |
| Duration | duration (fallback: seconds) | as-is HH:MM:SS; fallback seconds→HH:MM:00 |
| Series Name | series_name | may be absent |
| Series Sequence | series_sequence | may be absent |
| Image URL | image_url | as-is |
| Read Status | read_status | as-is ("Unread"/"Reading"/"Finished") |
| M4B | m4b_path | download path |
