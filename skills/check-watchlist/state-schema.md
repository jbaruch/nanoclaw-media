# watchlist.json — State Schema

Stateful artifact per `jbaruch/coding-policy: stateful-artifacts`. Owner skill: **check-watchlist** — only this skill changes the shape or migrates records. `recommend-shows` appends entries in the shape below and otherwise leaves them alone.

- **Path:** `/workspace/group/watchlist.json`
- **Current `schema_version`:** 1

## Record Shape (v1)

```json
{
  "schema_version": 1,
  "tracking": [
    {
      "title": "string (may carry a season suffix: 'Fauda S5', 'MobLand Season 2')",
      "platform": "string",
      "expected": "YYYY-MM-DD | YYYY-Qn | YYYY-MM | YYYY",
      "reason": "string (why it matches Baruch's taste)",
      "added": "YYYY-MM-DD",
      "notified": "bool",
      "last_checked": "YYYY-MM-DD | absent (UTC date of the last resolved verification)",
      "last_verdict": "released | unreleased | unknown | absent (a first airing on another platform stays unknown)",
      "released": "YYYY-MM-DD | absent (set with notified: true)",
      "cancelled": "true | absent (set with notified: true)"
    }
  ]
}
```

Field promises: `notified: false` is the steady state of a tracked-but-unreleased show — it is not a "needs work today" flag. `last_checked`/`last_verdict` are present only for entries a verification run actually resolved; a lookup that failed or ran out of budget leaves both absent so the entry stays due. A record without `schema_version` is legacy pre-v1 with the same shape minus `last_checked`/`last_verdict`; readers treat it as v1.

`expected` precision is load-bearing: it sets both the wake window and how often an unresolved entry is re-asked (`_RECHECK_INTERVALS` in `scripts/check-watchlist-precheck.py`). Prefer the most precise value known — a bare year is re-asked monthly, a dated release nightly.

## Writer / Reader Contract

| Skill | Role | Promise |
|---|---|---|
| `check-watchlist` (`scripts/verify-release.py`) | writer + owner | Atomic-writes `last_checked` + `last_verdict` for every entry it resolved, before the agent composes anything. Leaves every other field untouched |
| `check-watchlist` (`scripts/mark-entry.py`, run by SKILL.md Steps 4–5) | writer + owner | One mutation per invocation on one entry: `--released` and `--cancelled` set `notified: true` plus their field; `--clear-stamps` drops `last_checked`/`last_verdict` after a failed send, leaving `notified` false. Stamps an unstamped record, refuses any other version, exits non-zero without writing on any failure. Steps 4–5 stop on that non-zero exit; on the verifier's `write_skipped` they deliver nothing and call nothing |
| `check-watchlist` (`scripts/check-watchlist-precheck.py`) | reader | Reads `notified`, `expected`, `last_checked`, `last_verdict`; never writes. Tolerates a missing file, a missing `tracking`, and entries missing any optional field |
| `recommend-shows` (`scripts/append-watchlist.py`, run by Step 9; Step 6 reader) | non-owner writer | Appends `tracking` entries (`title`, `platform`, `expected`, `reason`, `added`) and writes `notified: false` itself, only to a record already at version `1`; stamps `1` on a record it creates. An unstamped or otherwise-versioned record is read-only to it — it never migrates and never rewrites another skill's fields. Refuses an `expected` the precheck cannot anchor |

## Migration Policy

- A record **without** `schema_version` is legacy pre-v1: same shape, readable as v1. Absent `last_checked`/`last_verdict` mean "never resolved" and the entry is due. The owner stamps such a record on the first read — including a run with no verdicts to write — and reports `migrated_to_schema_version`; later runs see the stamp and rewrite nothing.
- Migration upgrades a **valid** older record. A root that is not an object, or carries no `tracking` list, is malformed at every version: every script refuses it with an actionable error and writes nothing. Stamping it would mint a shape the other readers then reject.
- Exactly two record versions are interpretable: absent (the legacy case above) and the reader's own. Any other value — newer, older, or non-integer — is no usable prior state:
  - `scripts/check-watchlist-precheck.py` (reader, `SUPPORTED_SCHEMA_VERSION`) ignores `last_checked`/`last_verdict` and date-gates alone. That path wakes, never silences
  - `scripts/verify-release.py` (writer, `WATCHLIST_SCHEMA_VERSION`) leaves the file untouched and reports `write_skipped` — it never rewrites the marker, which would downgrade a newer record into a shape nothing understands. Verdicts still return on stdout, but `write_skipped` stops the caller: `SKILL.md` Step 2 delivers nothing and marks nothing, so no alert is sent that the record cannot record
- Writer and readers ship in this plugin and deploy together, so a bump is atomic — no cross-pipeline dual-accept window applies.
- Only the owner skill migrates. `recommend-shows` never rewrites fields it did not author.
- Any shape change bumps `WATCHLIST_SCHEMA_VERSION` in `scripts/verify-release.py` and `scripts/mark-entry.py`, `SUPPORTED_SCHEMA_VERSION` in `scripts/check-watchlist-precheck.py`, `WATCHLIST_SCHEMA_VERSION` in `../recommend-shows/scripts/append-watchlist.py`, and this document in the same change.
