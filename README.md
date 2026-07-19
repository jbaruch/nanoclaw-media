# jbaruch/nanoclaw-media

[![tessl](https://img.shields.io/endpoint?url=https%3A%2F%2Fapi.tessl.io%2Fv1%2Fbadges%2Fjbaruch%2Fnanoclaw-media)](https://tessl.io/registry/jbaruch/nanoclaw-media)

Personal entertainment-media skills for NanoClaw. Syncs Trakt watch history, recommends TV shows and audiobooks from your viewing/reading history, checks tracked shows for releases, digests new YouTube channel comments, and backs up Audible purchases — with a weekly cadence companion that drives the recurring pieces.

Per-chat overlay tile. Install via NanoClaw's `containerConfig.additionalTiles` mechanism.

## Capabilities

1. **Trakt watch-history sync** — pulls shows/movies/ratings from the Trakt API into `trakt-history.json`, routed in-container through the OneCLI gateway (the gateway owns Trakt OAuth + refresh)
2. **Show recommendations** — ranks unwatched titles from Netflix history, IMDb ratings, and Trakt history; tracks upcoming shows in `watchlist.json`
3. **Audiobook recommendations** — filters the Audible library by genre/rating, finds series continuations and new releases from favorite authors
4. **Watchlist release checks** — nightly check of `watchlist.json`, Telegram notification when a tracked show is released
5. **YouTube comment digest** — weekly fetch of new comments on the owner's channel, per-video summary when anything is new (silent otherwise)
6. **Audible backup** — backs up new audiobook purchases and appends them to `books-library.csv`
7. **Weekly entertainment refresh** — the `entertainment-sync` cadence companion runs the Trakt pull + watchlist check + recommendations + Audible sync on a weekly schedule

## Installation

```
tessl install jbaruch/nanoclaw-media
```

Add to a chat's overlay tile list via `update_group_config`:

```
additionalTiles: ["nanoclaw-media"]
```

Load the overlay at the **main or trusted** tier — the cadence skills materialise `scheduled_tasks` rows and the skills read/write owner data under `/workspace/group/`.

## Required environment

| Variable | Used by | Purpose |
|----------|---------|---------|
| `YOUTUBE_API_KEY` | youtube-comment-check | YouTube Data API v3 key |

NanoClaw forwards these into main/trusted containers. Trakt requires no container variable at all: watch-history requests route through the OneCLI gateway, which injects **every** Trakt credential on the wire — the custom-oauth connection injects the OAuth Bearer, and a header-injection secret injects the client id as the `trakt-api-key` header. No Trakt client id or token lives in the container or `.env`. The recommendation skills (`recommend-shows`, `recommend-books`) consume no secrets — they read owner-uploaded CSV/JSON data.

## Runtime data

The skills read and write files under the shared `/workspace/group/` mount:

| File | Access | Owner |
|------|--------|-------|
| `trakt-history.json` | write (`entertainment-sync`/`trakt-watch-history`), read (`recommend-shows`) | this plugin |
| `watchlist.json` | write (`recommend-shows`), read (`check-watchlist`) | this plugin |
| `books-library.csv` | write (`audible-backup`), read (`recommend-books`) | this plugin |
| `netflix-history.csv` | read | owner-uploaded |
| `imdb-ratings.csv` | read | owner-uploaded |
| `state/entertainment-sync-cursor.json` | read+write | this plugin (cadence cursor) |
| `state/youtube-comment-check-cursor.json` | read+write | this plugin (cadence cursor) |

Owner-uploaded files degrade gracefully when absent (ladder-fallback). Intra-cluster reads (`recommend-shows` ← `trakt-history.json`) resolve because all producing/consuming skills ship in this plugin.

## Skills

| Skill | Description |
|-------|-------------|
| [entertainment-sync](skills/entertainment-sync/SKILL.md) | Weekly entertainment refresh (cron `0 10 * * 0`): pulls Trakt watch history, checks watchlist for releases, runs show + book recs, syncs Audible purchases. |
| [recommend-shows](skills/recommend-shows/SKILL.md) | Ranks unwatched TV shows by predicted interest from Netflix/IMDb/Trakt history and tracks upcoming shows in `watchlist.json`. Use when asked for show recommendations or what to watch next. |
| [recommend-books](skills/recommend-books/SKILL.md) | Recommends audiobooks from the Audible library by reading history, genre/rating filters, series continuations, and new releases. Use when asked what to read/listen to next. |
| [trakt-watch-history](skills/trakt-watch-history/SKILL.md) | Fetches Trakt.tv watch history (shows, movies, ratings) for analysis and recommendation workflows. |
| [check-watchlist](skills/check-watchlist/SKILL.md) | Nightly check (cron `30 9 * * *`) of tracked shows in `watchlist.json`; Telegram notification when any have been released. |
| [youtube-comment-check](skills/youtube-comment-check/SKILL.md) | Weekly fetch (cron `45 10 * * 0`) of new comments on the owner's YouTube channel; per-video summary when new comments appear, silent otherwise. |
| [audible-backup](skills/audible-backup/SKILL.md) | Backs up new Audible purchases, decrypts to M4B, and appends to `books-library.csv`. |

## Status

- **V1** — migrated the personal-media skill cluster from `nanoclaw-admin` as a standalone per-chat overlay tile (`jbaruch/nanoclaw-admin#296`). Three cadence skills (`entertainment-sync`, `check-watchlist`, `youtube-comment-check`) materialise `scheduled_tasks` rows in chats that load this overlay; the rest are user-driven.

See [CHANGELOG.md](CHANGELOG.md) for version history.
