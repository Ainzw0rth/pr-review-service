# Deployment and operations

Use [installation](installation.md) for a fresh instance. All commands here assume:

```sh
export COMPOSE_FILE=compose.yaml:compose.postgres.yaml
```

Append any private override last. Keep this selection consistent for startup, exec, backup and
updates; the base file alone does not start PostgreSQL. Never use `docker compose down -v` on
an installation whose data or authentication you need.

## HTTPS proxy

For a public server use `LOCAL_HTTP=0`, `DEBUG=0`, `PUBLIC_URL=https://reviews.example.com`,
matching `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`, and `TRUST_PROXY=1` only behind your trusted
TLS proxy. The proxy must set the forwarded HTTPS scheme and preserve the configured public
Host. The application listener remains `127.0.0.1:8040`; publish only the TLS proxy, not port 8040.

Host-native proxies target `http://127.0.0.1:8040`. Containerized proxies need a reachable host
gateway or the app network with upstream `http://web:8040`. DNS, certificates and tunnel setup
are external to this Compose stack. `/healthz/` supports loopback health checks without a redirect.
Local HTTP mode instead uses `TRUST_PROXY=0` and strictly loopback hosts; see installation for
forwarding the webhook with a loopback upstream Host header.

## Concurrency

One worker supervisor runs configurable concurrent PR reviews on PostgreSQL. Do not scale worker
replicas as a substitute for capacity settings. Settings controls saved PR and CLI-session limits;
lowering a limit lets current work finish before replacement work starts.

`CLI_ACCOUNT_CONCURRENCY`, `API_ACCOUNT_CONCURRENCY`, `GRAPH_CONCURRENCY` and `TEST_CONCURRENCY`
provide environment-level resource limits; account aliases can share an API capacity pool through
`API_ACCOUNT_GROUPS`. Drain every supervisor and probe before changing environment limits,
account mappings, runner images or authentication. Saved live Settings controls do not require
restarts. Resource limits do not increase provider quotas. Concurrent CLI execution requires the
runner's authentication-coordination capability; build the supplied runner rather than an arbitrary
CLI image. SQLite worker startup is refused unless explicitly opted into single-thread development
with `--allow-sqlite`.

## Persistent CLI review sessions

Codex and Claude reviews keep a separate native session per PR, configured provider and review
role (including intent and synthesis). The database stores the session ID and a random storage
key. Native conversation files live under `review-sessions/<key>/` in that backend's existing
private authentication volume; credentials and runtime configuration are not copied into those
history directories. Probes stay ephemeral.

Later human-requested reviews resume the matching session with the current commit-pinned evidence.
Full evidence coverage and publication checks still apply. Missing history starts a new session;
changing the model, effort or review contract resets its storage key. Incomplete-evidence recovery
also starts fresh. Same-PR execution claims prevent concurrent writers, while different roles
have separate histories. Session continuation does not guarantee fewer billed input tokens.

This requires migration `0026_cli_sessions` and a rebuilt CLI runner with the
`auto-reviewer.sessions=v1` capability label. An older runner is rejected rather than silently
discarding history. Build/pin the runner using the installation instructions before deploying
the application update.

Native histories contain source code and review conversations in the CLI's own on-disk format,
not application-level encryption. Protect and back up the private volumes as sensitive data.
Session history is not included in the 90-day job cleanup; it persists until the operator removes
it. To reset history, drain workers first, then remove only the relevant `review-sessions/<key>/`
directory (never the authentication files). Its next review will obtain a fresh session ID.

## Updates

Review the intended revision and migration plan first. Keep a private backup of `.env`, preserve
both encryption keys, and retain auth volumes. Use the deployment script from the checkout on the
Linux Docker host; it requires Python 3 but no application virtual environment:

```sh
python3 scripts/deploy.py
```

