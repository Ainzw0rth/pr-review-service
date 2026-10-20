# Concurrent PR Reviews Implementation Plan

> **For agentic workers:** Use subagent-driven-development task gates; no repeated user approvals for already authorized work.

**Goal:** Run bounded concurrent PR workflows with safe ownership and an owner-editable capacity setting.

**Architecture:** PostgreSQL remains the durable queue and owns short job-claim transactions. A small supervisor runs independent job threads; live claim tokens fence state and retain per-PR/global capacity through cleanup. Shared-volume resource locks bound model accounts and native containers.

**Tech Stack:** Django 5.2, PostgreSQL 17, psycopg 3, Python threads/context managers/flock, existing Docker Compose and templates.

**Spec:** `.claude/concurrent-reviews-spec.md`

## Global Constraints

- Default **2 concurrent PR reviews**, owner-editable **1–8** in Settings. Changes govern new claims; lowering the limit never cancels active work.
- At most one owned generation per PR. Different PRs in the same repository may overlap. Ownership survives cancellation/completion until all cleanup/finalization finishes.
- Waiting for capacity is cancellable, visible in technical progress, and is not quota, fallback, Skipped or analysis-budget time.
- Preserve three specialist roles, sequential CLI / parallel API reviews, adjudication, doubled 30-minute phase budget, generation/head/publication guards, encrypted checkpoints, receipt-aware delivery, privacy and container restrictions.
- No Redis/Celery/new orchestration framework. Reuse Django/PostgreSQL, standard-library threads/flock and existing job/checkpoint lifecycle.
- Same-host shared data-volume file locks may contain local native-container resource slots. Never unlink held lock files; never bulk-delete healthy containers. Multi-host execution is not part of this release.
- Commit, push and deploy already authorized. No artificial production PR review, model request or GitHub publication for verification. Do not interrupt an active review for rollout.

---

### Task 1: Durable claims and bounded supervisor

**Files:** `core/models.py`, migration `0019_*`, `core/claims.py` (new), `core/workflow.py`, `core/worker.py`, `core/activity.py`, `core/delivery.py`, `core/publication.py`, `core/merging.py`, `core/management/commands/review_worker.py`, `core/management/commands/worker_control.py`, focused ownership/pool tests. Preserve existing external contracts where possible.

**Interfaces:** Produce singleton `ServiceSettings.concurrent_pr_reviews` (default2, constraints1–8), `claim()` compatible with existing callers, token-aware `current/finish`, explicit release after cleanup, all-worker drain acknowledgment. Keep resource progress codes from Task2 allowlisted. Model/backend settings are Task3; owner form Task4.

- [x] Write failing behavior tests: two distinct PRs claimed, third queued at limit2; two generations of samePR never overlap even if first cancelled/completed but cleanup not released; lowering2→1 permits no new claim until below1; stale token cannot finish, persist diagnostics/checkpoints or publish; live worker not recovered; abandoned publication gets attention. Use real DB transactions for races when PostgreSQL available.
- [x] Implement short globally bounded claim transaction. Lock singleton settings row, choose eligible job with skip-locked/self-only row lock, exclude every PR with retained live claim, create fresh claim token. No transaction surrounds provider/network work.
- [x] Implement owner heartbeat, same-host execution lock/deadowner proof, scoped recovery, finally release and bounded supervisor threads with closed per-thread DB connections. Retain claim through terminal Sheets/merge/journal/container cleanup; refuse stale mutation paths.
- [x] Replace single-token drain assumptions with all live worker acknowledgments and zero retained claims/probes. Preserve old `worker_token` compatibility as aggregate identity for deploy transition.
- [x] Run focused and existing worker/workflow/delivery/merge/deployment tests; record actual red/green commands, commit task.

Critical claim behavior:
```python
first = claim()
second = claim()  # distinct PR, cap=2
assert first is not None and second is not None
assert claim() is None
finish(first, 'completed')
assert claim() is None  # cleanup still owns its capacity
release(first)
assert claim() is not None
```

### Task 2: Account/resource capacity and active-time budgets

**Files:** `core/capacity.py` (new), `core/cli.py`, `core/providers.py` if necessary, `core/review_orchestration.py`, `core/quota.py`, `core/model_probe.py`, `core/runner.py`, `core/graph.py`, direct probe/warm commands, `core/flow.py`, `static/core/activity.js` and focused capacity/team/progress tests. Task2 integrates optional adapter callbacks at worker call sites after Task1 commit. Worker call-site integration coordinated with Task1 owner; do not overwrite unrelated ownership changes.

**Interfaces:** Produce `permit(scope, limit, cancelled, *, on_wait=None, wait=True)` and `ActiveBudget(seconds)`; adapters gain optional `on_capacity`/`budget` keywords. Resolve shared identity before API threads; native gate covers each CLI session and retains one active budget across recovery. Shared CLI-volume identity, API account/credential fingerprint identity, bounded graph/tests. Task3 adds env bounds if needed. `Progress` receives allowlisted waiting/ready events; provider thread callbacks stay queued to controller.

