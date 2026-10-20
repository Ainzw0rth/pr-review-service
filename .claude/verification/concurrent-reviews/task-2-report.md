# Task 2 — shared account/resource capacity

## Result

Implemented shared same-host permits, active-time budgets, scoped native cleanup/recovery, periodic nonblocking quota refresh, and phase-specific waiting projection. No real model, auth-volume, GitHub, production, push, or deployment operations performed.

Implementation commit: `e6a9824` (follow-up evidence/cancellation preservation commit recorded in final handoff).

## Decisions / interfaces

- `core.capacity.permit(scope, limit, cancelled, *, on_wait=None, wait=True)` yields an immutable slot identity; `wait=False` raises `CapacityBusy`. Independent file opens + real nonblocking `flock`, stable files, close-on-exec, no unlink/truncation. Cancellation checked before scans and after acquisition. Wait callback emits only transitions and clears on cancellation.
- `ActiveBudget(seconds)` starts only inside `.active()`. Thread-safe elapsed inspection; waiting excluded, cleanup included, recovery sessions retain accumulated active time. Each API specialist and synthesis has its own 1800-second budget; CLI phases retain 1800 seconds across their recovery passes. Intent/legacy calls keep their existing unlimited-active-time policy. Probes get 90 active seconds.
- CLI scope derives from actual named auth volume, not provider row/model/kind. Both CLI kinds sharing the same volume contend. API identity is resolved on the controller before futures. HMAC credential identity ensures duplicate keys share a gate; optional account alias adds a second gate, never replaces the key gate. This also prevents conflicting aliases from bypassing duplicate-key limits.
- Task3 settings seams: `API_ACCOUNT_CONCURRENCY=3`, `GRAPH_CONCURRENCY=1`, `TEST_CONCURRENCY=1`; positive integers bounded to 32 in the adapter. Optional `API_ACCOUNT_GROUPS` is a settings mapping of string credential IDs to account aliases. API family remains part of identity. No credentials, volume names, paths, or exception output enter public waiting events.
- API threads queue response/activity/capacity events to the controller; bounded queue preserved. Each active budget checked independently. Threads join before slots are treated as available; transport/client closure remains inside permit. Existing auth/quota precedence retained; capacity uncertainty takes precedence and cannot trigger provider fallback.
- Native resources have deterministic per-slot names plus namespace/scope/slot/generation/claim/owner labels. Creation returns an immutable ID; start/inspect/remove use IDs. Every acquirer reconciles the exact slot first, validates labels (and `/auth` mount for CLI), and verifies absence after removal. Unknown containers and inspection/removal uncertainty fail closed. Legacy broad cleanup entrypoints now reject without touching Docker.
- Graph and tests have distinct pools; tests acquire before archive extraction. Graph timeout still starts after process launch. Existing archive validation, isolation flags, resource limits, graph cache behavior, and named auth volumes retained.
- Supervisor runs safe recovery callbacks at startup/periodically. `CapacityError` escapes worker generic failure handling, retaining the DB claim until death-proven scoped recovery. Probe/quota uncertainty retains auxiliary ownership. Quota refresh runs before intake even under continuous PR load, skips busy account permits without modifying snapshots, and does not serialize already-running model calls. Probes remain idle-only.
- Public events are static allowlisted waiting/resumed codes. Per-phase waiting flags clear on resume and terminal states; one waiting specialist cannot hide a running peer. Four technical cards, native paper/ink styles, existing motion/reduced-motion behavior, and Complete-versus-coverage distinctions preserved. Updated API-overlap copy to acknowledge available account capacity.

## Verification evidence

All commands ran from `/root/projects/pr-review-service/.worktrees/concurrent-reviews`; `PY` below means `/root/projects/pr-review-service/.venv/bin/python`.

### Red

`$PY manage.py test core.tests.test_capacity --noinput > .claude/verification/concurrent-reviews/task-2-red.log 2>&1`

Failed as expected: `ModuleNotFoundError: core.capacity` before implementation. Initial native integration exposed old mocks assuming random names/unscoped cleanup; tests were changed to a stateful Docker boundary double while retaining actual flock acquisition and sandbox assertions. Initial active-budget tests were updated to start the supplied adapter budget rather than assuming orchestration owns a wall-clock deadline. See `task-2-initial.log` and `task-2-integration-first.log`.

Browser capacity test initially expected “Running”; existing product label is “In progress”. Corrected test expectation, not UI wording. Evidence: `task-2-browser-capacity-red.log`.

### Green

