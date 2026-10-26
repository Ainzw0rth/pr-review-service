# Remote workers

The coordinator keeps PostgreSQL, PR orchestration, account admission and GitHub
publication. A worker runs only the outbound HTTPS execution agent and disposable
Docker containers. No inbound worker port or SSH is required. Optional host-specific
SSH keepalive is separate: successful SSH is not proof of worker readiness or a
promise that a provider never suspends a machine.

## Prerequisites

- Linux amd64, local Docker at `/var/run/docker.sock`, Docker Compose v2, Python 3.
  Other architectures and remote/rootless Docker sockets are not verified.
- At least 5 GiB free **both** in Docker image storage and worker state storage.
  More may be needed for actual images/workloads. On InstaBox use persistent `/data`
  and check `docker info --format '{{.DockerRootDir}}'`; placing state under `/data`
  does not move Docker's image storage there.
- Public coordinator HTTPS origin with valid certificate and worker API routes
  reachable without an interactive login gateway. Never disable TLS verification.
- Owner-generated single-use pairing code (expires after ten minutes).
- Independently authenticate the same intended provider accounts on each host.
  Do not copy rotating Codex/Claude authentication files between machines.
- Coordinator remote scheduling is opt-in (`REMOTE_EXECUTION_ENABLED=1`). Leave
  disabled until enrollment, owner account-group bindings and nonpublishing
  verification are complete. Existing standalone setup remains the default.

The agent is a **trusted local Docker orchestrator**. Docker socket access grants
host-level control; keep worker configuration and host access private. Disposable
execution containers do not receive the socket or worker credential.

## Install from the same verified source revision

