---
name: trakt-watch-history
description: Fetch Trakt.tv watch history (shows, movies, ratings) for analysis and recommendation workflows. Use when the user asks for show recommendations, what to watch, wants to see their watch history, or wants suggestions based on their viewing habits.
---

# Trakt Watch History

Process steps in order. Do not skip ahead.

## Output Shape

The MCP fetch returns JSON (field names shown schematically, not literal JSON):
```text
{
  "schema_version": 1,
  "shows": [{"title", "year", "trakt_id", "slug", "episodes_watched", "last_watched", "rating"}],
  "movies": [{"title", "year", "trakt_id", "slug", "last_watched", "rating"}],
  "stats": {"total_shows", "total_movies", "rated"},
  "fetched_at": "ISO 8601 UTC timestamp"
}
```

This skill owns the persisted `trakt-history.json` record shape. The versioned contract, field promises, and migration policy live in:

```text
skills/trakt-watch-history/state-schema.md
```

> **Note on genre data:** The JSON schema does not include genre fields. Use general knowledge when confident (e.g., *Breaking Bad* = crime/drama) and be explicit about uncertainty for less-known titles — if a title's genre isn't clear, say so rather than guessing. No additional API call is available for genre classification.

## Step 1 — Fetch History

Call `mcp__nanoclaw__fetch_trakt_history()` to retrieve history.

- If the call returns `{"error": "..."}` — a real failure (auth, network, 5xx). Report the error to the user, suggest checking the Trakt.tv connection, and stop.
- If it succeeds but `shows` AND `movies` are both empty — this is a VALID state for a fresh Trakt account or privacy-restricted API access. Tell the user there's no recorded history yet and ask whether they expected data (so they can check their Trakt settings if needed) — don't treat silence as a failure. Finish here.
- If there's any history to work with, proceed immediately to Step 2.

## Step 2 — Analyze Patterns

Identify patterns from the returned data (no genre field in the schema — work with what's there first, layer genre inferences only where you're confident):

- **Primary signal: ratings.** Prioritize titles rated 8+ / 10 as the "strong positive" set. Note rewatch signal from `episodes_watched` on shows (binge or revisit).
- **Temporal signal: `last_watched` + `year`.** Recent entries vs. older ones, era clusters (e.g., run of 2000s titles), release-year patterns.
- **Title/creator signal.** Look for repeating franchises, directors/creators you can identify from the titles, adaptations of the same source material.
- **Genre, only where confident.** For titles you know well (`Breaking Bad` = crime/drama), you can layer a genre inference. For less-known titles, don't guess — note uncertainty instead of inventing a genre tag, and fall back to the rating/title/era signals above.

Proceed immediately to Step 3.

## Step 3 — Recommend

Suggest titles not already in the user's history that match the patterns you actually found:

- Lead with the strongest signal you could verify (e.g., "all five of your 9+ entries are from the same creator" beats "you like sci-fi" if you're not sure about the genre).
- Offer a secondary suggestion from a weaker-but-positively-rated cluster for variety.
- If ratings are sparse, fall back to `last_watched` recency as a preference signal.
- **Presentation:** Provide 3–5 primary recommendations and 1–2 secondary ones. For each title include its name, year, and a one-sentence reason tied to the user's viewing history (e.g., shared creator, era, or themes with a title they rated highly). If a rec rests on a genre inference, say so explicitly so the user can correct you.

Finish here — the skill is complete.

## Example Approach

If a user has rated five sci-fi shows 9/10 and two crime dramas 7/10, recommend sci-fi titles first (e.g., shows sharing cast, creators, or themes with their top-rated entries), then offer one or two crime dramas as an alternative. Always exclude titles already present in `shows` or `movies`.