1. Broad selected SQLite suites, **171 passed**:

   `$PY manage.py test core.tests.test_capacity core.tests.test_cli core.tests.test_cli_recovery core.tests.test_runner core.tests.test_graph core.tests.test_review_team core.tests.test_providers core.tests.test_v2_transport core.tests.test_worker_team core.tests.test_quota core.tests.test_dashboard core.tests.test_flow_v2 --noinput`

   Log: `task-2-green.log`.

2. After dual key/account gates, API/team regression: **65 passed**:

   `$PY manage.py test core.tests.test_capacity core.tests.test_providers core.tests.test_review_team core.tests.test_worker_team --noinput`

   Log: `task-2-api-final.log`.

3. Final immutable-ID absence verification/native regression: **56 passed**:

   `$PY manage.py test core.tests.test_capacity core.tests.test_runner core.tests.test_cli core.tests.test_graph --noinput`

   Log: `task-2-cleanup-final.log`. Capacity tests include actual process/thread locks, abrupt owner exit, stable inode reuse, caps, cancellation, key/account aliases, controller callback affinity, per-peer fake clocks, CLI recovery active-time accumulation, healthy-slot protection, exact orphan reconciliation, lost create replies, and fail-closed removal. Additional CLI recovery suite ran green in `task-2-native-final.log` (**61 passed** before last capacity additions).

4. PostgreSQL claim/capacity/quota suites: **36 passed**, no skips:

   `PYTHONPATH=.claude/verification/concurrent-reviews $PY manage.py test core.tests.test_capacity core.tests.test_claims core.tests.test_quota --settings=postgres_settings --noinput`

   Log: `task-2-postgres-focused.log`. This ran before the final extra recovery-budget/alias tests, subsequently covered on SQLite/native runs.

5. Browser regression with cached installed Playwright/Chromium: **1 comprehensive browser test passed**:

   `NODE_PATH=/root/.npm/_npx/31e32ef8478fbf80/node_modules node --test core/tests/activity_ui.test.cjs`

   Log: `task-2-browser-capacity.log`. Exercises keyboard disclosure/focus retention, polling, correct waiting/resume label, unaffected completed peer, history/coverage semantics, 1280×900 and 390×844 layouts, reduced motion, and mobile overflow. Screenshots manually inspected: `task-2-screens/waiting-desktop.png`, `task-2-screens/waiting-mobile.png`; no overflow or competing styles. Screenshot heartbeat warning is fixture age, not a production finding.

6. `$VENV/bin/ruff check .` — passed (`task-2-ruff.log`). Changed Python files: `$VENV/bin/ruff format --check` with the 23 changed Python files — passed (`task-2-format-changed.log`). `node --check static/core/activity.js` and source `git diff --check` — passed. Captured browser-red output contained two whitespace-only lines; trimmed in the evidence follow-up so the committed diff also passes.

7. `DATA_DIR=.claude/verification/concurrent-reviews/test-data DJANGO_SECRET_KEY=verification-only $PY manage.py check` — passed (`task-2-check.log`).

### Remaining / reserved

- PostgreSQL broader run: **41/43 passed** (`task-2-postgres.log`). Two worker-team tests reach saved-delivery attention: `test_incomplete_assessment_publishes_feedback_never_approval_merge_or_resolution`, `test_one_security_blocker_produces_one_request_changes_review`. Publication/PostgreSQL compatibility is reserved Task3; no publication code changed here. These tests pass SQLite. Parent informed.
- Repository-wide formatter reports four pre-existing files: `.claude/capacity-audit.md`, `.claude/concurrent-reviews-plan.md`, `core/management/commands/flush_journal.py`, `core/transcript.py`. Left untouched; changed files all formatted. Evidence: `task-2-format.log`.
- No real Docker daemon lifecycle test, real model/auth/probe call, deployment, or full all-project test run. Native lifecycle verified with a stateful Docker double; real thread/process flock tests use private temporary directories.
- Deployment contract: one Linux host/daemon, shared persistent DATA_DIR inode tree and identical SECRET_KEY/cap configuration. Different deployments require distinct secrets/namespaces. Never delete live lock files; flock is not FIFO. Drain before changing caps, rotating the capacity namespace secret, migrating legacy unscoped containers, or rolling back. Do not mix legacy broad-cleanup workers.
- `scripts/cli_login.sh` and `scripts/cli_auth.py` remain explicit operator-maintenance utilities, outside automatic worker paths. Require full worker drain before login/import/repair; parent to include this in Task3 deployment documentation. No credential-volume copies or automatic creation introduced.
- API blocking reads retain existing finite 360-second timeout; admission cancellation is prompt, transport cancellation is not claimed sub-second. Permits remain held until actual adapter cleanup.
