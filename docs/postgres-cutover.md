# PostgreSQL cutover (supervised, once)

Do not run normal `scripts/deploy.py` to switch database backends. Rehearse this procedure against a
private snapshot and an isolated PostgreSQL instance first. The retained original SQLite and final
snapshot are never migrated or overwritten. No old/new writers may overlap. All commands below
run from the production checkout, not a worktree; use the reviewed candidate image only.

## Preparation, without interrupting workers

1. Retain the previous image ID/tag and a private backup of configuration; preserve both
   `DJANGO_SECRET_KEY` and `CREDENTIAL_KEY` unchanged. Check disk space for source, snapshot,
   working copy, PostgreSQL volume and backup. Back up CLI auth volumes by the existing procedure;
   do not copy/repair authentication as part of this rollout.
2. Keep existing private `.env` and `compose.override.yaml`. Add database settings without replacing
   other values: `DB_NAME`, `DB_USER`, a privately generated `DB_PASSWORD`, `DB_HOST=postgres`,
   `DB_PORT=5432`; leave `DB_ENGINE=sqlite` until transfer validation. Use exactly:

   ```sh
   export COMPOSE_FILE=compose.yaml:compose.postgres.yaml:compose.override.yaml
   ```

   If there is no private override, omit that final file. Retain this `COMPOSE_FILE` setting for
   future deployments (environment or private `.env`). Inspect resolved Compose **in memory with
   secret redaction**, never print full `docker compose config`. Verify private tunnel overrides
   retain app dependencies/settings. PostgreSQL has no published port and receives DB variables
   only, not the full application environment. Never run `down -v`.
3. Build `docker compose build web`; `.dockerignore` excludes `.worktrees` and private verification
   files. Start only `docker compose up -d --wait postgres`. Do not start migrate/app dependencies.

## Drain and freeze

1. `docker compose exec -T web python manage.py worker_control drain`, then poll `status --json`.
   Require idle, zero active jobs, current process acknowledgment and completed cleanup/probes.
   Incoming accepted requests may remain queued during this wait. Capture the old worker token.
2. If an existing per-app proxy maintenance gate is available, close it; otherwise the graceful
   web stop in step 3 closes ingress. Do not alter an unrelated/global tunnel. Run the candidate one-off
   `docker compose run --rm --no-deps web python manage.py deployment_control pause` to create
   the durable gate honored by new web processes.
3. `docker compose stop --timeout -1 web events maintenance worker`. This waits gracefully rather
   than escalating to SIGKILL. Confirm every app replica stopped and no independent shell,
   maintenance command, Compose one-off or external writer remains. Inspect running containers
   with legacy `auto-reviewer.cli`/`auto-reviewer.test` and new `auto-reviewer.namespace` labels;
   all old CLI/test/graph owners must be quiescent. Legacy graph runners have no labels:
   also require `docker ps -q --filter 'name=^/pr-review-graph-[0-9a-f]{32}$'` to return empty. Unknown/unscoped legacy containers require
   positive owner-death evidence. Abort if ownership is unclear; do not bulk-delete containers.

## Snapshot, stage, transfer (all writers remain stopped)

Commands use the unchanged secret keys from the private environment. Do not pass keys/passwords
on argv or print row data. The source snapshot and new staging directory paths below are examples;
choose unique paths under `/app/var/backups` and retain them.

```sh
# Still DB_ENGINE=sqlite: consistent final snapshot, output is private path only.
docker compose run --rm --no-deps web python manage.py backup

# Substitute the exact backup path. New working directory must NOT exist.
docker compose run --rm --no-deps web python manage.py stage_sqlite \
  --source /app/var/backups/FINAL.sqlite3 --directory /app/var/backups/CUTOVER-stage --offline

# Fresh target only; never reuse a populated/failed staging database.
docker compose run --rm --no-deps -e DB_ENGINE=postgresql web python manage.py migrate --noinput
docker compose run --rm --no-deps -e DB_ENGINE=postgresql web python manage.py transfer_sqlite_to_postgres \
  --source /app/var/backups/CUTOVER-stage/reviewdesk.sqlite3 \
  --report /app/var/backups/CUTOVER-transfer.json --offline
```

