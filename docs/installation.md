# Installation

## Requirements

Use Docker Engine with the Compose plugin and Linux containers, Git, and Python 3 for the
standard-library-only environment initializer. No host Python dependencies are needed for the
application. Commands below use a POSIX shell from the project checkout.

Linux is the deployment target. Docker Desktop supplies a Linux VM on macOS/Windows; use a
compatible shell (for example WSL on Windows), Linux containers, and a working Docker socket.
Native Windows/Python deployment and ARM runner images are not verified here. CLI and graph
runners need their own image builds; do not assume architecture compatibility from the web image.
Allow capacity for PostgreSQL plus optional runners: each model session can use up to 2 GiB and
2 CPUs. Start with low concurrency.

Clone or download this repository, then enter its directory. Never copy another installation's
`.env` or credentials. Existing installations should use [deployment](deployment.md), not rerun
first-install steps.

## 1. Generate private configuration

Choose **one** mode.

### Local computer or private VPS access

```sh
python3 scripts/init_local.py
```

Dashboard: `http://127.0.0.1:8040`. This explicitly enables loopback HTTP without `DEBUG`.
Keep the Compose port bound to loopback; this mode is not for public HTTP access. On a remote
VPS, use an SSH tunnel from your computer, replacing the hostname and account:

```sh
ssh -L 8040:127.0.0.1:8040 your-user@your-server
```

### Public HTTPS domain

```sh
python3 scripts/init_local.py --mode server --public-url https://reviews.example.com
```

Replace the domain with your own. Configure DNS and an HTTPS reverse proxy/tunnel to
`http://127.0.0.1:8040` before opening the setup link. See [proxy setup](deployment.md#https-proxy).
The initializer does not provision domains, TLS or tunnels.

Both commands generate `.env` with new Django/encryption/database secrets and mode `0600`.
Existing `.env` is never overwritten. Keep an encrypted backup; losing `CREDENTIAL_KEY`
makes stored credentials and checkpoints unreadable. Inspect `DOCKER_GID` if the worker
cannot access your Docker socket.

## 2. Start PostgreSQL and the application

Use the PostgreSQL overlay for **every** Compose command. In this shell:

```sh
export COMPOSE_FILE=compose.yaml:compose.postgres.yaml
docker compose up -d --build
docker compose ps
docker compose logs --tail 100 migrate web events worker
```

Repeat the export in future shells. If you have a private `compose.override.yaml`, append it
last: `compose.yaml:compose.postgres.yaml:compose.override.yaml`. The equivalent explicit
prefix is `docker compose -f compose.yaml -f compose.postgres.yaml`.

The migration service must finish successfully; web, events, worker and maintenance then
start. PostgreSQL has no host-published port. Check `http://127.0.0.1:8040/healthz/` from the host.
`LIVE_WRITES=0` initially prevents live review publication.

## 3. Create the administrator and workspace

```sh
docker compose exec web python manage.py bootstrap_admin --setup-link \
  --username admin --email you@example.com --organization 'My workspace'
```

Use your own email/workspace. Open the private one-use URL printed by the command and set a
password within one hour. No default password exists. The command also reports the actual
`DASHBOARD_RETRY_USER_ID` to put in `.env`; **do not assume the ID is 1**. Set that value, then:

```sh
docker compose up -d --no-deps --force-recreate web events worker maintenance
```

This initial recreation is before reviews are enabled. Later changes require a drained
[deployment](deployment.md). The selected owner must remain an active superuser. Owner
Settings and Workflow are separate from ordinary organization-member access.

## 4. Make webhook ingress reachable

GitHub cannot deliver to localhost or an SSH-only dashboard. Local installations need a public
HTTPS forwarding endpoint; keeping the dashboard itself local is fine.

For a fresh install with a forwarding URL already known, add
`--webhook-url https://your-forwarding-host.example.com` to the initializer command. For an
existing `.env`, set `WEBHOOK_PUBLIC_URL` to that HTTPS origin without a trailing path and
recreate the services before enabling reviews. In local mode the forwarder **must override
the upstream Host header to `localhost` or `127.0.0.1`**; do not add its public hostname to
`ALLOWED_HOSTS` or turn on `TRUST_PROXY`. Restrict public forwarding to `/webhooks/github/`
when your forwarding service supports route rules.

For example, an already configured host-native Cloudflare Tunnel can forward to
`http://127.0.0.1:8040` with `--http-host-header localhost`. Tunnel provisioning/account setup
is external to this project. A tunnel container must use a reachable host gateway or join the
application network and target `http://web:8040`; its own `localhost` is not the application.

For public HTTPS server mode, leave `WEBHOOK_PUBLIC_URL` empty to use `PUBLIC_URL`, or set it
to a separate ingress origin. Ensure that proxy routing and accepted upstream Host agree.

## 5. Connect and enable

Follow [configuration](configuration.md): add a repository in Settings, register its webhook,
configure a model provider, verify the connection, then deliberately enable `LIVE_WRITES`.
The dashboard cannot configure provider accounts or perform CLI subscription login for you.
