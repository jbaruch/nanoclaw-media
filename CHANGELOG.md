# Changelog

All notable changes to this tile are documented here.

### Changed — per-skill `agentModel:` tier-down (`jbaruch/nanoclaw#613`)

Pin the cadence skills' models via `agentModel:` frontmatter so they stop defaulting to Opus: **Sonnet** (`claude-sonnet-4-6`) for `entertainment-sync` — it synthesizes watch/read recommendations (its `Skill()`-invoked `recommend-*` sub-skills run in the same spawn, so recommendation quality rides on this model, not Haiku); **Haiku** (`claude-haiku-4-5-20251001`) for `check-watchlist` and `youtube-comment-check` (triage). Part of the #613 Claude tier-down.

## 0.1.0

### Added

- Initial tile: the personal entertainment-media skill cluster migrated from `nanoclaw-admin` into a standalone public per-chat overlay tile (`jbaruch/nanoclaw-admin#296`). Seven skills move together because they share intra-cluster data under `/workspace/group/` (e.g. `entertainment-sync` and `trakt-watch-history` write `trakt-history.json`, which `recommend-shows` reads; `recommend-shows` writes `watchlist.json`, which `check-watchlist` reads; `audible-backup` writes `books-library.csv`, which `recommend-books` reads): `entertainment-sync` (weekly cadence wrapper), `recommend-shows`, `recommend-books`, `trakt-watch-history`, `check-watchlist` (nightly cadence), `youtube-comment-check` (weekly cadence), and `audible-backup`. Carries each skill's helper scripts, state-schema/reference docs, and unit tests unchanged from the admin originals. The cluster is self-contained — no cross-tile code dependency on `nanoclaw-admin`'s `heartbeat`/Composio shared infra; each skill talks to its own data plane (Trakt API, YouTube Data API, owner-uploaded CSVs).

### Rules

- **Closed-loop carve-out claimed for `jbaruch/coding-policy: plugin-evals`** (2026-06-07). This tile is part of the `jbaruch/nanoclaw-*` plugin fleet — a fully-automated agent loop satisfying all three preconditions of the rule's "Narrow exception for closed-loop automated systems with no human eval-result consumption" clause: (1) no human reviews eval output for this tile in any form (no eval scores, no lift deltas, no scenario-by-scenario diffs, no regression alerts); (2) no automated gate consumes eval results (no `evals.yml` workflow, no publish-tile eval step, no downstream dashboard or paging route); (3) the owner accepts that re-introducing any consumption of eval results later — whether human review OR automated gating — requires re-introducing evals first under the standard requirement. Matches the carve-out claimed by `jbaruch/nanoclaw-admin` on 2026-05-09 and inherited by every `jbaruch/nanoclaw-*` tile thereafter. Covers all seven skills in this tile. No `evals/` directory ships in this tile.
