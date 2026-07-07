---
name: recommend-books
description: Recommend audiobooks based on Baruch's reading history and preferences. Analyzes reading patterns, filters unread titles by genre and rating, identifies series continuations, checks for new releases from favorite authors, and suggests similar authors or highly-rated unread titles from the Audible library JSON. Use when Baruch asks for book recommendations, "what to read next", wants something similar to a specific author, or asks about his unread queue.
---

# Audiobook Recommendation Skill

**Every step below is mandatory. Execute them in order. Do not skip, reorder, or abbreviate any step.**

## Input

Baruch may ask with or without specifics:
- "Посоветуй книгу" (general)
- "Что-нибудь похожее на Jeremy Robinson"
- "Хочу что-то легкое / фантастику / триллер"
- "Что у меня непрочитанного?"

## Step 1: Load library

Read `/workspace/extra/audiobooks/books (1).json` — a JSON array of audiobook objects. Key non-obvious fields:

| Field | Notes |
|---|---|
| `title` | May contain `&amp;` HTML entities |
| `series_name` | Widely populated; use for series continuation detection |
| `series_sequence` | Position in series |
| `read_status` | `"Finished"` / `"Unread"` / `"Reading"` |
| `percent_complete` | 0–100, listening progress. **Trust `read_status` over `percent_complete`** for finished classification |
| `rating_average` | Overall Audible rating (float as string) |
| `performance_rating` | Narration quality rating (float as string) |
| `story_rating` | Story quality rating (float as string) |
| `genre` | Hierarchical `"Top:Sub:SubSub"` format, comma-separated for multi-genre |

## Step 2: Analyze reading patterns

Derive all counts and current state directly from the JSON.

**From "Finished" books — build preference profile:**
- Favorite authors (by finished count)
- Preferred genres using the hierarchical `genre` field — split by comma, extract top-level and sub-genres
- Themes and styles from description/summary
- Series he's completing
- **Narrator preference:** per narrator, compute avg `performance_rating` minus avg `story_rating` across finished books. High delta + high finished count = preferred narrator
- **Story vs narration preference:** if Baruch finishes books with higher `performance_rating` than `story_rating`, he's narrator-driven; reverse means story-driven. Weight recommendations accordingly

**Filtering rules — apply before generating recommendations:**
- `read_status = "Reading"`: skip authors, genres, or styles matching an in-progress book
- `purchase_date` before 2024 + `read_status = "Unread"`: dominant genres of these books are low priority
- Genre with multiple abandoned unread titles: exclude entirely

## Step 3: Check new releases (web search required)

Always search the web for new books from top authors before recommending.

Derive top authors from the JSON (highest "Finished" count), plus any author Baruch mentions.

For each, search with the current and next calendar year derived from today's date (never hardcoded years): `"[Author name]" new book [current year] OR [next year] audiobook`

Cross-reference results against the library JSON by `title` and `author`. If already in library, skip. If not in library and fits taste, recommend (flag as "not yet in your library, available on Audible").

## Step 4: Generate recommendations

| Request type | Logic |
|---|---|
| "What to read next" / unread queue | Filter `read_status = "Unread"`. Priority: continuing an in-progress series > high-rated (`rating_average` >= 4.3) > matching favorite genres. Flag series continuations: "Book 5 of [series_name] — you've finished 1-4" |
| "Something like X" | Find books by same author, same genre/subgenre, or same series style. Check both Finished (for comparison) and Unread (for suggestions) |
| General recommendation | Mix: 1-2 unread books matching top genres + 1 wildcard from a less-explored genre with high rating |

**3D rating ranking:**
- `rating_average` >= 4.3 = strong candidate
- `performance_rating` >= 4.5 = great narration; boost if narrator-driven
- `story_rating` >= 4.5 = great story; boost if story-driven
- Performance-Story delta > 0.3 = notably better narration than story (or vice versa); call out explicitly
- Tie-break on the dimension Baruch values more

## Step 5: Format response

Keep it tight — 3-5 recommendations max. For each, write a **targeted pitch**, not a summary:
- Connect to something specific Baruch already likes ("Батчер но в Риме", "как Агата Кристи только жестче")
- Flag relevant facts: series length, finished, narrator quality
- Include all three ratings when meaningful (e.g., "narration 4.9 / story 4.3 — carried by the narrator")
- Be honest about weaknesses ("первые 2 книги медленные", "автор ещё не закончил серию")

```
<b>[Title]</b> — [Author] (read by [Narrator])
[1-2 sentence targeted pitch tied to Baruch's taste]
[Duration] | ⭐ [rating_average] (narr: [performance_rating] / story: [story_rating]) | [Series note if relevant]
```

If a recommendation doesn't fit ("уже читал", "не закончена"), pivot immediately to alternatives — don't just say "okay".
