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

## Updates

Review the intended revision and migration plan first. Keep a private backup of `.env`, preserve
both encryption keys, and retain auth volumes. Use the deployment script from the checkout on the
Linux Docker host; it requires Python 3 but no application virtual environment:

```sh
python3 scripts/deploy.py
```

It builds the candidate, checks migrations/deployment configuration, drains reviews, closes intake,
stops writers, backs up and applies migrations before starting and verifying new processes.
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
