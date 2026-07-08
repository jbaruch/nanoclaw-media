# Changelog

All notable changes to this plugin are documented here.

### Added — trakt-history.json is a versioned stateful artifact (#33)

The cross-invocation `trakt-history.json` record now satisfies the stateful-artifacts contract: the producer stamps `schema_version` (currently 1), the schema/writer/reader contract lives in `skills/trakt-watch-history/state-schema.md` next to the owner skill, and `recommend-shows` documents its reader tolerance — records without `schema_version` are legacy pre-v1 with the same shape, records with a newer version mean no usable prior state. Additive bump: existing consumers read v1 records unchanged.

## 0.1.18 — 2026-07-08

### Changed — migrate tile.json to .tessl-plugin/plugin.json (#29)

`tessl plugin migrate` converts the deprecated `tile.json` manifest to `.tessl-plugin/plugin.json`, and `.tileignore` is renamed `.tesslignore`, retiring the two deprecation warnings and the future publish break Tessl announced for the legacy format. The publish workflow is renamed `publish-plugin.yml` ("Review & Publish Plugin", `tessl plugin lint`), and package-sense "tile" wording in ignore-file comments, the README data table, and the cadence-rationale references now reads "plugin". NanoClaw-domain "tile" terminology stays (`additionalTiles`, "per-chat overlay tile") — that is the host product's name for its overlay mechanism, not the Tessl package format. Historical CHANGELOG entries keep their original wording.

## 0.1.17 — 2026-07-08

### Fixed — release-search prompts derive years from the run date (#27)

`check-watchlist` and `recommend-books` hardcoded "2025 2026" in their web-search prompts, which rot as calendar time advances — by mid-2026 the queries already missed late-2026/2027 releases. The prompts now instruct deriving the current and next calendar year from the run date in UTC, and `check-watchlist` folds in the watchlist entry's `expected` year when it differs.

## 0.1.16 — 2026-07-07

### Fixed — youtube-comment-check tests freeze the clock (#30)

`test_fetch_youtube_comments.py` built recent/old fixture timestamps from the real wall clock, violating the testing-standards determinism rule and letting the 7-day-cutoff boundary drift with run time. `fetch-youtube-comments.py` now reads time through a `_utcnow()` seam; the tests freeze it at a fixed past reference (`FROZEN_NOW`) and derive fixture offsets from it.

## 0.1.15 — 2026-07-07

### Fixed — .env.example documents the runtime Trakt and YouTube variables (#28)

`.env.example` listed only the CI reviewer/publish secrets; the five runtime variables the README requires (`TRAKT_CLIENT_ID`, `TRAKT_ACCESS_TOKEN`, `TRAKT_REFRESH_TOKEN`, `TRAKT_CLIENT_SECRET`, `YOUTUBE_API_KEY`) were absent, so a maintainer could satisfy CI while missing every secret the media skills need at runtime. Runtime container variables now lead the file with acquisition pointers, separated from the GitHub Actions secrets block.

## 0.1.14 — 2026-07-07

### Fixed — recommend-shows: document the current trakt-history.json schema (#24)

The skill described `trakt-history.json` as a flat list of watched-episode events (`show`/`episode`/`watched_at`), but the producer emits an object with `shows`, `movies`, `stats`, and `fetched_at`, carrying per-item aggregates. An agent parsing the old shape misses watched titles and can recommend already-watched shows. The data-source description now matches the producer, classification keys off `shows[*].episodes_watched` and `shows[*].last_watched`, and Trakt per-item ratings join IMDB in the explicit-rating signals.

## 0.1.13 — 2026-07-07

### Fixed — audible-backup: scheduled no-op runs are silent (#26)

Step 1 told the agent to report "No new Audible purchases" and stop, while Step 4 said scheduled runs should be silent on no new books — Step 1 fired first, so every quiet week produced a noisy message that contradicted entertainment-sync's silent-success contract. The scheduled-vs-user-initiated split now lives at Step 1 (silent for scheduled/wrapper runs, reported for direct invocations) and Step 4 defers to it.

