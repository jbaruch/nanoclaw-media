# State schema — `entertainment-sync-cursor.json`

Per `coding-policy: stateful-artifacts`.

## Path

`/workspace/group/state/entertainment-sync-cursor.json` (overridable via `ENTERTAINMENT_SYNC_CURSOR` env var).

## Owner

`tessl__entertainment-sync`. Cursor written exclusively by `scripts/stamp-cursor.py`.

## Reader

`scripts/precheck-entertainment-sync.py`. Reader does NOT migrate.

## Shape (schema_version 1)

```json
{
  "schema_version": 1,
  "last_run": "2026-05-02T03:14:07Z"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `schema_version` | integer | yes | Currently `1`. |
| `last_run` | string | yes | UTC ISO-8601 with trailing `Z`. The wall-clock instant the most recent clean run stamped the cursor: Step 6's summary returned `advance_cursor: true` and Step 7 ran. |

## Lifecycle

- **First run** — cursor absent. Precheck wakes with `reason: "no_cursor"`.
- **Steady state** — gates `wake_agent: false` while `last_run` is newer than the cadence cap (`CADENCE` in `scripts/precheck-entertainment-sync.py`).
- **Bundle failure** — any of Steps 1-5 fails; the run continues, Step 6 summarizes it with `advance_cursor: false`, and Step 7 is skipped. Cursor stays at its prior value.
- **Stamp failure** — Step 7's `stamp-cursor.py` exits 2; the run finishes with a failure result and the cursor stays at its prior value.
- **Cursor corruption** — fail-open with `cursor_error`. Next successful bundle self-heals.

## Migration policy

Bump `SUPPORTED_SCHEMA` in `stamp-cursor.py` and `SUPPORTED_SCHEMA_VERSION` in the precheck. Stamp script writes the new shape; precheck treats the old shape as "no usable prior state".
