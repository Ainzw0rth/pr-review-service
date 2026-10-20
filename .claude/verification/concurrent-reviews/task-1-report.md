# Task 1 — durable claims and bounded supervisor

Implementation commit: `fc76bbb` — `feat: retain fenced PR claims through bounded worker cleanup`.

## Delivered

- `ServiceSettings`: singleton default concurrency **2**, strict integer **1–8**, database bounds, persisted drain generation. Migration `0019` initializes settings and conservatively classifies interrupted legacy jobs/attempts.
- `JobClaim`: unique job and PR, immutable random token, owner incarnation, heartbeat and expiry. Terminal/cancelled/quota status never frees capacity. Admission locks settings, PR, then selected Job; PostgreSQL selection uses `SKIP LOCKED` and `OF self`, including nullable provider joins. No provider/network call runs inside a claim transaction.
- Same-host, stable-inode execution flocks acquired before publishing claim metadata; separate supervisor lifetime flocks. Expiry never proves death. DB-time lease predicates reject expired renewal. Recovery requires execution quiescence and successful scoped cleanup before deleting occupancy.
- Explicit token-aware current/finish, fenced attempts, diagnostics, activities, checkpoint/cursor/receipt/approval/journal persistence. Fresh Job objects cannot impersonate an existing claim. Release uses token identity and the same PR→Job lock order.
- Bounded eight-thread supervisor, global DB cap, per-thread DB closure, execution identity context, no hidden executor backlog. Claim survives complete worker finally (including journal and optional Sheets/merge). Exceptional execution retains the DB slot but explicitly closes its execution flock after thread cleanup so scoped recovery remains possible.
- Publication uses final local admission checks plus existing fresh GitHub guards. **No transaction spans network/model execution.** An already admitted bounded request may complete after cancellation; subsequent requests recheck authority. Short persistence fences preserve receipts without claiming external exactly-once or linearizable cancellation.
- Reconciliation, retry and admin cancellation use PR-first lifecycle locking. Retention excludes claimed jobs/checkpoints; protected claim references prevent accidental deletion.
- All-supervisor drain acknowledgments, retained-claim count, tracked global auxiliary quota/probe slot, aggregate `worker_token`, current generation acknowledgment, legacy drain-file import/mirroring. No startup bulk container deletion or blanket probe/attempt failure.
- Owner-scoped ModelProbe relationship and lifecycle lock support dead probe-owner recovery.

## Verification

Interpreter: `/root/projects/pr-review-service/.venv/bin/python`.

### Red

`python manage.py test core.tests.test_claims --noinput` before implementation: **4 tests; 3 missing-claims-module errors and 1 stale-token assertion failure**. Behavior covered two distinct claims/cap, retained same-PR cleanup exclusion, lowering capacity, stale token rejection and live expired-owner recovery refusal.

### Green

- `python manage.py test core.tests --noinput`: **609 tests, 63.918s, OK; 2 PostgreSQL-only skips**. Log: `task-1-suite-final.log`.
- Following final quarantine/DB-clock refinements and an additional PostgreSQL-only test: `python manage.py test core.tests.test_claims core.tests.test_auto_approval core.tests.test_journal core.tests.test_worker --noinput`: **190 tests, 12.738s, OK; 3 PostgreSQL-only skips**. Log: `task-1-final-focused.log`.
- Real PostgreSQL 17: `PYTHONPATH="$PWD/.claude/verification/concurrent-reviews:$PWD" python manage.py test core.tests.test_claims core.tests.test_deployment --settings=postgres_settings --noinput`: **19 tests, 3.560s, OK**. Log: `task-1-postgres.log`.
  - Four independently connected concurrent claimers obey global cap2.
  - Two executor threads overlap and retain both slots while terminal finally remains blocked.
  - Cleanup exception retains DB occupancy but releases execution flock.
  - Same-job retry rejects prior token's diagnostics/activity/checkpoint/finish.
  - Live expired claim cannot be recovered or renewed; abandoned publication becomes attention.
  - Probe owner requires lifetime-lock death proof; drain requires every supervisor and no retained claim; stale acknowledgments cannot be reused.
- Ruff checks on all changed Python files: **passed**.
- `DJANGO_SECRET_KEY=test python manage.py makemigrations --check --dry-run`: **No changes detected**.
- `git diff --check`: **passed**.

Existing tests were adapted where they deliberately changed terminal status/deleted a job without simulating cleanup release, expected blanket restart recovery, or mocked the old synchronous singleton command. Those expectations contradict retained execution ownership; replacement tests assert the new safety contract. Intermediate failures and their full logs remain in this verification directory.

## Required follow-on integration / review boundaries

1. **Task 2 must wire native cleanup.** `recover_dead_owners(cleanup=callback)` receives a `JobClaim`; `recover_owners(cleanup=callback)` receives a `WorkerOwner`. Neither invokes unsafe global cleanup. Without the callback, abandoned native-resource slots deliberately remain retained. Supervisor startup/periodic recovery wiring is still Task 2.
2. Task 2 must let unknown native cleanup failure escape the generic worker exception handling so `run_job` does not return normally and release occupancy. The supervisor already quarantines escaped failures and closes its execution-lock FD. Native account/graph/test capacity, container labels and scoped orphan cleanup are not delivered by Task 1.
3. Native identity APIs: `core.claims.current_claim_token()`, `current_owner_token()`, `execution_job`, `supervisor_owner`. For task threads `run_job` sets immutable execution context; probes/quota use supervisor owner context. `ModelProbe.owner` identifies probe ownership.
4. Settings UI/authorization belongs to Task 4. PostgreSQL production configuration/migration belongs to Task 3. No production command, container change, publication, push or deployment performed.
5. Full PostgreSQL application suite was not rerun here. Controller established pre-existing PostgreSQL issues (ExpectedLabel action width, nullable auth joins and a fixture sequence) for Task 3. Task 1 PostgreSQL claim/drain/concurrency checks are green.
6. No real GitHub/model request was made for verification. Deployment test output is mocked script behavior, not a deployment.

Unrelated plan/audit files were not included in the implementation commit.
