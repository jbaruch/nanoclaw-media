---
name: recommend-shows
description: Analyzes Baruch's viewing history and explicit ratings across netflix-history.csv, imdb-ratings.csv, and trakt-history.json to identify preferred genres, classify completed and abandoned shows, and rank unwatched titles by predicted interest. Generates targeted TV show recommendations with quality thresholds, searches for new releases, and tracks upcoming shows in watchlist.json. Use when Baruch asks for show recommendations, "что посмотреть", "что смотреть", or similar requests for what to watch next.
---

# TV Show Recommendation Skill

## Step 0: Refresh Trakt history (MANDATORY — do not skip)

Before reading `trakt-history.json`, refresh it:

```
mcp__nanoclaw__fetch_trakt_history()
```

Writes fresh `/workspace/group/trakt-history.json`. Skipping recommends already-watched shows. On MCP failure, proceed with the existing file but add this staleness note as the FIRST line of Step 5's output (before pitches): `<i>⚠️ Trakt refresh failed — using cached history as of {mtime, ISO date}. Recommendations may include already-watched titles.</i>`. Do NOT fabricate freshness; do NOT bury the note.

## Data Sources

- `/workspace/group/netflix-history.csv` — Netflix history. Format: `"Show Name: Season X: Episode Name", "date"` — split on `: `. Single-part titles are movies (skip). Group by show name.
- `/workspace/group/imdb-ratings.csv` — Explicit ratings (Const, Your Rating 1–10, Title, Title Type, Year, Genres). ~160 entries.
- `/workspace/group/trakt-history.json` — Trakt watch history. Format: `[{"show": {"title": ...}, "episode": {"season": N, "number": N}, "watched_at": ...}]`. **Refreshed in Step 0 — trust Trakt over CSVs for recency.** On Step 0 failure, Step 1a still applies and Step 5's staleness preamble discloses the age.
- `/workspace/group/watchlist.json` — Upcoming tracked shows. Check before web research (Step 4a).

Filter out kids' content: animated children's shows, preschool series, toy-brand cartoons.

### Step 1a: Source priority

**Trakt is the primary source** — live and synced across all platforms. The CSVs are static exports that go stale.

| Trakt watched | CSV watched | Decision |
|---|---|---|
| ✓ | any | Watched (primary signal) |
| ✗ | ✓ | Watched (Trakt sync may be incomplete) |
| ✗ | ✗ | Candidate for recommendation |

If Baruch reports "уже видел" for a recommendation, Trakt sync hadn't finished — note and pivot.

## Step 2: Classify shows

For each show, compute `episodes_watched / total_episodes` (search for total if needed). Derive thresholds from actual data distribution before applying these defaults:

- **Completed**: ratio > 0.8, or last episode of final season in history. Ongoing "caught up" = no gap in recent seasons.
- **Abandoned**: ratio < 0.15 AND episodes_watched ≤ 3 AND no return after initial cluster.
- **In progress**: Everything else — do not re-recommend unless a new season exists.

Shows with 1–3 plays that never resumed are strong abandonment signals. **IMDB ratings** (imdb-ratings.csv): high-rated genres = confirmed loves; low-rated = genres that don't click.

## Step 3: Taste profile

Derive from data files using Step 2 classifications and IMDB ratings:

- **Top genres** — most frequent in high-rated, high-completion shows
- **Avoided genres** — frequent in abandoned or low-rated shows
- **Style signals** — patterns from comparing completed vs abandoned (language, episode structure, tone)
- **Quality calibration** — use median and percentiles of Baruch's own IMDB ratings as the floor

**Intermediate output format** (emit before Step 4 to anchor recommendations):
```
## Taste Profile (derived from data)

**Top genres:** Crime/Thriller, Dark Comedy, Sci-Fi (hard), Drama
**Avoided genres:** Reality TV, Procedural, Romantic Comedy
**Language signal:** Non-English shows rate 0.4 pts higher on average (lean into it)
**Episode structure:** Prefers serialized > episodic (completion rate 78% vs 41%)
**Quality floor:** Baruch's median IMDB rating = 7.8 → use 7.5 as recommendation floor
**Completed shows (top):** [Show A, Show B, Show C]
**Abandoned shows:** [Show X, Show Y]
```

## Step 4a: Check watchlist for tracked shows

Before web research, read `/workspace/group/watchlist.json`. For `notified: false` entries, search for release status:
- Released → include as top recommendation, note watchlist origin
- Not yet → mention briefly as "coming soon" at end

## Step 4: Check new releases (web search required)

Training cutoff is stale; always search. Derive queries from Step 3's taste profile — top genres and themes from completed/high-rated shows. Do NOT hardcode genre names.

Cross-reference against all three data files; if present in any, he's seen it. Flag started-but-not-in-history shows as "new to you."

### Quality filter (mandatory)

Use thresholds from Step 3. If sparse, fall back to IMDB ≥ 7.5 / RT ≥ 75%. Search for ratings before recommending if unavailable. Always include ratings.

## Step 5: Generate recommendations

**Prioritize:**
1. New seasons of completed shows (not yet in history)
2. Shows similar to his top-completed (genre/vibe/style from Step 3)
3. Patterns from data (e.g., non-English content rates high → lean in)

**Avoid:** Step 3 abandoned/low-rated genres, previously abandoned shows, shows below quality threshold.

**Targeted pitch format** (Telegram HTML, no Markdown):
```
<b>[Show Name]</b> ([Year], [Seasons]) — IMDB X.X | RT XX%
[1-2 sentence targeted pitch: connect to something he already loves]
[Where to watch] | [Status: ongoing/finished]
```

Max 3–5 recommendations.

## Step 6: Save announced/upcoming shows to watchlist

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
