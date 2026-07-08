# trakt-history.json — State Schema

Stateful artifact per `jbaruch/coding-policy: stateful-artifacts`. Owner skill: **trakt-watch-history** — only this skill changes the shape or migrates records.

- **Path:** `/workspace/group/trakt-history.json`
- **Current `schema_version`:** 1

## Record Shape (v1)

```json
{
  "schema_version": 1,
  "shows": [
    {
      "title": "string",
      "year": "int | null",
      "trakt_id": "int | null",
      "slug": "string | null",
      "episodes_watched": "int (aggregate count)",
      "last_watched": "ISO 8601 timestamp | null",
      "rating": "int 1-10 | null (Baruch's own rating)"
    }
  ],
  "movies": [
    {
      "title": "string",
      "year": "int | null",
      "trakt_id": "int | null",
      "slug": "string | null",
      "last_watched": "ISO 8601 timestamp | null",
      "rating": "int 1-10 | null (Baruch's own rating)"
    }
  ],
  "stats": { "total_shows": "int", "total_movies": "int", "rated": "int" },
  "fetched_at": "ISO 8601 timestamp (UTC)"
}
```

Field promises: `shows` and `movies` are always present (possibly empty arrays) and sorted by `last_watched`, most recent first. `rating` is null for unrated items. An `{"error": "..."}` object is a failed fetch, not a record — writers never persist it.

## Writer / Reader Contract

| Skill | Role | Promise |
|---|---|---|
| `trakt-watch-history` (via `fetch_trakt_history` MCP) | writer + owner | Emits every field above on every successful run, stamped with the current `schema_version` |
| `entertainment-sync` (Step 1) | writer trigger | Invokes the same MCP fetch; never writes the file itself |
| `recommend-shows` (Step 1 trigger, Steps 2–5 reader) | writer trigger + reader | Step 1 invokes the same MCP fetch (never writes the file itself, never migrates). Reads `shows[*].title`, `year`, `slug`, `episodes_watched`, `last_watched`, `rating` and `movies[*].title`, `year`, `rating` for classification and taste signals; tolerates a missing file (no prior state) |

## Migration Policy

- A record **without** `schema_version` is a legacy pre-v1 record; its shape is identical to v1, and readers may treat it as v1. The next successful fetch rewrites it stamped.
- A record with a **newer** `schema_version` than the reader accepts means the reader is lagging: treat as no usable prior state (refresh via Step 1 of the consuming skill) and update the reader.
- Only the owner skill migrates. Readers never rewrite the file.
- Any shape change bumps `SCHEMA_VERSION` in `scripts/trakt-watch-history.py` and this document in the same change.