`--offline` is an operator attestation, not a mechanism for stopping external writers. The transfer
also rejects live DATA_DIR paths, SQLite sidecars, schema/ledger/table mismatches, non-fresh target,
invalid ciphertext/session signatures, invalid raw booleans/JSON, FK errors and content mismatches.
It retains all explicit PKs/M2M IDs, migration ledger, ciphertext and deleted-highest sequence marks.
Working-copy migrations may add new permissions/content types/ledger records but must preserve
every original column/row. Source checksums are rechecked. Reports contain aggregates, no row data.
On failure keep everything stopped. Recreate only the dedicated staging PostgreSQL database after
investigation; sequence operations are not transaction-rollback-safe. Never delete the source.

## Start paused, cap 1, then cap 2

1. Change only `DB_ENGINE=postgresql` in the existing private `.env`. Keep both keys and volumes.
2. Explicitly bridge the old file drain into the **new database** before any worker starts:

   ```sh
   docker compose run --rm --no-deps web python manage.py worker_control drain
   docker compose run --rm --no-deps web python manage.py shell -c \
     'from core.models import ServiceSettings; ServiceSettings.objects.filter(pk=1).update(concurrent_pr_reviews=1)'
   docker compose run --rm --no-deps web python manage.py migrate --check
   docker compose run --rm --no-deps web python manage.py check --deploy --fail-level WARNING
   docker compose run --rm --no-deps web python manage.py deployment_control check
   docker compose up -d --no-deps --force-recreate --wait web worker
   docker compose exec -T web python manage.py worker_control status --json
   ```

3. Require a **new token**, every new supervisor acknowledged idle, zero active claims/probes,
   cap 1 and draining true. Confirm transfer report, sessions, memberships, queue/history,
   checkpoints/receipts and no extra publication. Use read-only database inspection while the app gate stays closed. Controlled login/UI checks
   after acceptance may write sessions: this crosses the lossless SQLite rollback boundary.
4. Start events/maintenance `--no-deps --wait` only after acceptance. These processes can write
   immediately. Resume workers, open app gate (`deployment_control open`), and reopen any per-app proxy gate
   used earlier. Reconcile failed GitHub deliveries. Observe a naturally queued capped-one review
   when available, then drain; do not fabricate production reviews or external writes.
5. Raise the persisted Settings concurrency to 2 (or the same validated operator update above
   with value 2), then resume. When naturally queued work permits, observe two eligible PRs overlapping,
   same-PR/provider exclusion, and drain waiting for **both** jobs plus cleanup/probes. Record
   unavailable natural-work observations as pending, not passed; synthetic concurrency/resize
   cases belong in isolated tests. Mandatory pre-resume gates are data validation, cap 1, new
   supervisor readiness and zero active tasks.
   Do not scale Compose worker replicas. Record these production checks separately from unit tests.

## Backups and rollback

`manage.py backup` uses SQLite backup API or PostgreSQL 17 `pg_dump` custom format, with private
0600 temporary files, archive-list validation, fsync and exclusive publication. PostgreSQL 17
client is installed in the application image. Passwords travel through process environment only.
Regularly restore with `pg_restore --exit-on-error --no-owner --no-acl` into a disposable empty
DB and compare full table manifests; listing alone is not restore verification.

Before **any target write**, rollback can stop candidate services, restore the old configuration
and compatible old image, verify original SQLite checksum and drain, then resume deliberately.
After a session/event/maintenance/webhook/job write, returning to SQLite discards history and may
repeat irreversible publication. Default to roll-forward or a verified PostgreSQL-compatible image.
Database restoration needs an explicit post-backup reconciliation decision; never automate it.
Keep original SQLite, snapshots, private configuration and transfer/backup receipts intact.

Drain all supervisors before changing `API_ACCOUNT_CONCURRENCY`, `GRAPH_CONCURRENCY`,
`TEST_CONCURRENCY`, account aliases, secret/namespace, or running CLI login/import/repair.
Environment capacities are strict integers 1–32; persisted PR concurrency is a separate live control.

SQLite `review_worker` startup is refused by default. Local development/tests may explicitly use
`--allow-sqlite`, which limits execution to one thread; it is not a production configuration.