It builds the candidate, checks migrations/deployment configuration, drains reviews, closes intake,
stops writers (including the dedicated keepalive scheduler), backs up and applies migrations before starting and verifying new processes.
It saves a rollback image and a private `var/deployment.json` receipt. Do not run an uncoordinated
`docker compose up --build` over active reviews. HTTPS production checks reject insecure server settings. Explicit validated loopback HTTP
mode permits only its expected local HTTPS/cookie warnings; other deployment checks remain active.

On failure the script leaves intake paused/draining. Inspect logs and the receipt, resolve the
cause, and establish schema/image compatibility before resuming. A previous image may not be
compatible after migrations. Restoring a database can discard accepted events and repeat external
publication; reconcile post-backup activity before any restore. The script does not automatically
restore or blindly roll back.

For manual inspection:

```sh
docker compose exec -T web python manage.py worker_control status --json
docker compose exec -T web python manage.py worker_control drain
```

Do not resume until all supervisors are healthy, active work/cleanup is accounted for and any
application deployment gate is intentionally handled. `worker_control resume` controls workers;
`deployment_control open` separately opens the application gate.

## Distributed workers: drain, upgrades and rollback

Remote mode is opt-in (`REMOTE_EXECUTION_ENABLED=1`); see [worker installation](workers.md).
The `keepalive` Compose service runs `worker_keepalive` independently of review/event
processing. It shares the private application data/configuration but has no Docker socket
or inbound port. The coordinator image includes OpenSSH client support. Deployment stops
and verifies this DB writer alongside web/events/maintenance/workers before migrations;
it restarts only after replacement supervisor readiness. Never leave an older independent
keepalive process writing the database during schema changes.
A coordinator/global drain stops **new PR admissions**, not ongoing reviews. Existing
remote placements must still fetch queued phases, renew execution and submit completion
while those reviews finish. A per-worker drain prevents new placement on that worker;
it does not cancel existing phases. Do not set the deployment maintenance gate early:
maintenance rejects every route except `/healthz/`, including worker callbacks, with
HTTP 503 before database access. The deployment script enables that gate only after
confirmed global cleanup and keeps it closed during migrations.

`worker_control status --json` includes:

- `active_jobs`: retained coordinator PR claims, including terminal jobs awaiting cleanup.
- `remote_active`: **all** remote assignments with unconfirmed cleanup, including queued,
  cancelled/quarantined, terminal and orphaned records with no remaining PR claim.
- `remote_quarantined`: the subset explicitly marked quarantined.
- `idle`: true only after drain acknowledgement from every local supervisor, no active
  claims/probes, and no unconfirmed remote cleanup.

A disconnected or revoked worker, expired lease, empty local Docker process list or
completed job status is not proof of remote termination. Deployment waits without
forcibly killing reviews when any remote cleanup remains uncertain. Empty workers do
not need to be online to permit a coordinator upgrade. Never clear pending assignment,
PR-claim or account-permit records just to make the deployment gate pass.

The worker runtime has no ORM/database migrations. Protocol-compatible version-1
agents do not require a simultaneous restart for coordinator schema-only updates.
The scheduler rejects unsupported protocol versions; after deployment, verify fresh
heartbeats and eligibility before expecting remote work. An incompatible agent may
stay unavailable without blocking unrelated local reviews, except when its uncertain
execution still holds a shared account permit. Drain and explicitly verify compatibility
before changing runner images, account mappings or the execution protocol itself.

For failed rollout/recovery:

1. Leave global intake drained and maintenance closed when schema compatibility is
   uncertain. Do not blindly start an old application image against new migrations.
2. Disable new remote scheduling in the coordinator configuration. Drain or revoke
   affected workers as appropriate: revocation fences results, not processes. Preserve
   existing assignment tokens/history and quarantined account ownership.
3. Establish termination on the actual worker host and obtain the supported cleanup
   acknowledgement. A local coordinator `docker ps` check cannot prove this. Keep
   unconfirmed capacity quarantined; do not treat a timeout as reconciliation.
4. Select a schema-compatible application revision, complete reviewed migrations and
   start replacement supervisors. Never restore a database over accepted events/results
   without reconciling those writes and external GitHub publication.
