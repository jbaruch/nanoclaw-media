---
name: recommend-shows
description: Analyzes Baruch's viewing history and explicit ratings across netflix-history.csv, imdb-ratings.csv, and trakt-history.json to identify preferred genres, classify completed and abandoned shows, and rank unwatched titles by predicted interest. Generates targeted TV show recommendations with quality thresholds, searches for new releases, and tracks upcoming shows in watchlist.json. Use when Baruch asks for show recommendations, "что посмотреть", "что смотреть", or similar requests for what to watch next.
---

# TV Show Recommendation Skill

Process steps in order. Do not skip ahead.

## Step 1 — Refresh Trakt History (MANDATORY)

Before reading `trakt-history.json`, refresh it via the in-container fetch script (routes through the OneCLI gateway, writes the file on success):

```bash
python3 /home/node/.claude/skills/tessl__trakt-watch-history/scripts/trakt-watch-history.py
```

Writes fresh `/workspace/group/trakt-history.json`. Skipping the refresh risks recommending already-watched shows. On fetch failure (stdout `{"error": ...}` / non-zero exit), proceed with the existing file but add this staleness note as the FIRST line of Step 8's output (before pitches): `<i>⚠️ Trakt refresh failed — using cached history as of {mtime, ISO date}. Recommendations may include already-watched titles.</i>`. Do NOT fabricate freshness; do NOT bury the note.

Proceed immediately to Step 2.

## Step 2 — Load Data Sources

- `/workspace/group/netflix-history.csv` — Netflix history. Format: `"Show Name: Season X: Episode Name", "date"` — split on `: `. Single-part titles are movies (skip). Group by show name.
- `/workspace/group/imdb-ratings.csv` — Explicit ratings (Const, Your Rating 1–10, Title, Title Type, Year, Genres). ~160 entries.
- `/workspace/group/trakt-history.json` — Trakt watch history. Format: object `{"schema_version": 1, "shows": [...], "movies": [...], "stats": {...}, "fetched_at": "ISO 8601 UTC timestamp"}`. Each show: `{"title", "year", "trakt_id", "slug", "episodes_watched", "last_watched", "rating"}` — `episodes_watched` is an aggregate count, `last_watched` an ISO timestamp, `rating` Baruch's own 1–10 rating or null. Movies carry the same fields minus `episodes_watched`. Full contract: `skills/trakt-watch-history/state-schema.md` (owner: trakt-watch-history; this skill triggers the rewrite via Step 1's in-container fetch script and reads the result — it never writes the file itself and never migrates). A record without `schema_version` is legacy pre-v1 — same shape, read it as v1. A record with `schema_version` > 1 is no usable prior state — rely on the Step 1 refresh. **Refreshed in Step 1 — trust Trakt over CSVs for recency.** On Step 1 failure, Step 3 still applies and Step 8's staleness preamble discloses the age.
- `/workspace/group/watchlist.json` — Upcoming tracked shows. Check before web research (Step 6).

Filter out kids' content: animated children's shows, preschool series, toy-brand cartoons.

Proceed immediately to Step 3.

## Step 3 — Apply Source Priority

**Trakt is the primary source** — live and synced across all platforms. The CSVs are static exports that go stale.

| Trakt watched | CSV watched | Decision |
|---|---|---|
| ✓ | any | Watched (primary signal) |
| ✗ | ✓ | Watched (Trakt sync may be incomplete) |
| ✗ | ✗ | Candidate for recommendation |

If Baruch reports "уже видел" for a recommendation, Trakt sync hadn't finished — note and pivot.

Proceed immediately to Step 4.

## Step 4 — Classify Shows

For each show, compute `episodes_watched / total_episodes` — `episodes_watched` comes from the Trakt show entry (fall back to counting grouped Netflix CSV rows for shows absent from Trakt); search for the show's total if needed. Derive thresholds from actual data distribution before applying these defaults:

