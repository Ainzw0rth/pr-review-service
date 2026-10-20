# Task 3 — PostgreSQL, transfer and deployment safety

## Scope and interfaces

Implemented in the isolated `feat/concurrent-pr-reviews` worktree. No production service,
production database, private environment/tunnel override, auth volume, or published Git history
was changed. Only the `auto-reviewer:concurrency-verification` image tag was built.

- `DB_ENGINE=sqlite|postgresql`, strict required PostgreSQL connection fields and port 1–65535.
  SQLite remains the development/test default; production `review_worker` requires PostgreSQL.
  Explicit `--allow-sqlite` supports local/test execution with one thread, no argv sniffing.
- Strict environment gates `API_ACCOUNT_CONCURRENCY=3`, `GRAPH_CONCURRENCY=1`,
  `TEST_CONCURRENCY=1` (1–32), `API_ACCOUNT_GROUPS` validated credential-ID/account-alias mapping.
- PostgreSQL 17 pinned private Compose overlay, dedicated volume, health dependencies, no host
  port. Existing base SQLite Compose and private last-file tunnel override preserved.
- psycopg/psycopg-binary 3.3.6 lock pins; matching PostgreSQL 17 client in the app image.
  `.dockerignore` now excludes `.worktrees`/`worktrees` before any original-root build.
- PostgreSQL baseline compatibility: migration 0020 widens ExpectedLabel.action to 32;
  nullable AccessLink join locks only `self`; explicit test owner PK fixtures use explicit
  second-user PKs instead of relying on SQLite-only sequence behavior.
- `manage.py backup`: selected-backend dispatch, private partial files, consistent SQLite
  backup or custom-format pg_dump, archive-list validation, fsync and exclusive publication;
  no password argv or subprocess database stderr. Failed dumps never publish partial files.
- `manage.py stage_sqlite --source SNAPSHOT --directory NEW --offline`: migrates only a new
  private workcopy, checks all original rows/columns/PKs and original fingerprint, permits
  intentional additive migration/contenttype/permission seeds. Restores all original
  sqlite_sequence high-water marks after SQLite table rebuilds (including empty tables).
- `manage.py transfer_sqlite_to_postgres --source WORKCOPY --report NEW.json --offline`:
  read-only source transaction, exact table/column/ledger inventory (including implicit M2M
  and migration PKs), raw Boolean/JSON validation, typed ORM reads/field-prepared SQL writes,
  exact fresh-target seed validation under exclusive table locks, deferred constraints,
  PK-ordered length-framed canonical table hashes/counts, all ciphertext/session signatures,
  source fingerprint recheck, all sequence high-water resets. Only default PostgreSQL target
  supported; populated target/source live path/sidecars/mismatches refused. No source mutation,
  save hooks, plaintext logging, trigger disabling, TRUNCATE CASCADE or implicit target reset.
- `deployment_control pause|open|check` and early maintenance middleware: durable ingress 503
  gate (health endpoint exempt), explicit database readiness. Normal deploy now supports reviewed
  migrations only after all-worker drain acknowledgment and graceful all-writer stop. Backup
  precedes migration; new supervisor identities and zero active claims required before resume.
  One-offs/residual native containers cause refusal, including exact unlabelled legacy graph
  names matching `^/pr-review-graph-[0-9a-f]{32}$`. No removal is automated. Failures reassert durable pause, retain
  private phase journal and previous image, never blindly roll back database/image.
- Concrete first-cutover runbook: `docs/postgres-cutover.md`. Explicit old-file-to-new-DB drain,
  cap 1 before any workers, mandatory data/readiness checks, then cap 2. Natural queued reviews
  supply production overlap evidence when available; no fabricated production review required.
  No assumed global tunnel maintenance switch: graceful web stop plus new app gate suffice.

## Red/green and commands

Python: `/root/projects/pr-review-service/.venv/bin/python` (`$PY` below).
PG test settings: private `.claude/verification/concurrent-reviews/task3_pg_settings.py` imports
controller settings and sets `TEST.NAME=test_reviewdesk_task3`. Credentials were never printed.
Logs stay private/ignored under this verification directory.

1. Before config implementation: `$PY manage.py test core.tests.test_database_config --noinput`
   produced **4 failures / 5 tests** (missing PostgreSQL selection and required-value/engine/port
   refusals). After implementation these passed. Backup/deployment/transfer tests were added
   incrementally; not claiming all were written before implementation.
2. Full PostgreSQL run (`task3-pg-full.log`): **646 tests, 5 failures, zero database errors**.
   All five were stale test contracts exposed by Task2: three CI-refresh fixtures supplied
   invalid encrypted API keys despite newly real capacity identity resolution; two callback
   doubles rejected the new `on_capacity` argument. Corrected fixtures/signatures only, kept
   all behavioral assertions. Full SQLite run before those fixture fixes (`task3-sqlite-full.log`)
   reproduced exactly those five failures: **648 tests, 6 skips**. These are diagnostic runs,
   not passing full-suite claims.
3. Focused fixture regressions on SQLite: **9 passed** (`task3-fixture-regressions.log`).
4. Final amended-module SQLite run: **291 tests passed, 1 PostgreSQL-only skip**, 40.010s
   (`task3-sqlite-final-focused.log`):

   ```sh
   $PY manage.py test core.tests.test_ci_refresh core.tests.test_worker \
     core.tests.test_database_transfer core.tests.test_database_config core.tests.test_backup \
     core.tests.test_deployment core.tests.test_events core.tests.test_quota \
     core.tests.test_auto_approval core.tests.test_auto_merge core.tests.test_workflow_editor \
     core.tests.test_dashboard --noinput
   ```