Source builds remain the default installation path. Verified Linux amd64 images are
published with [releases](https://github.com/Ainzw0rth/pr-review-service/releases),
including a `worker-release.json` digest manifest. The installer records immutable
image IDs; mutable tags are never accepted as execution pins.

```bash
python3 scripts/setup.py --mode worker --state-dir /data/review-worker
```

The installer prompts for coordinator origin, worker name, local CLI provider kinds
and a hidden pairing code. It checks platform, Docker, both storage locations and TLS;
enrolls once; durably saves `worker.json` mode 0600 inside a mode 0700 directory;
then builds images, guides independent provider login, validates agent configuration
and starts the worker-only Compose stack. No database/admin bootstrap runs. Config
contains a worker-scoped revocable token, **not** coordinator database/encryption or
GitHub publication credentials. Never paste it into tickets or shell arguments.

Select `none` for a worker without CLI providers. This avoids building CLI images;
it does not move centrally stored API keys to the worker. API-key providers continue
to execute on the coordinator. Such a worker is not model-ready until a supported
worker-local capability is explicitly configured. Graph/test capabilities are not
automatically advertised; configure pinned, locally available images only after
validating those workloads.

In the owner **Workers** screen, bind matching provider account groups explicitly.
Using the same subscription on both hosts requires the same group, not independent
limits. Confirm a fresh authenticated heartbeat and no readiness blockers. Installer
success means configured/started, **not** verified Ready or successful execution.
Run a nonpublishing validation before enabling the first live review slot.

## Resume and inspect

```bash
python3 scripts/setup.py --mode worker --state-dir /data/review-worker --resume
```

Enrollment is saved before builds/login. Resume preserves identity and completed
login receipts. Fresh setup refuses an existing identity. If an enrollment request
succeeds remotely but its response is lost before persistence, ask the owner to revoke
the orphan registration and issue a new code; do not assume a failed response means
that the original code remains unused.

`--no-start` builds/configures without starting the agent. `--skip-login` explicitly
defers provider authentication and does not establish readiness. `--noninteractive`
requires `--resume`; it never starts a provider login. For automation, provision via
interactive enrollment first, then use `--resume --noninteractive`. Failed subprocess
and HTTP response bodies are not printed, to avoid leaking secrets.

For manual Compose use, get the immutable runtime ID from the private `runtime.json`
receipt, then set `WORKER_AGENT_IMAGE` and the absolute `WORKER_STATE_DIR`. The agent
runs as the state owner, with the Docker socket group, so private mode-0600 files
remain readable without granting extra container capabilities. Always
supply the checked-out source path and bypass coordinator environment files:

```bash
WORKER_STATE_DIR=/data/review-worker WORKER_AGENT_IMAGE=sha256:<recorded-id> \
  WORKER_UID="$(id -u)" WORKER_GID="$(id -g)" \
  WORKER_DOCKER_GID="$(stat -c %g /var/run/docker.sock)" \
  docker compose --env-file /dev/null --project-directory /path/to/pr-review-service \
  -p worker-<worker-uuid> -f /path/to/pr-review-service/compose.worker.yaml ps
```

The installer resolves the Compose file and build context relative to its source
checkout, regardless of the caller's current directory. Keep that checkout available
for future source builds. Do not run a second installer/agent against the same state.

## Updates and recovery

Drain on the coordinator first; wait for active work and confirmed cleanup, including
unreachable assignments. An offline heartbeat is not proof that containers stopped.
Never remove state/auth volumes or reenroll to bypass uncertain execution. Follow the
coordinator's distributed deployment/recovery procedure before incompatible upgrades.

Before resuming an installed worker, drain it at the coordinator, confirm all assigned
work/cleanup is idle, then stop its agent Compose service. The installer takes the same
`agent.lock` as the runtime and refuses to build, change configuration or touch provider
authentication while an agent holds it. It does not stop an active worker for you.
Do not delete/bypass the lock file; an offline heartbeat is not proof the process stopped.
The lock stays held through configuration/authentication checks and is released immediately
before starting the configured agent.

Resume rebuilds the agent from the current checkout; previously recorded model image
IDs remain pinned. Updating a runner requires an explicit drained image/config update,
not silently retagging a running worker. If local provider login expires, drain and
perform login against that worker's existing auth volume; never copy another host's
credentials. Restart and verify fresh readiness before resuming admission.

## Versioned image releases

The `Worker images` workflow verifies amd64 coordinator, agent and CLI images. PRs and
manual runs with `publish=false` never publish. Publishing a GitHub release tagged
`vMAJOR.MINOR.PATCH`, or explicitly selecting `publish=true` for an existing release
at the checked-out revision, enables a separate publication job. Only that job receives
`packages: write` and `contents: write` for GHCR and the release manifest asset.
It publishes the exact images exported by the verification job, not rebuilt substitutes.
Existing image versions are refused; failed partial publication requires a new version.

Images use `ghcr.io/<owner>/<repository>/{coordinator,agent,cli}:<version>`; the release
asset `worker-release.json` records their immutable `repository@sha256:...` references,
source revision, protocol 1 and verified `linux/amd64` platform. Tags are convenience
labels, not an integrity pin. Install/update from manifest digests, never `latest`.
Registry publication has not yet been exercised here; no release availability or public
visibility is claimed. GHCR packages initially can be private; the release owner must
configure access/visibility. Private image pulls require local registry authentication.

The guided installer builds from source by default. To install a published release,
download `worker-release.json` from the intended repository's release assets, then check
out its recorded source revision. The manifest is trusted executable-code provenance;
do not import an arbitrary manifest. Only digest-pinned images under
`ghcr.io/ainzw0rth/pr-review-service` are accepted. The installer checks the exact manifest
schema, version, protocol, platform and checkout revision before enrollment or pulling.

```bash
python3 scripts/setup.py --mode worker --state-dir /data/review-worker \
  --release-manifest /path/to/downloaded/worker-release.json
```

For an existing worker, drain it, confirm all execution and cleanup idle, and stop the
agent first. Add `--resume` to the command above. The installer preserves worker identity,
provider login receipts and authentication volumes; a running agent blocks updates.
Release mode pulls the pinned agent and, only when CLI providers were selected, the CLI
runner. It never pulls the coordinator image. Images are resolved to immutable local
content IDs, and `runtime.json` records the release version and revision. No source build
runs in release mode. Private packages require prior local Docker registry login.
Omitting `--release-manifest` retains the source-build workflow. Neither path establishes
readiness automatically; verify fresh heartbeat, account bindings and scheduling in Workers.

Publication follows [GitHub's package workflow permissions](https://docs.github.com/en/packages/managing-github-packages-using-github-actions-workflows/publishing-and-installing-a-package-with-github-actions)
and [GHCR digest-pinned pulls](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).
