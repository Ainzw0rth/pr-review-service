# PostgreSQL transfer and deployment audit

Read-only design audit, 2026-10-20. No application/configuration/production edits; this report only. Production secrets were not read. Recommendations below are proposed interfaces, not existing runnable commands.

## Observed blockers

- **P1 — no PostgreSQL backend:** `reviewdesk/settings.py:51` fixes SQLite; `requirements.txt` and `requirements.lock` lack psycopg. Simply adding a database service does not switch the application.
- **P1 — invalid PostgreSQL backups:** `core/management/commands/backup.py:19` always passes the database NAME to sqlite3. Under PostgreSQL configuration this can create/read an unrelated SQLite file instead of backing up PostgreSQL. Branch on vendor; fail closed for unsupported vendors.
- **P1 — migrations blocked:** `scripts/deploy.py:47` rejects pending migrations, before drain. Concurrent-review schema rollout cannot use this script unchanged. Never just remove the check while leaving old writers running.
- **P1 — rollback advice becomes unsafe with schema upgrades:** `scripts/deploy.py:125` always recommends image rollback. A new schema may not support the previous image; database restoration must be paired with its image/configuration and post-backup writes must not be silently lost.
- **P1 — serial drain acknowledgment cannot be reused unchanged for a pool:** `review_worker.py` writes its token after the synchronous job returns; `worker_control.py` tests running-row count plus this token. A pool must additionally wait for every future, provider subprocess/container, probe and finally-cleanup task, even after database status becomes terminal.
- **P1 — final transfer snapshot needs all writers stopped:** `scripts/deploy.py:77` currently backs up while web/events/maintenance are alive. A consistent snapshot is sufficient for ordinary backup, but copying it then switching databases loses accepted writes after its snapshot. Block ingress and stop every writer before the final transfer snapshot.
- **P2 — migration seeding/implicit relations need deliberate handling:** auth permissions/content types are created by migrations; importing explicit old IDs into these populated tables causes collisions or wrong relationships. Natural-key export is not a substitute when all IDs must be retained. Include auto-created auth M2M tables and session rows.

## Smallest robust configuration

Keep SQLite default for local/serial tests. Add explicit `DB_ENGINE=sqlite|postgresql`; reject unknown values. PostgreSQL requires `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`; optional validated `DB_PORT=5432`. Do not silently fall back on missing PostgreSQL settings. Use Django's built-in postgres backend, `OPTIONS.connect_timeout=5`, `CONN_MAX_AGE=0` initially. No URL-parser dependency, PgBouncer, or psycopg connection pool required. Each job thread owns its own Django connection; call `close_old_connections()` on entry and close thread connections in `finally`.

Add `psycopg[binary]>=3.2,<4` to requirements and regenerate the existing lock using the repository's current process. Container installs `requirements.lock`, not requirements.txt. Verify Python 3.12 wheels in image build. SQLite options must never enter the PostgreSQL OPTIONS map. Keep unchanged `DJANGO_SECRET_KEY`, `CREDENTIAL_KEY`, auth volumes and DATA_DIR. The latter still contains drain/lock files, artifacts and backups.

Add a pinned PostgreSQL 17 image (verify patch/digest when implementing), dedicated persistent `postgres-data` volume mounted `/var/lib/postgresql/data`, no host ports, `restart: unless-stopped`, `pg_isready` healthcheck. Database service receives only database-specific variables, never the entire app `.env`. App services and migrate wait for healthy postgres when PostgreSQL production configuration is selected. Use a small production compose overlay/profile if keeping SQLite-only compose usable; avoid requiring DB secrets to run local SQLite tests. Do not add postgres by inheriting the app anchor: postgres needs its own user/write paths/entrypoint.

Preserve explicit production project name `auto-reviewer`; invoking Compose from a worktree must not point at a new data volume or accidentally operate production. Inspect resolved production overrides in memory with secret redaction; never print `docker compose config` or `.env`. Confirm overrides do not replace new environment/dependency settings. Existing production override was not inspected by this audit.

