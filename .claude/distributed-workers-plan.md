# Distributed Workers Implementation Plan

> For agentic workers: use the executing-plans skill to implement sequentially with verification checkpoints. Do not delegate unless separately authorized. Checkboxes track implementation, not planning completion.

**Goal:** Preserve easy standalone installation while adding optional, observable remote execution workers and monitored 20-minute InstaBox keepalive.

**Architecture:** Existing Django/PostgreSQL coordinator retains durable orchestration and publication. A shared execution package supports local and outbound-HTTPS remote agents; each host uses Docker Compose.

**Tech stack:** Python, Django, PostgreSQL, Docker Compose, existing templates/CSS/vanilla JS, OpenSSH. Prefer existing dependencies and standard library.

**Spec:** [Approved design](distributed-workers-spec.md).

## Global constraints

- One repo, two installation modes; default remains standalone.
- No remote database, coordinator encryption key, publishing credential or Docker socket exposure.
- Heartbeat 15 seconds; stale threshold 60 seconds; UI refresh 10 seconds while visible; InstaBox external keepalive 20 minutes.
- Initial remote capacity one review slot, separately bounded subprocess concurrency.
- Same-host locks cannot prove remote death; unavailable is not terminated.
- Preserve publication reconciliation and uncertain-execution quarantine.
- UI uses existing Martian Mono, paper/ink palette, 8px radii, 44px controls and reduced-motion behavior.
- No live deployment, provider login or publishing as a side effect of automated tests.

## Current seams and planned files

Existing: core/worker.py combines orchestration, persistence and delivery; core/providers.py and core/cli.py invoke inference; core/runner.py handles isolated tests; core/claims.py owns same-host PR claims; core/capacity.py uses local locks and Docker cleanup; core/cli_sessions.py stores session handles. scripts/setup.py builds/configures standalone installation; scripts/deploy.py assumes a local supervisor. maintenance currently processes unrelated work sequentially, so blocking SSH must not be inserted into that loop.

Create execution/ for protocol schemas, local execution adapter and remote agent runtime, with no Django ORM dependency. Create core/worker_registry.py, core/worker_api.py, core/execution_dispatch.py, core/worker_views.py and core/worker_keepalive.py for coordinator responsibilities. Add models/migrations in the existing Django app, tests under core/tests/, worker templates/assets, compose.worker.yaml and execution/Dockerfile. Extend existing setup/deployment docs and scripts rather than create a second repository.

Migration filenames must be generated from the actual migration head at execution time, not hard-coded against this planning snapshot.

## Task 1 — Establish execution contract without changing behavior

**Files:** core/worker.py, core/providers.py, core/cli.py, core/runner.py, core/review_orchestration.py; new execution/__init__.py, execution/protocol.py, execution/local.py, core/tests/test_execution_contract.py.

- [x] Capture baseline worker/provider/test/session checks; record any pre-existing failures before editing.
- [x] Define schema-version-1 envelopes: request_id, operation (model/test/graph), immutable input/artifact hashes, provider capability, account group, model configuration, session handle and execution limits. Results contain request_id, validated output, usage, session metadata and cleanup state; no ORM objects or raw secrets.
- [x] Add contract tests for malformed types, unknown operations, unsupported versions, oversized payloads and secret-free serialization. Test local cancellation and error translation.
- [x] Implement local adapter behind the existing call sites. Keep context gathering, checkpoints, progress persistence, final validation and GitHub writes on coordinator.
- [x] Verify existing worker_v2, worker_team, CLI, runner and session suites still pass. Do not rewrite the review engine as part of extraction.

**Deliverable:** unchanged standalone behavior through a serializable execution boundary.

## Task 2 — Registry and secure enrollment

**Files:** core/models.py, generated migration, core/worker_registry.py, core/worker_api.py, core/urls.py, reviewdesk/settings.py; tests test_worker_registry.py and test_worker_api.py.

**Records:** ExecutionWorker (UUID, incarnation, display name, local/remote, version, capabilities, heartbeat, drain/revoke state, capacity and bounded metrics); WorkerEnrollment (hashed code, expiry, consumed timestamp); hashed revocable worker credentials. Keep local WorkerOwner semantics distinct.