### Fixed — audible-backup: failed downloads no longer appended to CSV (#23)

`csv-append.py` appended every entry in the payload's `books` array regardless of per-book `status`, so a mixed backup result (some books failed to download/decrypt) corrupted `books-library.csv` with rows missing file paths and M4B metadata. The script now partitions on `status`: only `"ok"` books (or books without a `status` field, e.g. dry-run payloads) are appended; failures are excluded and reported in a new `skipped_failed` output field. The skill's Step 3 no longer asks the agent to pre-filter — the contract is enforced in code, with mixed and all-failed regression tests.

## 0.1.12 — 2026-07-07

### Changed — ignore tessl-generated .github/mcp.json (`jbaruch/nanoclaw-media#22`)

Add the tessl-generated `.github/mcp.json` scaffolding file to `.gitignore`.

## 0.1.11 — 2026-07-03

### Changed — refresh coding-policy PR review workflows

Upgrade the gh-aw `jbaruch/coding-policy` PR review workflow templates to the latest published version.

## 0.1.10 — 2026-07-02

### Changed — wire coding-policy stamp-changelog step before publish (`jbaruch/nanoclaw-media#21`)

Run `jbaruch/coding-policy/.github/actions/stamp-changelog` immediately before `tesslio/patch-version-publish` so un-headed top-of-file `### ` entry blocks get their `## <version> — <date>` heading at publish time, per the coding-policy CHANGELOG-hygiene rule.

## 0.1.9 — 2026-07-02

### Changed — backfill CHANGELOG entries for all released versions

Versions 0.1.1, 0.1.3, 0.1.4, 0.1.5, and 0.1.6 shipped without CHANGELOG entries, and the 0.1.2 agentModel, 0.1.7, and 0.1.8 audible-fix notes sat un-versioned at the top of this file. Every released version now has a heading; the entries are reconstructed from the merge commits that produced each release. No code change.

## 0.1.8 — 2026-07-02

### Fixed — audible-backup CSV: map remaining tool output fields (#10)

`map_book()` hardcoded `Language`, `Region`, `Abridged`, and `AYCE`, filled `Short Title`/`Key`/`Product ID` from the wrong source, and left `Book URL`, `Summary`, `Description`, `Publisher`, `Copyright`, `Author URL`, `Series URL`, `File name`, `File Paths`, and `User ID` blank even though the `audible_backup` tool provides all of them. All now map from the tool output, with the previous hardcoded values kept as fallbacks for payloads that omit a field and ASIN retained as the `Key`/`Product ID` fallback. `File Paths` joins the tool's list with `"; "`. `seconds_to_duration()` now returns empty for zero/negative input instead of `00:00:00`.

## 0.1.7 — 2026-07-02

### Fixed — audible-backup CSV field mapping (#4)

`csv-append.py`'s `map_book()` read nine field names that don't exist in the `audible_backup` tool output (`authors`, `narrators`, `genres`, `rating`, `num_ratings`, `cover_url`, `series_title`, `runtime_length_min`, `is_finished`), leaving those columns blank in `books-library.csv`. Keys now match the real tool schema; `duration` passes through verbatim (HH:MM:SS) with a `seconds`-derived fallback, and `read_status` is recorded verbatim (`Unread`/`Reading`/`Finished`) instead of collapsing to Finished-or-blank. Remaining hardcoded/ignored fields are tracked in #10.

## 0.1.6 — 2026-07-02

### Added — gate language diagnostics in CI with pyright (`jbaruch/nanoclaw-media#5`)

