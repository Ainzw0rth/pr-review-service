# Distributed workers implementation progress

## Workspace

Branch: feat/distributed-workers
Path: /root/projects/pr-review-service/.worktrees/distributed-workers
Base: d82bf614dca75a881bee11f172b281fd175e30d8
Foundation commit 22c1b89 was pushed and deployed from this worktree on 2026-10-26; the original main checkout remains unchanged. Managed worktree creation failed because session cwd is /root; used ignored project-local Git worktree fallback.

## Implemented so far

- Standard-library execution envelope parser: strict version/operation/UUID fields, byte limit, duplicate-key and nonfinite-number rejection, detached JSON input/results.
- Local explicit-handler dispatch, cancellation before/after execution, safe errors.
- Model adapter uses the boundary; credentials, provider instances and stateful callbacks remain coordinator-local.
- Additive execution-worker and enrollment models with generated migration 0027.
- Hashed one-use ten-minute enrollment, credential authentication/revocation, bounded heartbeat validation and freshness/capacity readiness helpers.
- HTTPS-only enrollment/heartbeat endpoints, bounded JSON bodies, bearer-only authentication, revocation enforcement and isolated rate budgets. Invalid proxy-shared traffic cannot consume valid worker/pairing limits.

## Verification

- Before edits: 60 targeted existing tests passed.
- Full frozen-tree regression after endpoint integration: 830 tests passed, 7 skipped (SQLite; PostgreSQL-only checks skipped).
- After final rate-limit fix: 59 targeted tests passed on isolated PostgreSQL 17, including concurrent enrollment redemption and both reverse-proxy abuse regressions.
- Final Ruff check and format check passed (143 files); migration drift check reported no changes; git diff whitespace check passed.
- Independent read-only review found a shared-proxy rate-limit denial-of-service issue. Reproduced with two failing tests, fixed by isolating invalid traffic from authenticated/code-specific budgets, then re-reviewed successfully.
- One earlier broad run was invalidated by editing imported code during execution. Frozen-tree run above supersedes it. A missing staticfiles directory warning is expected in this source-only worktree; no UI has been changed or visually checked.
- Temporary PostgreSQL test container used an isolated tmpfs database; production database and containers were not used for tests.

## Not complete

Task 1 still needs complete operation-specific envelopes, test/graph integration and remote-independent runner extraction. Task 2 has registry plus enrollment/heartbeat endpoints and PostgreSQL enrollment-race coverage; local supervisor registration, owner pairing UI and complete capability/account binding remain. No remote scheduling, HTTP agent, UI, external keepalive, worker installer or distributed deployment changes exist yet. Foundation is deployed on Contabo, but remote review execution must remain disabled until the remaining tasks are implemented.

## Next

Complete operation-specific execution extraction, then durable remote assignment/account permits with PostgreSQL race tests. Preserve existing global account admission until replacement is fully tested.

## Live rollout — 2026-10-26

- User authorized push/deployment and explicitly requested preserving the running review. Deployment waited for its normal completion/cleanup; no cancellation or forced container termination was performed.
- Pushed feat/distributed-workers; application revision 22c1b8966af1aa94915399a8acf0ae37cc4a7be7 deployed using scripts/deploy.py from this worktree.
- Production configuration is linked from the original checkout; deployment lock is shared. Use this worktree and COMPOSE_FILE=compose.yaml:compose.postgres.yaml:compose.override.yaml for this deployment, not the older main checkout.
- Existing PostgreSQL volume and tunnel network preserved. Additive migration 0027 applied. Script made a private database backup and retained a rollback image; details remain in var/deployment.json.
- Deployment receipt phase healthy; intake reopened, zero active jobs at verification, new local supervisor acknowledged readiness. Health HTTP 200 locally, publicly and from InstaBox. Worker heartbeat without credentials returns 401; malformed enrollment returns 400. Running provider/API file hashes match revision 22c1b89. No startup tracebacks/CRITICAL log markers in four application services.
- InstaBox source staged at /data/workspace/pr-review-service at the same revision; Docker/Compose available. No remote agent started or enrolled (registered remote workers: zero). No coordinator credentials copied there.
- Host-specific operational keepalive installed on Contabo as auto-reviewer-instabox-keepalive.timer: fixed SSH command every 20 minutes, verified host key, private configuration, bounded timeout and redacted failure output. Initial and three scheduled checks succeeded. This is not the planned integrated keepalive UI and does not establish an uninterrupted-uptime guarantee.
- No live review was triggered solely for verification. Worker UI, remote execution and installer remain unfinished.