- **Completed**: ratio > 0.8. Ongoing "caught up" = ratio near 1.0 for aired episodes AND `last_watched` recent relative to the airing schedule.
- **Abandoned**: ratio < 0.15 AND `episodes_watched` ≤ 3 AND `last_watched` months in the past.
- **In progress**: Everything else — do not re-recommend unless a new season exists.

Shows with `episodes_watched` ≤ 3 and an old `last_watched` are strong abandonment signals. **Explicit ratings**: Trakt per-item `rating` (shows and movies) plus IMDB ratings (imdb-ratings.csv) — high-rated genres = confirmed loves; low-rated = genres that don't click.

Proceed immediately to Step 5.

## Step 5 — Build the Taste Profile

Derive from data files using Step 4 classifications and explicit ratings (Trakt `rating` + IMDB):

- **Top genres** — most frequent in high-rated, high-completion shows
- **Avoided genres** — frequent in abandoned or low-rated shows
- **Style signals** — patterns from comparing completed vs abandoned (language, episode structure, tone)
- **Quality calibration** — use median and percentiles of Baruch's own explicit ratings (Trakt `rating` + IMDB) as the floor

**Intermediate output format** (emit before Step 7 to anchor recommendations):
```
## Taste Profile (derived from data)

**Top genres:** Crime/Thriller, Dark Comedy, Sci-Fi (hard), Drama
**Avoided genres:** Reality TV, Procedural, Romantic Comedy
**Language signal:** Non-English shows rate 0.4 pts higher on average (lean into it)
**Episode structure:** Prefers serialized > episodic (completion rate 78% vs 41%)
**Quality floor:** Baruch's median explicit rating (Trakt + IMDB) = 7.8 → use 7.5 as recommendation floor
**Completed shows (top):** [Show A, Show B, Show C]
**Abandoned shows:** [Show X, Show Y]
```

Proceed immediately to Step 6.

## Step 6 — Check the Watchlist for Tracked Shows

Before web research, read `/workspace/group/watchlist.json`. For `notified: false` entries, search for release status:
- Released → include as top recommendation, note watchlist origin
- Not yet → mention briefly as "coming soon" at end

Proceed immediately to Step 7.

## Step 7 — Check New Releases (web search required)

Training cutoff is stale; always search. Derive queries from Step 5's taste profile — top genres and themes from completed/high-rated shows. Do NOT hardcode genre names.

Cross-reference against all three data files; if present in any, he's seen it. Flag started-but-not-in-history shows as "new to you."

**Quality filter (mandatory):** use thresholds from Step 5. If sparse, fall back to IMDB ≥ 7.5 / RT ≥ 75%. Search for ratings before recommending if unavailable. Always include ratings.

Proceed immediately to Step 8.

## Step 8 — Generate Recommendations

**Prioritize:**
1. New seasons of completed shows (not yet in history)
2. Shows similar to his top-completed (genre/vibe/style from Step 5)
3. Patterns from data (e.g., non-English content rates high → lean in)

**Avoid:** Step 5 abandoned/low-rated genres, previously abandoned shows, shows below quality threshold.

**Targeted pitch format** (Telegram HTML, no Markdown):
```
<b>[Show Name]</b> ([Year], [Seasons]) — IMDB X.X | RT XX%
[1-2 sentence targeted pitch: connect to something he already loves]
[Where to watch] | [Status: ongoing/finished]
```

Max 3–5 recommendations.

Proceed immediately to Step 9.

## Step 9 — Save Announced Shows to the Watchlist

For announced-but-not-released shows (or just-announced new seasons) matching taste, add to `/workspace/group/watchlist.json` under `tracking` if not present:

```json
{
  "title": "Show Name",
  "platform": "Platform",
  "expected": "YYYY or YYYY-QN",
  "reason": "Why it matches Baruch's taste",
  "added": "YYYY-MM-DD",
  "notified": false
}
```

Read existing watchlist.json first, merge, write back. No duplicates.

Finish here — the skill is complete.
