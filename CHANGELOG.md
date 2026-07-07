# Changelog

All notable changes to this tile are documented here.

### Fixed — audible-backup: scheduled no-op runs are silent (#26)

Step 1 told the agent to report "No new Audible purchases" and stop, while Step 4 said scheduled runs should be silent on no new books — Step 1 fired first, so every quiet week produced a noisy message that contradicted entertainment-sync's silent-success contract. The scheduled-vs-user-initiated split now lives at Step 1 (silent for scheduled/wrapper runs, reported for direct invocations) and Step 4 defers to it.

### Fixed — audible-backup: failed downloads no longer appended to CSV (#23)

`csv-append.py` appended every entry in the payload's `books` array regardless of per-book `status`, so a mixed backup result (some books failed to download/decrypt) corrupted `books-library.csv` with rows missing file paths and M4B metadata. The script now partitions on `status`: only `"ok"` books (or books without a `status` field, e.g. dry-run payloads) are appended; failures are excluded and reported in a new `skipped_failed` output field. The skill's Step 3 no longer asks the agent to pre-filter — the contract is enforced in code, with mixed and all-failed regression tests.

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