Initial concurrency cap **1**. Increase to **2** only after validated PostgreSQL cutover, full capped-one end-to-end review and pool drain verification. Do not scale Compose worker replicas: keep one locked coordinator with internal dynamic slots. Cap decrease stops new admission and lets already-running work finish. Restarted scheduler loads its persisted cap. Dynamic status and cap configuration should use the project's chosen scheduler model; do not invent a competing env-only control plane. Enforce hard upper bounds and PostgreSQL-only concurrent mode.

## Transfer command contract

Proposed command:

```text
python manage.py transfer_sqlite_to_postgres \
  --source /app/var/backups/<final-snapshot>.sqlite3 \
  --target default \
  --report /app/var/backups/<transfer-id>.json
```

Default connection must be PostgreSQL. Source must be an existing regular SQLite snapshot, opened read-only (`mode=ro`); reject current live database path, missing files, source/target alias confusion and nonempty/unapproved target state. Never accept passwords on argv. Report permissions 0600; parent directory 0700. No row values, session contents, ciphertext, decrypted values or credentials in logs/report/errors. Reports include migration graph fingerprint, table names/counts/aggregate hashes, checks, timestamps and sequence high-water metadata only. Surface sanitized constraint/table identities on failure, not driver exception DETAIL containing failing rows.

### Schema alignment

Use one deliberately chosen migration boundary for both databases, normally current pre-concurrency leaf (`core.0018_job_workflow_history` in inspected checkout), including all installed Django app migrations. If production is older, make a *second disposable working copy* of its immutable final snapshot and migrate that copy to this boundary with the matching application image. Never migrate the retained original SQLite database/snapshot. Build target schema to the identical boundary first; import and validate; then apply concurrent-review migrations on PostgreSQL. This avoids forcing PostgreSQL-only migrations onto SQLite and avoids querying old columns with new runtime models. Implement using the historical app registry at the boundary (`MigrationExecutor` state), or ship transfer in a baseline-compatible image. Fail on unknown/unmapped application tables or migration mismatch, rather than silently dropping data.

### Transfer algorithm

1. Acquire deployment lock; require durable drain flag and stopped app writer services at orchestration layer. Require `PRAGMA integrity_check` success and empty `PRAGMA foreign_key_check` on source. Record its file checksum before/after read.
2. Enumerate concrete historical models **including auto-created through models**, all local concrete columns, and actual DB tables. Include all `core_*`, auth/users/groups/permissions/M2M, contenttypes, admin logs and django_session. Preserve unused/stale contenttype/permission rows too. Inventory `django_migrations` separately: preserve matched source ledger rows/IDs at the baseline, then let new migrations append on target. Handle sqlite internal sequence table explicitly; do not treat it as an application table.
3. Target must be freshly migrated, with only known migration-generated seed rows. Verify that condition before modifying anything. Within a single PostgreSQL transaction, defer deferrable constraints, clear **only explicitly inventoried destination tables** (including seed permissions/contenttypes/through rows and matched baseline migration ledger), and insert source rows with explicit PK/FK values. Do not use `TRUNCATE ... CASCADE` or disable triggers globally. No mutation of a target containing existing user/application data.
4. Read via source field converters and insert typed values using historical field metadata. Preserve UUIDs, timezone-aware datetimes, booleans, JSON, nulls, empty strings, password hashes, session keys/data, tokens/digests and encrypted strings without decrypt/re-encrypt. Avoid model `save()`, `auto_now` mutation and side-effect signals; use parameterized batched SQL or a verified raw-field insertion path. Explicitly preserve implicit M2M PKs; normal fixture M2M loading may regenerate them.
5. For each table, calculate count plus SHA-256 of deterministic, PK-ordered, length-framed canonical rows on both sides. Normalize UUID representation, timestamps to UTC with fixed precision, JSON key order and boolean types by field; preserve string/ciphertext bytes exactly. Include table/column/type metadata in the hash. Expose only table aggregates, never per-row hashes. Distinguish null from empty and zero. Check source count equals inserted count and target hash equals source hash **before commit**.
6. Validate all PostgreSQL constraints (`SET CONSTRAINTS ALL IMMEDIATE`) and explicit FK anti-join inventory where needed; fail on any missing relationships. Check uniqueness and all tables covered. Do not let PostgreSQL's stricter varchar/date/JSON constraints silently truncate/repair invalid SQLite values; abort with sanitized diagnostics.
7. For every integer identity sequence, set next allocation above both MAX(source PK) and source `sqlite_sequence` high-water mark, not merely current max. Include implicit through tables, auth/contenttypes and migration ledger. Preserve `is_called` behavior for empty tables. PostgreSQL sequence changes are not transaction rollback-safe: on any failed import, mark target unusable and recreate the dedicated staging database before retry. Never retry against partly imported destination implicitly.
8. Check every nonempty encrypted credential/checkpoint/outbox payload decrypts using unchanged key, retaining plaintext only in memory and reporting aggregate success/failure. Decode sample existing sessions using unchanged SECRET_KEY; report only success. Source validation may surface pre-existing invalid ciphertext; stop and classify, never overwrite it.
9. Commit only after all data validation passes. Apply new PostgreSQL-only migrations, run migrate --check, check --deploy, database/schema checks; produce a second post-migration manifest. Recheck preserved baseline fields/PKs against import manifest; document intentional data migrations separately.

