# Concurrent PR reviews — approved design

User approved durable queue + bounded PR workers, and requested the amount in Settings.

- Default **2 concurrent PR reviews**, owner-editable **1–8** in Settings. Changes govern new claims; lowering the limit never cancels active work.
- PostgreSQL production queue; short atomic claims with `SELECT FOR UPDATE SKIP LOCKED`. Keep SQLite available for local tests, but production pool requires PostgreSQL.
- At most one owned generation per PR. Different PRs in the same repository may overlap. Ownership survives cancellation/completion until all cleanup/finalization finishes.
- Independently fenced job claim tokens, heartbeat leases, worker-owned crash recovery and all-worker drain acknowledgment. Recover only proven abandoned work; uncertain external writes require attention, never automatic blind replay.
- Initial deployment uses one bounded thread-pool supervisor, but claims and global cap support more than one supervisor. Thread-local database connections are closed.
- One model session per shared CLI authentication volume, across model rows, reviews, probes and quota refresh. API account concurrency is bounded globally; graph and test runners have separate bounds.
- Waiting for capacity is cancellable, visible in technical progress, and is not quota, fallback, Skipped or analysis-budget time.
- Preserve three specialist roles, sequential CLI / parallel API reviews, adjudication, doubled 30-minute phase budget, generation/head/publication guards, encrypted checkpoints, receipt-aware delivery, privacy and container restrictions.
- Same-host shared data-volume file locks may contain local native-container resource slots. Never unlink held lock files; never bulk-delete healthy containers. Multi-host execution is not part of this release.
- No Redis/Celery/new orchestration framework. Reuse Django/PostgreSQL, standard-library threads/flock and existing job/checkpoint lifecycle.
- Preserve paper-and-ink UI; concise settings card; keyboard/focus/responsive/reduced-motion support using existing tokens and motion.
- Migrate all SQLite records, identifiers, relationships and ciphertext safely; preserve original database and private backup. Verify transfer before cutover, prevent simultaneous old/new writers, start at capacity1 then enable2 after checks.
- Commit, push and deploy already authorized. No artificial production PR review, model request or GitHub publication for verification. Do not interrupt an active review for rollout.