- [x] Test expired/replayed enrollment, concurrent code redemption, revoked credentials, wrong worker identity, unauthorized routes and oversized reports.
- [x] Add owner-issued enrollment with a 10-minute expiry; store only hashes for verification credentials. Mask operational secrets in output.
- [x] Implement /api/workers/v1/enroll/ and /api/workers/v1/heartbeat/ with explicit body validation, rate limiting and non-cookie authentication. Owner browser mutations retain CSRF protection.
- [x] Define heartbeat as a report for the authenticated worker/incarnation only; coordinator derives readiness and server-side timestamps. Bound capabilities and metrics to prevent arbitrary metadata growth.
- [x] Register local executor in the same fleet inventory without public enrollment or network dependence.

**Deliverable:** secure registry, local/remote identity and honest health state, but no remote production assignment yet.

## Task 3 — Durable remote assignments and account admission

**Files:** core/models.py, generated migration, core/execution_dispatch.py, core/worker_api.py, core/capacity.py, core/claims.py, core/cli_sessions.py; tests test_remote_assignments.py and test_distributed_capacity.py.

**Records:** ExecutionAssignment (PR job, execution request, worker/incarnation, unique token, generation, state, lease, result digest, cleanup status); durable account permits and worker session placement. Use PostgreSQL transactions/constraints for admission, not file locks across hosts.

- [x] Write PostgreSQL transaction tests for two claimers/one assignment, shared-account limits including probes, per-worker capacity and local/remote contention.
- [x] Implement claim, assignment heartbeat/progress, completion and failure endpoints under /api/workers/v1/. Grant work only to ready, compatible, authorized capability/account bindings.
- [x] Fence every accepted progress/result mutation by assignment token, worker incarnation and current PR generation. Same completion digest returns original acknowledgement; different digest conflicts.
- [x] Keep global PR claim on coordinator. Remote assignments are phase work; reserve review placement so one-slot workers cannot accidentally host many PRs through phase interleaving.
- [x] Centralize account permits for all execution entrants, including local/probe paths. Explicit account-group mapping is required to identify shared subscriptions across hosts.
- [x] Require cleanup acknowledgement before releasing uncertain capacity; restart reconciliation quarantines unknown running work. Lease expiry alone is insufficient. Add bounded deadlines and worker-side fail-closed cancellation on lost renewal.
- [x] Preserve session affinity; on unavailable history deliberately create a new session key rather than silently reuse a nonexistent session. No shared auth filesystem.

**Deliverable:** safe scheduling and result acceptance independent of worker transport failures.

## Task 4 — Worker runtime and bounded artifacts

**Files:** execution/agent.py, execution/storage.py, execution/protocol.py, execution/local.py, execution/Dockerfile, compose.worker.yaml; core/worker_api.py; tests test_execution_agent.py and test_worker_artifacts.py.

- [x] Test agent reconnect, process restart, persisted pending completion, cancellation and lost acknowledgement without re-execution.
- [x] Implement outbound HTTPS polling with backoff, separate 15-second heartbeat and bounded request timeouts. Validate coordinator TLS; keep worker state mode 0600.
- [x] Authorize artifact download per assignment; check length/hash before use and preserve existing archive traversal/symlink/decompression defenses. Do not accept arbitrary remote Docker flags, mounts or shell commands from worker enrollment.
- [x] Run disposable containers with current security restrictions; agent alone has local Docker access. Pin execution images and advertise validated capability labels.
- [x] Persist session/config state; cap caches/logs and clean only agent-owned finished work. Stop admissions on low storage or insufficient resources. Report unavailable metrics as unknown.
- [x] Keep provider logins local. For existing centrally stored API providers, retain local execution initially unless an explicit worker-local account binding has been configured; never silently distribute stored secrets.
- [x] Test model, test and graph operations through the remote adapter; disable unimplemented capabilities rather than advertise partial support.

**Deliverable:** one worker can execute a nonpublishing request and safely return its result.

## Task 5 — External keepalive scheduler