Django dumpdata/loaddata can prototype rehearsals, but a naive `dumpdata --exclude contenttypes --exclude auth.permission` is not sufficient for the all-identifiers requirement. An ORM-aware transfer command is smaller than adopting a separate ETL stack and makes validation explicit.

## Cutover procedure

1. Rehearse with private snapshot and isolated PostgreSQL volume/database; no production network side effects. Run transfer, restore and concurrency tests. Save current app image digest, migration manifest and encrypted config backup separately. Verify CREDENTIAL_KEY and DJANGO_SECRET_KEY are available for restore without printing them. Check disk space for original SQLite, two snapshots, PostgreSQL and dump.
2. Build candidate without changing running containers. Start only database service; wait healthy. Validate configuration and candidate migrate plan read-only. Prepare target schema at import boundary. Preserve old app image tag and old configuration securely.
3. Drain existing serial worker using existing control interface. Wait current process token plus zero running jobs and completed cleanup/probe. Keep webhook intake open during long drain so accepted requests remain queued in SQLite.
4. Enter ingress maintenance window; stop web/events/maintenance gracefully, let in-flight handlers finish, then stop acknowledged-idle worker. Verify **all** writers stopped. A stop timeout must abort and request investigation, not force-kill active work; explicitly handle any residual subprocesses. Failed webhook deliveries during maintenance require operator redelivery; GitHub automatic retry must not be assumed.
5. Run old-image SQLite backup as an isolated one-off with `--no-deps`; use SQLite backup API. Retain immutable original and snapshot. Transfer from snapshot as above; no app process points to target yet. Keep maintenance + drain throughout.
6. Switch app DB config to PostgreSQL, preserve both secret keys and volumes, set scheduler cap 1. Start candidate web and worker only, while ingress remains blocked and drain flag retained. Validate DB connectivity (HTTP health alone may not prove it), migrations, old session login, organization membership, history, queued jobs/checkpoints/receipts and no extra publication. Require a *new* worker token acknowledging all tasks/probes/cleanup idle. Start events/maintenance only when data validation is accepted; these may write immediately, so treat this as crossing rollback boundary.
7. Reopen ingress and resume intake explicitly. Exercise full review success and failure paths at cap 1. Observe receipts/publication idempotency, journal/approval state, DB connections/locks and pool drain behavior. Record deployment healthy only after these gates.
8. Raise persisted cap to 2. Demonstrate two distinct eligible jobs overlap, provider/PR exclusion constraints hold, lower-cap update does not kill work, drain waits for both jobs and final cleanup. Keep previous source SQLite and backups intact.

## Backups and subsequent deployments

