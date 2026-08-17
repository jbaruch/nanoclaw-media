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
| `check-watchlist` (SKILL.md Steps 4–5) | writer + owner | Sets `notified: true` plus `released` or `cancelled`, one entry at a time, writing the whole file back after each |
| `check-watchlist` (`scripts/check-watchlist-precheck.py`) | reader | Reads `notified`, `expected`, `last_checked`, `last_verdict`; never writes. Tolerates a missing file, a missing `tracking`, and entries missing any optional field |
| `recommend-shows` (Step 9 writer, Step 6 reader) | writer | Appends new `tracking` entries (`title`, `platform`, `expected`, `reason`, `added`, `notified: false`); never migrates, never rewrites another skill's fields |

## Migration Policy

- A record **without** `schema_version` is legacy pre-v1: same shape, readable as v1. Absent `last_checked`/`last_verdict` mean "never resolved" and the entry is due.
- A record with a **newer** `schema_version` than the reader accepts means the reader is lagging: treat as no usable prior state — the precheck ignores `last_checked`/`last_verdict` and date-gates alone (wake), never falls to silence. The reader's accepted ceiling is `SUPPORTED_SCHEMA_VERSION` in `scripts/check-watchlist-precheck.py`.
- Writer and readers ship in this plugin and deploy together, so a bump is atomic — no cross-pipeline dual-accept window applies.
- Only the owner skill migrates. `recommend-shows` never rewrites fields it did not author.
- Any shape change bumps `SCHEMA_VERSION` in `scripts/verify-release.py` and this document in the same change.