Adopt a pyright zero-findings gate: `pyrightconfig.json` for the skill-bundle layout and a `python -m pyright --warnings skills/ tests/` CI step after ruff, before pytest (`--warnings` fails on warnings too). The first run surfaced 57 findings including a real startup crash — `stamp-cursor.py` in both `entertainment-sync` and `youtube-comment-check` built its argparse description from `__doc__`, which is `None` under `python -OO` — plus test-side typing gaps fixed with a typed `_CommentServer` fixture and explicit `if ...: raise` loader guards, no suppressions. Adds a weekly Dependabot for the pinned dev toolchain.

## 0.1.5 — 2026-07-02

### Changed — refresh coding-policy PR review workflows (`jbaruch/nanoclaw-media#8`)

Upgrade the gh-aw `jbaruch/coding-policy` PR review workflow templates to the latest published version.

## 0.1.4 — 2026-07-01

### Changed — refresh coding-policy PR review workflows (`jbaruch/nanoclaw-media#7`)

Upgrade the gh-aw `jbaruch/coding-policy` PR review workflow templates to the latest published version.

## 0.1.3 — 2026-06-16

### Fixed — date-gate the check-watchlist precheck on the release window (`jbaruch/nanoclaw-media#3`)

Gate the `check-watchlist` precheck on the release window so it only fires when a watched title is actually out.

## 0.1.2 — 2026-06-08

### Changed — per-skill `agentModel:` tier-down (`jbaruch/nanoclaw#613`)

Pin the cadence skills' models via `agentModel:` frontmatter so they stop defaulting to Opus: **Sonnet** (`claude-sonnet-4-6`) for `entertainment-sync` — it synthesizes watch/read recommendations (its `Skill()`-invoked `recommend-*` sub-skills run in the same spawn, so recommendation quality rides on this model, not Haiku); **Haiku** (`claude-haiku-4-5-20251001`) for `check-watchlist` and `youtube-comment-check` (triage). Part of the #613 Claude tier-down.

## 0.1.1 — 2026-06-07

### Added — script tests omitted from the initial scaffold

Add the script unit tests that were left out of the initial tile scaffold.

## 0.1.0

### Added

- Initial tile: the personal entertainment-media skill cluster migrated from `nanoclaw-admin` into a standalone public per-chat overlay tile (`jbaruch/nanoclaw-admin#296`). Seven skills move together because they share intra-cluster data under `/workspace/group/` (e.g. `entertainment-sync` and `trakt-watch-history` write `trakt-history.json`, which `recommend-shows` reads; `recommend-shows` writes `watchlist.json`, which `check-watchlist` reads; `audible-backup` writes `books-library.csv`, which `recommend-books` reads): `entertainment-sync` (weekly cadence wrapper), `recommend-shows`, `recommend-books`, `trakt-watch-history`, `check-watchlist` (nightly cadence), `youtube-comment-check` (weekly cadence), and `audible-backup`. Carries each skill's helper scripts, state-schema/reference docs, and unit tests unchanged from the admin originals. The cluster is self-contained — no cross-tile code dependency on `nanoclaw-admin`'s `heartbeat`/Composio shared infra; each skill talks to its own data plane (Trakt API, YouTube Data API, owner-uploaded CSVs).

### Rules

- **Closed-loop carve-out claimed for `jbaruch/coding-policy: plugin-evals`** (2026-06-07). This tile is part of the `jbaruch/nanoclaw-*` plugin fleet — a fully-automated agent loop satisfying all three preconditions of the rule's "Narrow exception for closed-loop automated systems with no human eval-result consumption" clause: (1) no human reviews eval output for this tile in any form (no eval scores, no lift deltas, no scenario-by-scenario diffs, no regression alerts); (2) no automated gate consumes eval results (no `evals.yml` workflow, no publish-tile eval step, no downstream dashboard or paging route); (3) the owner accepts that re-introducing any consumption of eval results later — whether human review OR automated gating — requires re-introducing evals first under the standard requirement. Matches the carve-out claimed by `jbaruch/nanoclaw-admin` on 2026-05-09 and inherited by every `jbaruch/nanoclaw-*` tile thereafter. Covers all seven skills in this tile. No `evals/` directory ships in this tile.
