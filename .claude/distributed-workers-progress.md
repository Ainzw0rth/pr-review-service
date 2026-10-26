# Distributed workers implementation progress

## Workspace

Branch: feat/distributed-workers
Path: /root/projects/pr-review-service/.worktrees/distributed-workers
Base: d82bf614dca75a881bee11f172b281fd175e30d8
Production checkout and running containers have not been changed. Managed worktree creation failed because session cwd is /root; used ignored project-local Git worktree fallback.

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

Task 1 still needs complete operation-specific envelopes, test/graph integration and remote-independent runner extraction. Task 2 has registry plus enrollment/heartbeat endpoints and PostgreSQL enrollment-race coverage; local supervisor registration, owner pairing UI and complete capability/account binding remain. No remote scheduling, HTTP agent, UI, external keepalive, worker installer or distributed deployment changes exist yet. Nothing is deployed; do not enable remote work from these foundations.

## Next

Complete operation-specific execution extraction, then durable remote assignment/account permits with PostgreSQL race tests. Preserve existing global account admission until replacement is fully tested.
