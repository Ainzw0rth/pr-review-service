# Subscription CLI runner operations

## Install and authenticate

Run from the project directory on the VPS, not your laptop:

```bash
docker build -f cli/Dockerfile -t auto-reviewer-cli:local .
docker image inspect auto-reviewer-cli:local --format '{{.Id}}'
bash scripts/cli_login.sh codex
bash scripts/cli_login.sh claude
```

Put the printed immutable image ID in `.env` as `CLI_RUNNER_IMAGE`. The login commands use
official subscription login flows and persist authentication in separate private Docker volumes.
Complete browser/device authorization yourself. Do not share tokens, browser sessions or passwords.
The service does not implement a third-party OAuth client and does not copy personal CLI settings.
For initial migration only, `scripts/cli_auth.py` can seed an existing official credential cache;
dedicated login is preferred to avoid sharing rotating refresh tokens with an interactive CLI.

The image pins CLI versions. Rebuild and reverify intentionally when upgrading. It includes CA
certificates for HTTPS. The worker alone controls Docker; neither the web app nor model runner
receives the Docker socket. A model runner receives only its provider's authentication volume.

## Verify before enabling

Deploy the application migration and new image/settings. Use the worker for a no-publication probe:

```bash
docker compose exec -T worker python manage.py probe_cli codex_cli gpt-6.1-sol
docker compose exec -T worker python manage.py probe_cli claude_cli claude-opus-5-5
docker compose exec -T worker python manage.py probe_cli claude_cli claude-opus-4-8
```

These model IDs are requested configuration, not a guarantee of account availability. Enable only
verified connections in platform admin. Choose **Codex CLI** or **Claude Code CLI**, leave credential
empty, set the exact model ID and priority. Disable superseded API connections to avoid paid API
fallback. Retain old provider records for historical attempts. An empty synthetic probe should
produce validated structured output with `complete: false`; it is not a real PR review.

For full integration verification, `probe_cli --context PATH --result PATH` accepts previously
collected context and saves a validated result with private permissions. It never enqueues work or
publishes to GitHub/Sheets. Keep context/results in ignored private storage, not Git. Remove when
no longer needed. Never print raw CLI streams or credentials into shared logs.

## Runtime behavior

- One queued review at a time. Each model attempt uses a fresh non-root, resource-limited container.
- A fixed read-only MCP tool supplies bounded ranges of collected evidence. No shell, arbitrary
  paths, web tools, repository hooks or external write tools are enabled.
- Only that tool is pre-approved. CLI configuration is isolated from personal/project settings.
- Independent coverage tracks evidence supplied; incomplete delivery cannot produce an accepted
  review. Counters do not expose private model reasoning and do not prove comprehension.
- Dashboard nodes show backend, structured-event count and evidence-delivery counts. Findings and
  review explanations remain on GitHub. Raw events and model text are not stored as progress logs.
- Existing eligibility, commit, label, CI and publication checks remain mandatory.
- Provider outage/refusal follows the existing ordered route. Quota waits on that connection;
  unknown reset defaults to the existing next-check interval. Incomplete evidence gets one fresh
  recovery pass with missing artifact IDs prioritized; if still incomplete, routing falls forward.
  Coverage resets for the new session and must be complete independently. Auth/config/other invalid output pauses
  with an explicit reason. There is no silent API billing fallback.
- No whole-review wall-clock timeout. Admin interrupt stops/removes the runner. Process, memory,
  input and event-size limits still apply. Authentication refresh stays inside the private volume.
- Worker startup removes orphan review containers before recovering interrupted attempts. Do not
  restart the worker while an intentional diagnostic CLI probe is running.

## Recovery

For expired/revoked login, rerun the corresponding `scripts/cli_login.sh` on the VPS, then probe.
For `cli_setup`, check the pinned image exists on the worker's Docker host and its auth-volume
setting matches the actual volume. For `cli_coverage`, inspect the recovery pass and later model attempts. Each CLI model gets at most
one extra coverage-recovery pass. Do not weaken coverage or publish an incomplete response. For quota, inspect the provider account
and dashboard next-check time. No credentials belong in screenshots or error reports.

Deploying or fixing credentials does not restart an old paused job. The designated owner can use
**Retry review**, or a developer can request the reviewer and retoggle `on review`. A retry still
requires fresh eligibility and creates a separate attempt. No formal GitHub approval is automated.

## Current verified configuration (2026-10-06)

Opus 5.5 → Sol 6.1 → Opus 4.8, all official subscription CLI backends. Both Opus connectivity probes
passed after VPS login. A real-PR Opus 5.5 probe emitted an explicit refusal; the controller now
terminates that runner immediately and reports decline to the existing fallback route. Sol previously
completed the full PR probe. Probe results never publish. API provider records remain disabled.

## Optional final-summary journal

Service-side journaling uses the existing journal submission API, not host CLI Stop hooks.
Configure the repository allowlist, endpoint and encrypted submission credential as documented in
README. Only accepted final review summaries or unsuccessful-run operational outcomes are queued;
raw CLI transcripts and rejected model text are excluded. Delivery runs separately in maintenance.
