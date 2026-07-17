# State schema — `youtube-comment-check-cursor.json`

Per `coding-policy: stateful-artifacts`: every stateful artifact ships a schema document next to its owner skill.

## Path

`/workspace/group/state/youtube-comment-check-cursor.json` (overridable per-process via the `YOUTUBE_COMMENT_CHECK_CURSOR` env var, used by tests).

## Owner

`tessl__youtube-comment-check` (this skill). The cursor is written exclusively by `scripts/stamp-cursor.py`. No other skill writes it.

## Reader

`scripts/precheck-youtube-comment-check.py` (this skill, but reader-not-writer). Per the rule, the reader does NOT migrate; on encountering an unsupported `schema_version`, it treats the row as "no usable prior state" (fail-open: wake the agent so the next stamp restores a current cursor).

## Shape (schema_version 1)

```json
{
  "schema_version": 1,
  "last_run": "2026-05-02T03:14:07Z"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `schema_version` | integer | yes | Currently `1`. Bump on shape change; only the owner script migrates. |
| `last_run` | string | yes | UTC ISO-8601 with trailing `Z`. The wall-clock instant the most recent successful check completed Step 2 (fetch) AND Step 3 (report — including `mcp__nanoclaw__send_message` if comments existed) and reached Step 4 (advance cursor). |

## Lifecycle

- **First run / fresh install** — cursor is absent. The precheck returns `wake_agent: true` with `reason: "no_cursor"`. The first successful check creates the file.
- **Steady state** — precheck reads the cursor; gates `wake_agent: false` when `now_utc - last_run` is under the precheck's `CADENCE` cap, otherwise `wake_agent: true`.
- **Check failure** — Step 2 (fetch) OR Step 3 (`mcp__nanoclaw__send_message` if comments needed reporting) fails; Step 4 is skipped intentionally. The cursor stays at its prior value, so the next eligible cycle's precheck either keeps gating (if still inside the cap window) or wakes the agent for a retry (once the cap has elapsed). The "Step 3 fail then stamp" anti-pattern would gate the next cap window even though Baruch never saw the comments — Step 4's "Steps 2 AND 3 both succeeded" gate prevents that. Because the fetch window is driven from this cursor (not a fixed 7 days), the retry re-covers the comments the failed run missed rather than losing them outside a fixed window.
- **Cursor corruption** — any read error (missing keys, malformed JSON, naive datetime, schema mismatch) flips the precheck to fail-open (`wake_agent: true`). The next successful check stamps a fresh cursor that self-heals the corruption.

## Migration policy

If a future shape change is needed (new field, renamed field, semantic shift on `last_run`):

1. Bump `SUPPORTED_SCHEMA` in `stamp-cursor.py` and `SUPPORTED_SCHEMA_VERSION` in the precheck.
2. The stamp script writes the new shape on its next run.
3. The precheck, observing `schema_version != supported`, treats the row as "no usable prior state" until the owner stamps the new shape.

Do NOT silently repurpose `last_run` to mean something different at the same `schema_version`.