**Files:** core/worker_keepalive.py, core/models.py, generated migration, core/management/commands/worker_keepalive.py, compose.yaml, core/worker_api.py; tests test_worker_keepalive.py.

- [x] Add encrypted private connection configuration, verified host-key reference, enabled flag, interval, next_due_at, last attempt/success and sanitized outcome.
- [x] Run a dedicated coordinator management-command service so SSH delays cannot block existing maintenance or heartbeat handling.
- [x] Claim due checks atomically with expiring scheduler ownership; persist next due time and recover overdue checks after restart. Default enabled InstaBox interval is 1200 seconds; other workers opt in.
- [x] Invoke fixed SSH argv with BatchMode, strict known-host verification, bounded connect/command timeout, disabled forwarding and no shell interpolation. Enrollment cannot supply arbitrary endpoints; owner configuration validates host/user/port and prevents option injection. No unrestricted generic URL fetcher.
- [x] Never place token-bearing arguments in diagnostics. Limit retries and distinguish expired authentication, host-key mismatch, timeout and successful command.
- [x] Provide owner-only Check now, deduplicated with periodic attempts. SSH success updates connectivity, not worker Ready status.
- [x] Unit-test due scheduling, overlap prevention, coordinator restart, retry bounds and secrets redaction. Later run an authorized live idle-window experiment; retain explicit unverified behavior until measured.

**Deliverable:** reliable external 20-minute checks with visible evidence and failure states.

## Task 6 — Theme-native worker management UI

**Files:** core/worker_views.py, core/urls.py, templates/core/base.html, templates/core/workers.html, templates/core/worker_detail.html, static/core/workers.css, static/core/workers.js; tests test_workers_ui.py and test_worker_permissions.py.

- [x] Reuse current owner authorization pattern; test anonymous/member denial for fleet data and every mutation. Do not expose repo activity or credentials to unauthorized viewers.
- [x] Build summary, responsive table/cards and detail disclosures using existing tokens. Show local worker from first install; provide useful no-remote-worker onboarding.
- [x] Implement pairing, drain, resume scheduling, check now, keepalive configuration and revocation controls. Explain that drain/revoke are not remote machine shutdown.
- [x] Render availability separately from readiness reasons; show active PR/phase, slot use, RAM, working-disk free space, capabilities/version, heartbeat age and keepalive schedule/result.
- [x] Poll every 10 seconds only while visible; abort or finish prior request before another, back off on failure, preserve focused controls/expanded details and mark stale snapshots visibly.
- [x] Reuse motion tokens for disclosures and status changes; disable under reduced motion. Announce actionable transitions politely, not every timer tick.
- [x] Verify empty/loading/error/success/disabled states and keyboard access. Capture 375/768/1440px renders, check overflow and interaction/motion with reduced motion on/off. Store evidence under .claude/verification/.

**Deliverable:** owner can understand and operate fleet without reading logs for routine states.

## Task 7 — Guided installation and versioned releases

**Files:** scripts/setup.py, new scripts/setup_worker.py, scripts/cli_login.sh, compose.worker.yaml, execution/Dockerfile, release build workflow under .github/workflows/, README.md, docs/quickstart.md, docs/installation.md, new docs/workers.md; tests test_worker_setup.py and existing test_guided_setup.py.

- [x] Preserve standalone default and --resume behavior. Offer worker mode without rerunning database/admin bootstrap.
- [x] Prompt URL/name/pairing code privately; preflight platform/Docker/disk/TLS, enroll once, persist returned credential before subsequent steps and never echo it.
- [x] Resume interrupted enrollment/configuration/login without creating duplicate workers. Warn clearly on incompatible existing installation instead of overwriting it.
- [x] Guide independent CLI login/account binding and check capabilities before declaring Ready. Provide an API-key-only path without installing unnecessary CLI images.
- [x] Build and publish pinned coordinator/agent/runner release images for tested architectures; do not claim ARM support before native verification. Pull images by release manifest/digest; keep source builds documented.
- [x] Verify worker-only stack contains no database, web dashboard or coordinator secrets. Document outbound HTTPS and optional host-specific keepalive, not mandatory SSH for ordinary workers.

