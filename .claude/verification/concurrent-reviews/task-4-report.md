# Task 4 — owner Settings capacity

Commit: this report ships in the Task 4 implementation commit; resolve with `git log -1 --format=%H -- core/tests/test_concurrency_settings.py`.

## Behavior

- One Service capacity card: **Concurrent PR reviews**, persisted number, default 2, bounds 1–8. Existing theme, native 44px control, 160/260ms transitions and reduced-motion behavior reused; no CSS/dependencies added.
- Owner-only GET/POST, CSRF retained. Strict single ASCII integer 1–8; missing field preserves existing capacity. Invalid field returns 400 with linked inline error/aria-invalid and preserves entered value.
- Global ServiceSettings lock acquired before Repository locks in one atomic transaction, matching admission order. Capacity and every repository policy validated before writes. Only capacity field updated, preserving draining and drain_generation.
- Capacity included in form revision: concurrency-only changes invalidate stale forms (409).
- Real claim integration test raises live cap to admit another job, lowers without cancelling three existing executions, and verifies a completed job retains its slot until cleanup releases its claim.
- Existing related-item owner test used explicit pk1 then implicit other user: PostgreSQL reproduced duplicate auth_user primary key. Minimal fixture fix: explicit pk2.

## Red → green

After fixing initial test-fixture issues (singleton already seeded by migration, imported TestCase discovery), seven new tests produced 15 expected failing assertions against old code: ignored capacity, accepted invalid inputs, missing field, stale revision and live claim admission. Red output remains local ignored `task-4-red.log`. Implementation then passes.

## Actual checks

Working directory: repository worktree. `PY=/root/projects/pr-review-service/.venv/bin/python`; all DATA_DIR paths below isolated inside `.claude/verification/concurrent-reviews/`. Created ignored `staticfiles/` directory before runs; no global warning suppression.

- `DATA_DIR=$PWD/.claude/verification/concurrent-reviews/task-4-data DEBUG=1 $PY manage.py test core.tests.test_concurrency_settings core.tests.test_auto_approval.ApprovalSettingsTests core.tests.test_auto_merge.MergeSettingsTests core.tests.test_related_settings --noinput`: **37 passed**, latest 1.166s. Includes seven new tests plus existing Settings and module-imported workflow coverage.
- PostgreSQL equivalent: `DATA_DIR=... DEBUG=1 PYTHONPATH=$PWD/.claude/verification/concurrent-reviews:$PWD $PY - test` running:

```python
import os
os.environ['DJANGO_SETTINGS_MODULE'] = 'postgres_settings'
import django
django.setup()
from django.db import connections
connections['default'].settings_dict['TEST']['NAME'] = 'test_reviewdesk_task4'
from django.core.management import call_command
call_command('test', 'core.tests.test_concurrency_settings',
             'core.tests.test_auto_approval.ApprovalSettingsTests',
             'core.tests.test_auto_merge.MergeSettingsTests',
             'core.tests.test_related_settings', interactive=False)
```

  **37 passed**, 10.904s; isolated database destroyed. First valid PG run exposed pre-existing fixture collision above; rerun green after minimal fix. Private credentials read only by existing ignored postgres_settings.py; never printed.
- `ruff check core/settings_views.py core/tests/test_concurrency_settings.py core/tests/test_related_settings.py`: clean. `ruff format --check` same files: 3 already formatted. Browser setup helper also Ruff checked/formatted.
- `DATA_DIR=... DEBUG=1 DJANGO_SECRET_KEY=isolated-check-only $PY manage.py check`: no issues.
- Same env `manage.py makemigrations --check --dry-run`: no changes.
- `NODE_PATH=/root/.npm/_npx/31e32ef8478fbf80/node_modules node --test core/tests/activity_ui.test.cjs`: **1 passed**. Four technical progress disclosures, specialist/adjudication attribution and delivery statuses retained. Two generated Task2 screenshots restored to HEAD after rerun.

## Real browser

Fixtures: `task-4-browser-setup.py`; automation: `task-4-browser.cjs`. Isolated SQLite, fake repository/unused credential, Django runserver bound only 127.0.0.1:8054; no Docker/model/GitHub requests. No packages installed.

Setup/run env: `DATA_DIR=$PWD/.claude/verification/concurrent-reviews/task-4-browser-data DEBUG=1 DJANGO_SECRET_KEY=isolated-browser-only DASHBOARD_RETRY_USER_ID=1 PYTHONPATH=$PWD`. Run `$PY task-4-browser-setup.py` (full artifact path), then `$PY manage.py runserver 127.0.0.1:8054 --noreload`. Browser command: `NODE_PATH=/root/.npm/_npx/31e32ef8478fbf80/node_modules node .claude/verification/concurrent-reviews/task-4-browser.cjs`.

Passed: desktop 1280×900/mobile390×844, semantic number label/min/max/step, initial2/current persisted value, keyboard Tab/Shift+Tab visible focus, min44px field, 160ms transition, native overflow validity, desktop save3 + success, server-invalid9→400 with input retained/linked error, stale-tab→409, no lost saved value, keyboard advanced disclosure, mobile ArrowUp→4/save + success, reduced-motion transition0s, no horizontal overflow/page errors. Full-page screenshots in `task-4-screens/`: desktop-focus, desktop-success, desktop-invalid, desktop-stale, desktop, mobile, mobile-success, mobile-reduced-motion; metadata results.json. Inspected actual desktop-focus and mobile renders: matching palette/type/spacing; clean wrapping and focus; no clipping.

Session cookie/fixture DB remain ignored, not committed. Local server stopped after verification.

## Self-review and limits

Traced Settings GET/POST → owner/CSRF → singleton/repository locks → validation → atomic writes → revision/redirect/messages → subsequent claim admission/retained cleanup. Reviewed failure paths, backwards omission and drain-state preservation. No production commands, deployment, push, external publication, new dependencies or full-suite claim. No live worker/model/production-capacity exercise; controller owns full SQLite/PostgreSQL suites, independent final review, integration/deployment. Browser run Chromium only; no screen-reader audit or other-engine check.
