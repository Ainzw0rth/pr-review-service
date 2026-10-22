# Troubleshooting

Start in the project directory with `export COMPOSE_FILE=compose.yaml:compose.postgres.yaml`
(and append your private override if present).

```sh
docker compose ps
docker compose logs --tail 100 migrate web events worker maintenance
docker compose exec -T web python manage.py worker_control status --json
```

Share sanitized errors, not `.env`, tokens, setup links, raw model streams or database contents.

## Guided setup stopped

Run `python3 scripts/setup.py --resume` after correcting the failed step. It resumes only
its own unfinished installation with an unchanged `.env`; it will not overwrite a production
environment. Do not delete `.env` or regenerate secrets to bypass that check.

- **Docker unavailable:** start Docker Engine and verify `docker info` and `docker compose version`.
- **Existing Docker installation detected:** use its original checkout and deployment guide.
  The wizard deliberately refuses another `auto-reviewer` stack/data volume.
- **Login failed:** resume repeats the provider login step. Complete the official browser/device flow.
- **Model test failed:** check subscription/API access, quota, exact model ID and network. Resume
  retries the test. You can skip it to finish setup, but access remains unverified; repair the provider
  in Workflow/admin before enabling publication. With exactly one enabled provider, retry with
  `docker compose exec -T worker python manage.py probe_provider`.
- **Lost password link:** use the explicit password recovery command below. Links expire after an hour.

## Application will not start

- **PostgreSQL missing:** use the overlay for every command; confirm `DB_ENGINE=postgresql` and
  matching DB settings. Inspect PostgreSQL and migration logs before restarting dependencies.
- **Missing or conflicting `.env`:** initializer refuses overwrites intentionally. Keep existing
  secrets; adjust configuration explicitly. Do not regenerate keys against an existing database.
- **Docker socket permission:** check that `DOCKER_GID` matches the socket group on the Docker host.
  The trusted worker needs the socket; web/model/test containers do not.
- **Image unavailable:** build the relevant app/CLI/graph image for the host architecture. Pin
  immutable runner image IDs that exist on the worker's Docker daemon.

## Login redirects, CSRF or missing Settings

Local HTTP requires the generated local mode and loopback hostname; `DEBUG=1` is not the fix.
Public HTTPS needs matching origin/host settings and a trusted proxy forwarding the HTTPS scheme.
Check that links use the correct `PUBLIC_URL`; the webhook origin is a separate setting.

Settings and Workflow require the active superuser whose actual database ID is configured in
`DASHBOARD_RETRY_USER_ID`. Set the bootstrap-reported ID, then recreate app services. A workspace
membership alone does not confer owner rights. For explicit password recovery:

```sh
docker compose exec web python manage.py access_link --user admin --hours 1
```

Replace `admin` with your username. The resulting URL is a private bearer credential. Creating
another access link for an unfinished bootstrap invalidates its old link.

## Repository saved, but no review starts

1. Check the repository is enabled, its exact name/reviewer match GitHub, and a model is enabled.
2. In GitHub webhook delivery history, confirm a real delivery succeeded; a registered webhook
   alone does not establish reachability. Localhost needs public HTTPS forwarding with the right
   upstream Host. Use **Verify connection** to repair registration with its generated secret and **Pull requests** events.
3. Ensure the PR is open and non-draft, reviewer requested, and `on review` applied. Both actions
   may occur in either order. Pushes/replies alone do not trigger work.
4. Inspect event/worker logs, dashboard operational note, current provider quota and drain state.
   Timeline mismatch backs off rather than trusting webhook arrival order.
5. `LIVE_WRITES=0` suppresses live publication. Once setup is verified, enable deliberately and
   apply environment changes through a drained deployment.

After fixing a connection, use **Verify connection** in Settings. This checks/configures the
connection; inspect an actual GitHub delivery separately. Pausing does not delete remote webhooks.
Old paused jobs do not automatically restart after repairs; an authorized explicit retry or fresh
human review request is needed.

## Provider failures or long waits

Check the provider kind, exact account-supported model ID, enabled priority and selected credential.
CLI providers require an empty API credential field, a pinned runner image and their private login
volume. Drain reviews/probes before repairing login with `scripts/cli_login.sh`, then rerun
`probe_cli` as described in [configuration](configuration.md).

Inspect model-attempt diagnostics for authentication, quota, refusal, incomplete evidence or
invalid output. API quota waits on the selected provider; CLI quota may skip other models sharing
its login. Do not weaken evidence validation to get a review through. Progress heartbeat proves
worker liveness, not model reasoning progress. Provider calls can be silent for extended periods.
Interrupt cannot undo charges or already-published GitHub actions.

## Approval or merge did not happen

An `approved` label is not formal approval. Both auto-approval and auto-merge are opt-in and
snapshotted for new jobs; toggles do not upgrade older jobs. The connected user cannot approve
its own PR. CI is advisory for review but mandatory when configured for automatic merging.
Inspect approval records, job delivery notes, reviewed SHA and GitHub branch rules/state.
Uncertain merge responses are not blindly retried: inspect GitHub first.

## Safe diagnostics and time display

The dashboard shows organization-scoped metadata and filtered public progress, not findings,
raw prompts, private reasoning or unfiltered terminal output. Provider diagnostics are admin-only.
New installations display timestamps in **UTC**, configurable with `TIME_ZONE`. Existing
installations without that setting retain **Asia/Jakarta (WIB, UTC+7)**. Stored/API timestamps are UTC.
Old attempts may lack events or checkpoint history; absent history is not reconstructed.

For production configuration checks:

```sh
docker compose exec -T web python manage.py check --deploy
```

Validated local HTTP intentionally omits HTTPS protections and suppresses only the corresponding
local deployment warnings. Do not use those settings on a public HTTP listener.