5. Real PostgreSQL final focused command includes the same modules plus `test_claims` and
   `test_capacity`, using `PYTHONPATH="$PWD/.claude/verification/concurrent-reviews:$PWD"`
   and `--settings=task3_pg_settings --noinput`. **322/322 passed**, 130.837s (`task3-pg-final-focused.log`).
6. Transfer integration exercises explicit/non-contiguous/deleted-highest IDs, Unicode, nullable
   values, booleans, JSON, timezone timestamps, UUID jobs, auth M2M IDs, password/session survival,
   credentials/checkpoints/outbox ciphertext and receipts, every inventoried table manifest,
   populated target refusal, rollback on canonical mismatch, corrupt ciphertext/FK/Boolean/JSON/
   varchar/unmapped-table/ledger refusal, and unchanged source fingerprints. Staging regression
   proves old 0018 source remains byte-identical and sequence 999 survives table rebuild.
7. Real PostgreSQL backup→restore round trip uses the verification image and a separate disposable
   DB, comparing canonical manifests for every model/through table and migration ledger.
   Command: `RUN_POSTGRES_BACKUP_ROUNDTRIP=1` plus the private PG settings and
   `$PY manage.py test core.tests.test_database_transfer core.tests.test_backup --noinput`.
   No external network writes or review/provider calls; PostgreSQL and Docker local only.
8. `ruff check core reviewdesk manage.py scripts`: passed. All 21 changed Python files formatted.
   Repository-wide format check still flags two pre-existing untouched files:
   `core/management/commands/flush_journal.py`, `core/transcript.py`; recorded in `task3-lint.log`.
   `git diff --check`, `manage.py check`, `makemigrations --check --dry-run`: passed.
9. Compose resolved in memory with fake non-secret values and `--no-env-resolution`: fixed
   `auto-reviewer` project, no PostgreSQL published port, only three PostgreSQL env keys, all five
   app/migrate services depend on DB health. No full resolved config printed.
10. Candidate Docker build passed, PostgreSQL client 17.11 installed, locked psycopg wheel installed.
    Final build log: `task3-build-final.log`. Image ID recorded below.

## Controller's production-shaped isolated rehearsal (reported evidence)

Controller used an immutable 89,260,032-byte old backup, original secret keys in memory only,
new private stage, and separate isolated `reviewdesk_rehearsal_v2` database. Final rehearsal:
**34 tables / 50,837 rows / 208 encrypted values / 13 sessions**, exact counts/hashes/FKs/ledger
validated. All **23 original sequence high-water marks** retained. First rehearsal exposed
ExpectedLabel sequence 718→716 during SQLite table rebuild; fixed in stage helper and rerun
from original immutable snapshot. V2 retains **718 in staged SQLite and PostgreSQL**.
Original backup unchanged. Evidence: `source-rehearsal-v2.log`,
`source-rehearsal-transfer-v2.json`; original first-attempt evidence retained separately.

## Operational limits / remaining gates

- `--offline` is an explicit operator attestation, not a substitute for stopping external writers.
  Source read transaction/checksums and target exclusive table locks add validation, not process
  orchestration. Follow runbook; no mixed-version/native owners or independent writers.
- Transfer currently holds table rows in memory (production rehearsal ~89MB source was verified).
  Deliberately no ETL framework; larger datasets may need bounded batching.
- Recreate dedicated target after failed import before operational retry: PostgreSQL sequence
  changes are not rollback-safe. Tests retry only injected pre-sequence failures to prove rollback.
- Archive-list validation alone is not restoration proof; round-trip verification is separate.
  Dump failure is injected, not an actual host-disk exhaustion event. Process-kill/power-loss
  durability and real production migration failures were not induced.
- Normal deployment orchestration is verified with stateful command-order/failure tests, not run
  against production. Actual cap1/cap2 behavior with live reviews, UI acceptance, graceful stop of
  legacy native owners, private override compatibility, and webhook reconciliation remain controller
  rollout gates. No automatic old-image/database rollback.
- Full post-Task4 SQLite/PostgreSQL suites and independent review remain controller gates; focused
  amended-module checks here do not misrepresent the earlier full-suite diagnostic failures.

Primary documentation checked: [Django PostgreSQL backend](https://docs.djangoproject.com/en/5.2/ref/databases/)
and [PostgreSQL 17 pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html).

## Final verification closeout

- Expanded transfer corruption + real Docker PostgreSQL backup/restore + deployment/backup suite:
  **17/17 passed**, 19.441s (`task3-transfer-final.log`). This includes seven malformed-source
  refusals, canonical-mismatch rollback, populated-target refusal, unchanged source checksums,
  auth through-table PK 77, receipt PK 42/remote ID with leading zeros, three encrypted payload
  categories, session signature, deleted-highest sequence, and all-table restore manifests.
- Corruption test initially exposed SQLite's DB-API Boolean converter even on raw cursors:
  declared Boolean value 2 normalized before validation. Fixed by selecting CAST(column AS TEXT)
  for raw validation; the same regression now refuses value 2 before any target writes.
- Final isolated legacy-graph/deploy/backup SQLite suite: **15/15 passed**, 0.269s
  (`task3-final-deploy-backup.log`). Running legacy graph names matching the exact 32-hex pattern
  prevent backup/migration/start/resume after graceful stop; no cleanup is attempted.
- Final exact-source verification image rebuilt successfully: `auto-reviewer:concurrency-verification`
  ID `sha256:7ff2a0ffa63c3f30dba404b0cafffb72b531d29340ee85065784cc7ca7c0c60a`. No `auto-reviewer:local` tag modification.
- Final Ruff check, changed-Python format check (21 files), and git diff whitespace check passed.
- No production execution or provider/publication calls performed by this task agent.
