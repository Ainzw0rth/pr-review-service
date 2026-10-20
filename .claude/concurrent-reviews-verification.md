# Concurrent PR reviews — pre-deploy verification

Source at `69ded73` (documentation-only fix after code `2c79cdc`).

- Full SQLite: 656 tests, 69.686s, OK, 6 PostgreSQL/optional skips.
- Full PostgreSQL 17: 656 tests, 254.541s, OK, no skips.
- Ruff check, 61 changed Python files formatted, Django check, migration drift and git diff checks pass. Unchanged legacy formatter issues were not reformatted.
- All four task reviews and final whole-branch review pass. Final README backup finding corrected and scoped review accepted.
- Real isolated Docker resource lifecycle/healthy-slot/orphan cleanup and PostgreSQL backup/restore pass. No model/auth/GitHub calls used for verification.
- Private production-shaped transfer rehearsal: 34 tables, 50,837 rows, 208 encrypted values, 13 sessions, all 23 source sequence highwaters retained; original snapshot unchanged. Final live transfer still needs a fresh stopped-writer snapshot.
- Settings browser: desktop/mobile, save/success, invalid400/stale409, keyboard/focus, 44px controls, reduced motion, no overflow. Existing progress browser regression passes. Root inspected rendered screenshots. Chromium only; no cross-engine/screen-reader audit claimed.

## Ruling

Keep existing remote-write admission semantics: an already-admitted bounded GitHub request may finish after cancellation; fresh guards prevent later requests. No database transaction held around network calls. Cost if wrong: one admitted remote write may finish after cancellation and require receipt reconciliation.

## Rollout

Authorized existing-branch push and supervised stopped-writer SQLite→PostgreSQL transfer. Start paused/cap1, validate data/readiness/history, then default2. Preserve original SQLite, backup, private keys/override and auth volumes. Actual production receipt is private under `.claude/verification/concurrent-reviews/live-rollout/`; no secret values or private row content belong in git.
