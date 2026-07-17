# Cadence rationale — why a filesystem cap, not a comment-count gate

The epic (`jbaruch/nanoclaw#404`) lists the precheck signal for this sub-skill as "YouTube channel comment-count delta since last run". Two options were considered:

## Rejected — comment-count gate via a YouTube API call in the precheck

Have the precheck call the YouTube Data API and count comments in the last 7 days. Wake only when the count is positive.

Why rejected (same reasoning slice 1's `nightly-undated-task-sweep` applied to a Tasks-API gate):

- **No precedent in this plugin.** Every existing precheck in `nanoclaw-admin` (`heartbeat-precheck.py`, `morning-brief-precheck.py`, `precheck-undated-task-sweep.py`, `precheck-state-purge.py`) reads from SQLite or the local filesystem only. None of them shells out to a network API.
- **OAuth refresh inside the precheck container.** The OneCLI vault mediates token refresh for agent containers. The precheck process is a short-lived subprocess spawned by the task-scheduler with a different env profile; wiring vault access into it doubles the surface where a token rotation can fail silently.
- **Failure modes leak into wake decisions.** A 500 from YouTube would force a fail-open ("wake to be safe") on every cycle the API is unhealthy, defeating the gate's purpose during the exact incidents the gate is supposed to insulate from.

## Chosen — filesystem cadence cap

Precheck reads `<state_dir>/youtube-comment-check-cursor.json`. If `last_run` is missing or older than the cadence cap (value in `scripts/precheck-youtube-comment-check.py`), wake; otherwise skip. The skill stamps the cursor in Step 3, after Steps 1 (fetch) and 2 (report) both succeed. The cap value and the reason it sits below the weekly cron interval live in the precheck's `CADENCE` comment (`jbaruch/nanoclaw#803`).

Why this works:

- **Matches the existing precheck idiom.** Read-only filesystem check, no network, fails open on parse errors.
- **Aligned with the epic's proposed cadence.** The epic table proposes "weekly" — the cap targets that weekly cadence rather than a fixed schedule, which composes better with the orchestrator's Sunday-4am cron pattern.
- **Empirically verifiable.** `task_run_logs` will show the check fire at most once per ISO week; on weeks where weekly-housekeeping fires multiple times (continuation cycle, manual re-run), only the first fire reaches Step 2.

## When to revisit

If `task_run_logs` shows the gating savings are insufficient (e.g. the check wakes every cycle because the cursor write keeps failing, or the cap is too tight for the actual rate of new comments), revisit the option matrix. A count-based gate via the YouTube API remains an option once the OAuth / failure-mode concerns above are addressed at the plugin level — as a shared precheck client, not a per-skill ad-hoc.
