# Cadence rationale — why a weekly cadence cap, no per-source delta gate

The epic (`jbaruch/nanoclaw#404`) lists the precheck signal for this sub-skill as "Trakt/Audible API delta". Two options were considered:

## Rejected — per-source API delta gate in the precheck

Have the precheck call Trakt's API to count new history entries since `last_run`, AND call Audible's API to count new purchases. Wake only when at least one signal is positive.

Why rejected:

- **Same OAuth-leak failure mode as slice 1.** Network calls from a precheck process create the exact failure surface (token rotation, 5xx fail-open, no-precedent in this tile) that slices 1, 3, 4, 5 already documented for Composio-style gates. Two APIs doubles the surface.
- **Inner skills already gate.** `recommend-shows`, `recommend-books`, `audible-backup`, `check-watchlist` each handle the "no new data" case by staying silent. The wrapper running with no new entries on a cadence-elapsed cycle costs nothing user-visible.
- **The wrapper IS cheap to run.** Five inner-skill invocations sequentially, most of which short-circuit when no new data exists. Running the bundle weekly regardless of the API delta is fine.

## Chosen — weekly filesystem cadence cap

Precheck reads `<state_dir>/entertainment-sync-cursor.json`. If `last_run` is missing or older than the cadence cap (value in `scripts/precheck-entertainment-sync.py`), wake; otherwise skip. The wrapper stamps the cursor on Steps 1-5 success.

The cap sits below the weekly cron interval. The value and the `nanoclaw-admin#353` near-miss rationale live in `scripts/precheck-entertainment-sync.py` (the `CADENCE` comment) and the `#353` CHANGELOG entry.

Why this works:

- **Matches the existing precheck idiom across slices 1, 3, 4, 5, 7, 8.** Single cursor JSON read.
- **Aligned with the epic's proposed cadence.** Weekly.
- **Inner skills' silence-on-empty contract makes the wrapper's "always run" mode acceptable.**

## When to revisit

If `task_run_logs` shows the wrapper firing regularly with all five inner skills going silent (suggesting Trakt + Audible have nothing new), tighten cadence to biweekly. If a per-source delta gate becomes feasible at the tile level (a shared Composio-aware precheck client landing for OWASP work, etc.), it can layer on top of this cap without changing the wrapper.
