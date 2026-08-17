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
| `check-watchlist` (SKILL.md Steps 4–5) | writer + owner | Sets `notified: true` plus `released` or `cancelled`, one entry at a time, writing the whole file back after each. On a failed send it sets neither and deletes that entry's `last_checked`/`last_verdict`, so the backoff cannot suppress the retry of an undelivered alert |
| `check-watchlist` (`scripts/check-watchlist-precheck.py`) | reader | Reads `notified`, `expected`, `last_checked`, `last_verdict`; never writes. Tolerates a missing file, a missing `tracking`, and entries missing any optional field |
| `recommend-shows` (Step 9 writer, Step 6 reader) | writer | Appends new `tracking` entries (`title`, `platform`, `expected`, `reason`, `added`, `notified: false`) to a record at version `1` or unstamped, stamping `1` on a record it creates or on a legacy one it appends to. Writes nothing at any other version. Never migrates, never rewrites another skill's fields |

## Migration Policy

- A record **without** `schema_version` is legacy pre-v1: same shape, readable as v1. Absent `last_checked`/`last_verdict` mean "never resolved" and the entry is due. The owner stamps such a record on the first read — including a run with no verdicts to write — and reports `migrated_to_schema_version`; later runs see the stamp and rewrite nothing.
- Exactly two record versions are interpretable: absent (the legacy case above) and the reader's own. Any other value — newer, older, or non-integer — is no usable prior state:
  - `scripts/check-watchlist-precheck.py` (reader, `SUPPORTED_SCHEMA_VERSION`) ignores `last_checked`/`last_verdict` and date-gates alone. That path wakes, never silences
  - `scripts/verify-release.py` (writer, `WATCHLIST_SCHEMA_VERSION`) leaves the file untouched and reports `write_skipped` — it never rewrites the marker, which would downgrade a newer record into a shape nothing understands. Verdicts still return, so a released show is still notified
- Writer and readers ship in this plugin and deploy together, so a bump is atomic — no cross-pipeline dual-accept window applies.
- Only the owner skill migrates. `recommend-shows` never rewrites fields it did not author.
- Any shape change bumps `WATCHLIST_SCHEMA_VERSION` in `scripts/verify-release.py`, `SUPPORTED_SCHEMA_VERSION` in `scripts/check-watchlist-precheck.py`, and this document in the same change.