Extend `backup` vendor dispatch. SQLite retains backup API. For PostgreSQL use `pg_dump --format=custom --no-owner --no-acl` with server-compatible client, private exclusive temporary file, then successful exit + archive listing validation, fsync and atomic final rename. Failed/partial archives never get a success filename. Do not put password/connection URI on argv or logs. Either install a matching client in app image or, smaller, have deploy's DB backup adapter invoke pg_dump inside the pinned postgres service and stream stdout to a private host file (not command()'s text capture). The existing operator `manage.py backup` interface must either support PostgreSQL with an installed client or explicitly fail with exact replacement command; do not pretend it still works. Restore into a disposable DB and compare manifests regularly; listing an archive alone is not restore verification.

Future `scripts/deploy.py` normal path: lock → tag rollback image → build → candidate checks and migration plan → drain → pool idle acknowledgment → ingress maintenance → stop all writers → verified backup → candidate one-off migrate --noinput → migrate --check → recreate app (no implicit dependency migration rerun) → new-token pool readiness + database/web checks → resume/reopen ingress. Candidate migration checks may inspect plan before stop, but actual schema changes occur only after stop+backup. Existing no-pending-migrations check moves to after migration. Avoid auto-restarting old writers on failure. Keep machine-readable phase journal with image IDs, backup manifest and applied-migration identities; no secrets.

Rollback before target starts accepting writes: stop target app processes, restore old DB configuration/image, verify original SQLite fingerprint and drain, then resume. Never delete PostgreSQL staging volume automatically. After **any** target write (events, maintenance, sessions, webhook or job), reverting to SQLite would discard history and may duplicate irreversible publication. Default to roll-forward or compatible previous PostgreSQL-capable image. Destructive database restoration requires explicit decision about post-backup changes and reconciliation of external effects; never automate it. DB migration rollback is not synonymous with image rollback.

## Required tests and actual verification

Executed `/root/projects/pr-review-service/.venv/bin/python manage.py test core.tests.test_deployment --verbosity 1`: **7 passed** on SQLite. These test serial drain and mocked deploy ordering only; no production commands were executed.

Add:

- Settings tests: SQLite default, all PostgreSQL required values, malformed engine/port, no SQLite-only OPTIONS leakage, cap >1 rejection on SQLite.
- Real PostgreSQL transfer integration: every model/through table, non-contiguous and deleted-highest PKs, UUIDs, JSON, unicode, null/empty distinction, sessions/passwords/permissions, history, encrypted credentials/checkpoints/outbox, receipts and new FK relationships. Exact counts/hashes, FK enforcement, next IDs above original high-water, decryption without re-encryption, session survival.
- Failure injection: nonempty target, schema mismatch, unmapped tables, bad FK/JSON/long varchar/ciphertext, hash mismatch, disk-full dump, interrupted transfer, broken connection and partial archive. No source changes, no secret stdout/stderr, no published side effects; rerun requires fresh staging DB after sequence changes.
- Deploy ordering: no writers alive at backup/migrate, pending migrations supported, failed preflight leaves old system running, failure after drain never resumes, failed migrate never starts candidate, failure after schema change does not recommend blind old-image rollback. Maintenance status stays set on errors.
- Pool drain: terminal row while cleanup still running, multiple running futures, probe and quota task in progress, stale token, coordinator crash, cap shrink and concurrent drain/claim. New readiness must prove current generation and zero owned tasks, not just running-job count.
- PostgreSQL backup→restore round trip and full app smoke with network writes disabled. Cap 1 then 2 production rollout gates are operational checks, not claims established by current tests.

## Primary references

Django 5.2 supports PostgreSQL and psycopg; backend configuration is native. [Django database reference](https://docs.djangoproject.com/en/5.2/ref/databases/).

Dumpdata uses managers; --all selects the base manager. Fixtures and sequence-reset tooling are useful building blocks, but exact implicit relation IDs require additional handling. [Django management commands](https://docs.djangoproject.com/en/5.2/ref/django-admin/).

PostgreSQL custom dumps support selective restore; pg_dump provides a consistent snapshot, but source write freeze is still necessary for a lossless database cutover. [PostgreSQL 17 pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html).
