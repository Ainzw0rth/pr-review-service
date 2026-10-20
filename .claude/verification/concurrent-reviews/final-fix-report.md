# Final review correction — Operations backup/restore

Base: `2c79cdcbbf5604a870d577dbee2af7d0e78b6b31`.
Finding: final-review.md P3, README Operations instructions still described SQLite-only backups/restoration.

Corrected README only: selected-backend `.sqlite3` versus custom-format `.pgdump` archives,
explicit SQLite-only file replacement, PostgreSQL `pg_restore`, matching key/image/schema safeguards,
and direct link to the cutover guide's backup/rollback section and post-backup publication reconciliation.

Verification: link target `docs/postgres-cutover.md` exists and contains `## Backups and rollback`;
`git diff --check` passed. Docs-only change; no tests added or suites rerun. No code, production,
private configuration, service, data, push or deployment operations performed.
