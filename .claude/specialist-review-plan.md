# Specialist review orchestration

Approved design: three independent specialist reviews, followed by evidence-based
adjudication and the existing guarded GitHub publisher.

## Constraints

- New workflow revision `v3`; preserve saved legacy/v2/v2.1 behavior and delivery retries.
- Shared commit-pinned evidence and validated grounded intent; no peer findings in specialist input.
- Fixed code/security/tests roles; existing provider adapters only, no new dependencies.
- API fan-out limited to three calls. CLI roles run sequentially against the shared auth volume.
- All callbacks touching Django run on the worker thread, not provider threads.
- A required reviewer failure, incomplete coverage, malformed finding or unresolved blocker
  cannot produce clearance. Existing fallback/quota/cancellation/publication rules remain.
- Synthesis must account for every candidate finding, retain attribution, justify rejection,
  and verify against original evidence; no majority voting or speculative coverage demands.
- Private reports/checkpoints encrypted; public progress and token counters allowlisted.
- Bound new review phases in time; retain existing context/output-size and API token limits.
- No production deployment or live GitHub/model calls during implementation.

## Execution

- [x] Contracts: add `core/review_team.py` schemas, role policies, contextual evidence
  validation and synthesis accounting. Tests: `core.tests.test_review_team`.
- [x] Execution: add bounded orchestration with isolated inputs, API fan-out, serialized
  CLI execution, cancellation and worker-thread callbacks. Test failed/incomplete reviewers,
  rejected evidence, unresolved blockers, duplicates, cancellation, deadlines and isolation.
- [x] Integration: route v3 jobs through specialists/synthesis after intent; preserve
  provider fallback and delivery checkpoints. Test `core.tests.test_worker_team`; pin older
  workflow fixtures to their historical revision.
- [x] Progress: snapshot v3 stages and allowlisted phase usage; reuse existing dashboard
  components/motion. Verify stage states, retry resets, privacy and five-phase accounting.
- [x] Verification: focused tests, full Django suite, Node tests, Ruff, Django checks,
  migration drift check, rendered desktop/mobile and reduced-motion inspection if available.

## Checks

```bash
.venv/bin/python manage.py test core.tests --noinput
node --test cli/*.test.mjs
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
```

Quality improvement versus the previous reviewer requires a later human-scored comparison
on representative PRs; implementation tests do not establish model-review quality.

## Verification results — 2026-10-19

- Full Django suite: **580 passed**. CLI Node suite: **16 passed**.
- Focused new contracts/orchestration/transport/worker checks: **22 passed**, included
  in the full suite. Includes single-reviewer security blocking, malformed citations,
  complete candidate accounting, peer cancellation, mixed quota/auth failure precedence,
  provider fallback, historical routing, and delivery-only retries.
- Ruff lint passed repository-wide. All **15 changed/new Python files** pass Ruff
  formatting. Repository-wide formatting flags two unchanged baseline files:
  `core/management/commands/flush_journal.py` and `core/transcript.py`.
  Confirmed the same format failures using their `HEAD` versions; left untouched.
- Django system checks passed; migration drift check reports no changes.
- Independent read-only review completed. Compact CLI JSON citation mismatch reproduced
  with the real Node serializer, fixed, and regression-tested. Mixed API quota/unavailable
  race reproduced and fixed; observed auth/quota failures retain stop/wait semantics.
  Final independent re-review reported no remaining actionable defects.
- Browser checks used an isolated SQLite fixture and dummy model connection, not live
  service data. Inspected actual desktop (1440px) and mobile (390px) renders of job and
  workflow pages; both document widths match the viewport. Specialist cards show independent
  peers above adjudication, with original colors/type/spacing and no misleading peer arrows.
- Exercised pause/resume, token details, keyboard focus, live specialist-to-adjudication
  transition, five-session accounting and retained open details. State changes animate;
  reduced-motion emulation disables animation. Browser console: zero errors/warnings.
- Screenshots and local fixture tooling: `.claude/verification/review-team/` (git-ignored).
- No live model/GitHub calls, quality/cost benchmark, production migration or deployment.