**Deliverable:** fresh standalone setup remains simple; a second host joins through one guided flow.

## Task 8 — Upgrade, drain, recovery and rollback

**Files:** scripts/deploy.py, core/management/commands/worker_control.py, core/deployment.py, core/execution_dispatch.py, docs/deployment.md, docs/troubleshooting.md; tests test_distributed_deployment.py.

- [x] Test fleet drain with running remote work, offline worker with uncertain assignments and mixed protocol versions.
- [x] Stop new remote/local admissions during incompatible upgrades; require all active execution/cleanup accounted for before migration. Do not treat an empty local Docker list as fleet quiescence.
- [x] Retain deployment gate until new coordinator and eligible agents acknowledge supported versions. Use additive migrations first and explicit compatibility checks.
- [x] Document rollback: disable remote assignment, fence outstanding tokens, quarantine uncertain execution, restore only schema-compatible application versions, then verify local readiness. Never automatically restore DB over accepted work.
- [x] Preserve local processing when remote capacity is absent, except account groups with unconfirmed cleanup. Expose actionable recovery state and audited operator reconciliation.

**Deliverable:** deployment/recovery tools understand remote execution and do not strand or duplicate work silently.

## Task 9 — Integrated verification and controlled rollout

- [x] Run Django suites against PostgreSQL for lock/admission tests, existing unit tests, lint/format and migration consistency checks. Use isolated test data, never production DB as a harness.
- [x] Run two-worker scenario matrix: simultaneous claims; old incarnation reconnect; coordinator restart; lost completion response; expired/revoked credentials; stale result; provider quota exhaustion; worker OOM; disk full; session migration/fresh start; network partition during cleanup.
- [x] Verify normal publication, crash-window reconciliation, stale PR generation and no duplicate accepted result. Idempotent internal completion does not itself prove exactly-once GitHub side effects.
- [x] Fresh-install and interrupted-resume checks for both modes; verify no secrets in logs, browser responses or release artifacts.
- [x] Render/interaction checks from Task 6; document actual results and omissions.
- [x] Obtain current dedicated worker/provider/keepalive credentials through private setup, not copied conversation tokens. Configure InstaBox working storage under /data and inspect actual Docker storage placement/space before pulling images.
- [x] Run nonpublishing remote review. Validate keepalive across at least two 30-minute idle windows, record provider state if available and independently observe fresh agent heartbeat.
- [x] Present concrete deployment result and test evidence before live rollout approval; then enable one remote slot on a designated test repository. Monitor memory, disk, queue wait and account limits before increasing capacity.

## Verification commands

Run from project root in an isolated development environment:

```bash
.venv/bin/python manage.py test core.tests.test_execution_contract core.tests.test_worker_registry core.tests.test_worker_api core.tests.test_remote_assignments core.tests.test_distributed_capacity core.tests.test_execution_agent core.tests.test_worker_artifacts core.tests.test_worker_keepalive core.tests.test_workers_ui core.tests.test_worker_permissions core.tests.test_worker_setup core.tests.test_distributed_deployment --noinput
.venv/bin/python manage.py test --noinput
.venv/bin/ruff check core execution reviewdesk manage.py scripts
.venv/bin/ruff format --check core execution reviewdesk manage.py scripts
DJANGO_SECRET_KEY=local-check-only .venv/bin/python manage.py makemigrations --check --dry-run
```

New test module names above are implementation targets, not existing passing checks. Configure an isolated PostgreSQL test database for concurrency tests. Add the existing release workflow's YAML/container checks when that workflow is inspected; never print a fully resolved secret-bearing Compose configuration.

## Planning review and remaining operator input

All approved requirements map to Tasks 1–9. No new implementation dependency or architecture exception is authorized by this document. Before account configuration, determine whether InstaBox uses separate provider accounts or the same account groups; the design supports both, with shared global limits for shared accounts. Before live keepalive use, verify current dedicated credential validity and host fingerprint. These do not block code implementation.

Implementation underway on feat/distributed-workers; see distributed-workers-progress.md for actual verification and remaining work. Checkmarks identify completed substeps only, not completion of the overall distributed service.
