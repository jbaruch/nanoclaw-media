# Field Mapping Reference

How `skills/audible-backup/scripts/csv-append.py` maps `audible_backup` tool output JSON fields to
`books-library.csv` columns.

| CSV Column | JSON Field | Transform |
|---|---|---|
| ASIN | asin | as-is |
| Key | key | fallback: asin |
| Product ID | product_id | fallback: asin |
| Title | title | as-is |
| Short Title | title_short | fallback: title |
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
| Language | language | fallback: "english" |
| Region | region | fallback: "US" |
| Abridged | abridged | lowercase string bool; fallback: "false" |
| AYCE | ayce | lowercase string bool; fallback: "false" |
| Book URL | info_link | as-is |
| Summary | summary | as-is |
| Description | description | as-is |
| Publisher | publisher | as-is |
| Copyright | copyright | as-is |
| Author URL | author_link | as-is |
| Series URL | series_link | as-is |
| File name | filename | as-is (empty on dry-run) |
| File Paths | files | list joined with "; " (empty on dry-run) |
| User ID | user_id | as-is |
| M4B | m4b_path | download path |

Not addressable — no corresponding keys in the tool output: PDF URL,
Audible (AAX), MP3, Image, PDF.