- [x] Write failing tests using independent threads/processes and actual flock files: shared CLI volume/model rows serialize; separate accounts proceed; cap cannot exceed configured count; cancellation releases wait without acquiring/calling provider; probes/quota use same lock; healthy containers never globally cleaned.
- [x] Acquire actual slot before starting phase deadline; API phases get active-time clocks per started invocation, synthesis still waits for all reports. Keep timeout/auth/quota precedence, bounded callback queues and controller-thread state writes. Waiting produces no fallback or skipped attempt.
- [x] Tag/contain native containers per acquired resource slot. Cleanup only under exclusive ownership; deterministic names or equivalent prevent late orphan create/name races. Cleanup failure fails closed and cannot expose credentials.
- [x] Cover review+recovery and all direct CLI/quota/probe paths, graph/tests and warmups. Separate graph/test limits; preserve per-repo graph cache lock and sandbox args.
- [x] Run focused and existing native runner/orchestration tests; record evidence, commit task.

Capacity waiting behavior:
```python
with slot('cli-volume:shared', 1, cancelled):
    # Only after entry: start the invocation's 1800s active deadline.
    result = call_model()
```

### Task 3: PostgreSQL, transfer and deploy safety

**Files:** `reviewdesk/settings.py`, `requirements.txt`, `requirements.lock`, `Dockerfile`, `compose.yaml`, `.env.example`, `core/management/commands/backup.py`, new transfer command, `scripts/deploy.py`, new cutover helper if necessary, `README.md`, database/deployment tests.

**Interfaces:** PostgreSQL selected by explicit environment configuration; SQLite fallback for local development. Backups support selected backend. Transfer from offline SQLite source to fresh migrated PostgreSQL target preserves every table/PK/ciphertext, validates counts/content/FKs, resets sequences and refuses a populated target. Normal deploy accepts reviewed schema migrations only while all writers stopped/drained.

- [x] Write failing config/backup/deploy/transfer tests; include real PostgreSQL transfer with booleans, UUID, JSON, timezone values, M2M rows, permissions/contenttypes, session/credential/checkpoint/receipt preservation and rollback on mismatch.
- [x] Add psycopg3 and private PostgreSQL17 Compose service/volume/healthcheck, no public DB port. Do not place secrets in code/receipts/logs. Add explicit account/graph/test capacity env bounds consumed by Task2.
- [x] Implement consistent private backup and a typed offline transfer with complete validation and sequence reset. Never delete/overwrite original SQLite or existing PostgreSQL records. No simultaneous old/new writers.
- [x] Adapt drain-aware deploy for all worker identities/migrations, retaining safe image+DB rollback and failclosed paused state. Document staged cutover, incompatible schema rollback limits and downtime/webhook retry behavior.
- [x] Run real PostgreSQL queue/transfer/race tests plus focused deployment tests, commit task.

Verification examples:
```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test core.tests.test_concurrency core.tests.test_database_transfer --noinput
```
Use actual test module names from implementation in report.

### Task 4: Settings control and end-to-end verification

**Files:** `core/settings_views.py`, `templates/core/settings.html`, existing CSS only if needed, `core/tests/test_concurrency_settings.py`, integration test/verification helpers under `.claude/verification/concurrent-reviews/`.

**Interfaces:** Consume `ServiceSettings.concurrent_pr_reviews` and lock global row before repositories consistently with claims; include value in stale form revision; form omission preserves current value for backwards compatibility. Owner-only validated1–8. Saving applies atomically with existing repository policies.

- [x] Write failing owner/stale-form/range/atomicity tests. Zero, negative, decimal, malformed, empty and >8 reject400 without changing repository policy; other superusers403; concurrency-only changes invalidate stale forms409.
- [x] Add concise Service capacity card with label **Concurrent PR reviews**, numeric field1–8, current active/waiting context only if useful, explanatory copy: new jobs only; lowering doesn't interrupt running reviews; shared account capacity can still serialize model calls. Reuse paper/ink tokens, controls and existing motion/reduced-motion.
- [x] Exercise rendered Settings at desktop/mobile with keyboard focus, save/error/success, no overflow and reduced motion; keep previous four technical cards/phase delivery statuses intact.
- [x] Run full Django suite on SQLite and PostgreSQL where applicable, Ruff lint/format, migrations/check, browser regressions; independent task/final review. Fix evidence-backed defects, rerun only covering checks after fixes.
- [ ] Commit/push, preserve current production checkout private env/override, drain naturally, private backup and verified database cutover with writers stopped, initial cap1 smoke then cap2. Verify owner settings/read-only historical jobs, health/services/restarts, queue resumption. Record deployment receipt without secrets; never fabricate a review to prove concurrency.