5. Verify local readiness and zero unresolved cleanup, then intentionally open intake.
   Re-enable remote scheduling only after supported agent protocol, authentication,
   account binding and fresh heartbeat/readiness checks succeed.

The deploy script accepts legacy standalone status responses without remote counters
only after read-only schema inspection proves the remote assignment table does not exist.
This permits the first upgrade from standalone; a present or uninspectable ledger fails
closed. The proof is discarded after migration. Upgraded coordinators
report both counters; malformed values fail closed, and pending remote counts block
rollout even if an inconsistent status claims to be idle.

## Backup and retention

```sh
docker compose exec -T web python manage.py backup
```

The command prints a private archive path under `/app/var/backups` in the data volume. PostgreSQL
backups use PostgreSQL 17 `pg_dump` custom format. Copy backups off the Docker host along with
protected `.env` and provider auth-volume backups; a container volume alone is not an off-host
backup. Do not print credentials or full resolved Compose configuration into shared logs.

Test restoration into a separate empty PostgreSQL database with `pg_restore --exit-on-error
--no-owner --no-acl`, then validate table contents. Archive listing is not restore verification.
The production database and `.env` encryption key must stay paired.

Maintenance runs retention cleanup automatically. Terminal jobs/activity and analysis checkpoints
normally expire after 90 days; tracked approval ownership is retained for stale-approval handling,
and pending journal delivery survives job-history cleanup. To run cleanup explicitly:

```sh
docker compose exec -T web python manage.py cleanup
```

For an existing SQLite installation, follow the separate [supervised cutover](postgres-cutover.md).
Changing `DB_ENGINE` alone does not transfer data.

## Revoked-worker termination reconciliation

A revoked worker cannot authenticate its cleanup acknowledgement. Keep its assignment
and account capacity quarantined until the configured dashboard owner has verified
termination **on the actual worker host**. The following command records that owner's
attestation; it does not contact the worker, stop containers, infer death from a timeout,
accept model output, retry a review, or publish to GitHub.

Before using it:

1. Revoke the affected worker through the owner Workers page. Record the worker UUID,
   assignment UUID, incarnation, and affected host. Stop the agent on that host and
   disable automatic restart so it cannot start additional work during inspection.
2. On that same host, inspect Docker containers using the exact label
   `auto-reviewer.worker=<WORKER_UUID>`. Verify each candidate's full immutable container
   ID and ownership labels. Terminate/remove only the verified owned IDs, then confirm
   their absence and that no worker-owned container remains. Retain a timestamped
   record identifying the host, IDs checked, and the completed absence verification.
   Also account for any operator-renamed containers by immutable ID/ownership label.
3. If host access, container ownership, or actual termination is uncertain, **stop here**:
   leave capacity quarantined. Revocation, expired leases, missing heartbeats, an empty
   coordinator Docker list, or an SSH connection alone are not termination evidence.
4. On the coordinator, the owner may record the verified termination:

   ```sh
   docker compose exec -T web python manage.py reconcile_worker_assignment \
     <ASSIGNMENT_UUID> --operator <OWNER_USERNAME> \
     --evidence 'Host node-7; UTC verification time; agent stopped/restart disabled; exact owned container IDs removed and absence verified; incident reference.' \
     --confirm-terminated
   ```

`--operator` must identify the configured active dashboard owner; it is audit attribution,
not a replacement for administrator access to the coordinator shell. Evidence must be
nonblank and at most 4096 characters. Include concrete verification facts or a retained
incident-record reference, **not credentials, tokens, model output, or repository content**.

The transaction locks the admission pool, worker, and assignment. It requires revocation,
marks unresolved work failed with confirmed cleanup, purges encrypted input/output, and
retains the operator identity, UTC timestamp, evidence, and digest in the assignment's
result audit. Only this explicitly reconciled assignment stops blocking its resource slot;
other unresolved work remains quarantined. Repeating the command for an already-cleaned
assignment changes nothing and cannot replace its original audit. Review the remaining
cleanup/drain status before reopening intake; re-enrollment and scheduling are separate,
explicit operations.
