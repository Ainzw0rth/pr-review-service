# Durable bounded PR pool — read-only design audit

Scope: inspected workflow, worker, progress, worker commands, models, delivery, publication, retry, merge, probe and Docker seams. No implementation changes or runtime checks. Report only.

## Smallest robust architecture

- PostgreSQL is the coordination authority; one Docker worker supervisor, `ThreadPoolExecutor(max_workers=8)`, persistent `ServiceSettings.concurrent_pr_reviews` owner Settings limit default 2, validated integer 1–8. No Compose replica management, Celery or broker.
- Support multiple supervisors without the current singleton worker lock. Each execution holds a shared-volume per-claim `flock` throughout execution and finally cleanup. Acquire before the claim can be recovered; release only after cleanup. Recovery must acquire this lock before reclaiming an expired claim. Use independent open file descriptions and stable lock-file inodes; never unlink/recreate contested lock paths. DB admission enforces the cross-process cap; file locks prove execution quiescence on this host.
- Use the singleton `ServiceSettings` row (limit, draining, drain generation), `WorkerOwner` (UUID incarnation, heartbeat/lease, retired/drain acknowledgment), and `JobClaim` (unique job and PR, random immutable token, owner, heartbeat/expiry). Claim rows represent actual occupied execution slots, not Job lifecycle statuses. Separate claim rows avoid nullable status-dependent uniqueness tricks; delete only after completed cleanup or confirmed owner death and orphan cleanup. Tokens must never be inferred from a newly fetched Job.
- A terminal/cancelled/expired-but-unconfirmed claim still occupies capacity and its PR. Finish, cancel, supersede, retry and terminal status updates do NOT delete it. A replacement generation may queue immediately, but cannot execute until the old generation is fully cleaned up.
- Claim transaction locks the singleton pool row, checks drain and global claim count, selects oldest eligible queued/due-quota job whose PR has no claim, locks its PR/job, checks current generation/head/eligibility, inserts claim and sets running. Claim metadata must be committed before executor submission. Submission failure performs fenced attention transition and release; do not silently lose a claimed item.
- Consistent lock order for lifecycle writers: pool when needed, PR, Job/claim. `reconcile` currently locks only Webhook; explicitly lock the PR before generation/cancellation changes. Retry currently locks Job then PR: change to the same order. With a singleton claim admission mutex, `skip_locked` is optional optimization, not the correctness mechanism.
- Never hold the singleton pool lock during network calls, model work, or cleanup. DB claims remain short. Count actual submitted futures (including finally cleanup) before submitting; do not preclaim a hidden executor backlog. Increasing limit applies next poll; decreasing waits for occupants to finish, without cancellation.
- Each task owns its Django thread-local DB connections, GitHub client and mutable callbacks. `close_old_connections()` at entry and `connections.close_all()` in outer finally. The outer wrapper releases claim only after `run_job` returns/raises, closes task connections, and finishes resource cleanup. If cleanup cannot be confirmed, retain/quarantine claim and expose attention rather than pretending capacity is free.

## Concrete interfaces

Use a small `core/worker_pool.py`, not a generic distributed queue abstraction:

```
Claim(job_id, pr_id, owner_id, token)  # immutable execution identity
register_owner() -> Owner
claim(owner) -> Claim | None
load_claimed_job(claim) -> Job
owned(claim, *, allow_terminal=False) -> bool
heartbeat(claim) -> bool
mutate(claim, callback, *, allow_terminal=False)  # transaction + locks + fence
finish(claim, status, reason='', retry_at=None) -> bool
release(claim) -> bool  # outer wrapper only, after cleanup
recover_dead_owners() -> recovery summary
set_limit(actor, limit); set_drain(bool); pool_status()
```

Prefer passing explicit claim execution context through existing job functions. `owned` tests token, owner incarnation, unexpired lease, PR identity; runnable check additionally tests status/current generation/head/eligibility. Cleanup ownership is distinct from runnable ownership: cancelled/terminal workers must clean up and heartbeat while forbidden from publishing.

Heartbeat must never revive an already expired/revoked claim. Use DB time and token/owner predicates. DB errors fail closed for new side effects. A lost heartbeat marks local execution cancelled; thread cancellation remains cooperative. Lease timeout should exceed heartbeat interval and normal scheduling jitter; make values explicit and testable.

## External side-effect fencing: critical limitation

A token check followed by a GitHub request is not atomic. GitHub does not honor our fencing tokens. A paused process can wake after a lease timeout and send an already-admitted request. Therefore **do not release/reassign expired claims solely because a timestamp elapsed**.

Same-host recovery must establish execution quiescence by acquiring the per-claim execution flock (or confirmed owning process exit), stop/remove only its owner-and-claim-labeled child resources, then mark interrupted work attention and release. A live hung supervisor cannot be safely evicted one Python thread at a time; stop the entire supervisor process, confirm exit, recover, restart. One supervisor remains the default deployment; multiple supervisors use the same DB cap, per-claim locks and registry. Do not claim automatic transparent per-thread failover.

For linearizable cancellation relative to writes, add a per-PR publication gate shared by local cancellation/supersession and write dispatch. The gate must cover final fenced admission plus the bounded remote request and receipt/cursor persistence, NOT all context/network reads. A PostgreSQL PR-row transaction lock can provide this if all lifecycle paths honor it; use bounded HTTP timeouts and avoid blocking heartbeats on that row. Alternatively a same-host per-PR file mutex plus in-process mutex is explicit but requires every participating process to share the volume. Define semantics: an already-admitted remote request can finish before cancellation is acknowledged; cancellation prevents subsequent requests. Remote GitHub human changes and response-loss ambiguity remain unavoidable and use existing fresh-state guards/receipt reconciliation. Never hold the global pool row for this gate.

Durable write intents are important where an API lacks idempotency (merge already has `merge-intent`). After a crash, uncertain delivery is attention, not blind publication replay. Existing comment markers/receipts support explicit owner delivery retry after inspection. Preserve this behavior.

## Exact mutation and side-effect seams

| File / entry | Current gap | Required treatment |
|---|---|---|
| `workflow.py:105 reconcile` | Locks Webhook, not shared PR; cancels status while old code remains alive | Lock PR; publication cancellation gate; leave claim held; generation changes atomic |
| `workflow.py:208 claim` | `running.exists()` then first/update is not a cross-process cap | Replace with global admission transaction and per-PR unique claim |
| `workflow.py:227 finish`, `:237 current` | Status alone; reused job on delivery retry permits stale owner ABA | Token/owner/lease predicates; finish never releases claim |
| `activity.py:127 record`, `:177 _heartbeat`, `:199 cancelled` | Unfenced insert/prune and heartbeat; heartbeat stops at non-running status | Fence job activities/retention; maintain claim heartbeat through cancelled/terminal cleanup; stale owner must not refresh |
| `worker.py:24 run_job` | Provider selection/diagnostics, ModelAttempt creation/updates and phase callbacks all mutate without ownership | Fence every write; guard before starting model/test/CLI actions; late callbacks no-op/abort on lost ownership |
| `worker.py:437 finally` | Final ModelAttempt update and journal enqueue occur after lifecycle terminal status | Cleanup-owner fence, not runnable status; release only outside complete finally |
| `worker.py:461 _deliver_and_finish` | Marks completed before Sheets and merge | Keep claim while optional effects run; completed ownership gate for all writes and warning updates |
| `worker.py:490 recover_interrupted` | Globally changes every running job/attempt | Recover only confirmed-dead owner claims; include terminal/cancelled claims; never touch another live owner |
| `worker.py:507 completed_guard` | Status/generation only | Require still-owned terminal claim before Sheets write |
| `delivery.py:19 save_review`, `:29 update_cursor`, `:78 deliver` | Checkpoint create/read-modify-write, attempt increments, final payload/stage and reconciled Receipt writes unfenced | Transactional owned mutations; prevent stale cursor overwrite; F increment or locked row |
| `delivery.py:144 queue_retry` | Requeues same Job despite possibly live old cleanup | Lock PR/job; reject or queue only while retaining prior claim; new execution gets fresh token; availability UI must agree |
| `publication.py:91 transition` | ExpectedLabel insert/delete surrounds label POST/DELETE; default no-op check | Worker calls require publication gate, preserve expectation behavior and rollback ambiguity |
| `publication.py:144 once` | Comment/reply/related writes and receipt creation have check/request gap | Gate each write and receipt; markers remain logical job identity, not execution token |
| `publication.py:325 publish_threads` | Resolve-thread GraphQL and resolution receipts | Same gate/fence, including recovered-resolution receipts |
| `publication.py:529 _publish_review` | Review POST, ExpectedLabel, ApprovalRecord remember/save, receipt changes | Gate all dispatches; preserve approval-intent/revocation handling |
| `publication.py:631 terminal_transition` | Custom terminal guard bypasses `current`; cursor changes after each write | Add ownership check directly; gate label requests, not just generic guard |
| `merging.py:20 merge_approved` | Completed job check, warning writes, merge intent and PUT bypass `current` | Terminal-owner gate; keep existing pinned SHA and no-replay merge intent |
| `retries.py:33 retry_job` | Job→PR lock order; new generation can overlap live cleanup | Canonical lock order, preserve old claim, fresh token only when actually claimed |
| `admin.py:127 cancellation`, `events.py lifecycle updates` | Alternate status writers can bypass lifecycle serialization | Route cancellation through same PR gate; preserve immediate cancellation intent while waiting for admitted write |
| `journal.py:queue_job`, `approvals.py` | Journal/approval writes occur outside generic guard | Fence job-originated intents; independent approval revocation/sweeper is a different authority, must remain able to repair stale approval |
| worker commands | Startup globally kills runner containers/reclassifies probes; filesystem drain assumes one synchronous job | Owner-scoped cleanup and DB drain/claims; futures and probes included in acknowledgment |

Provider quota persistence is shared cross-job state: inspect `record_exhausted`/refresh updates for lost-update races. Thread pool PR cap does not cap internal specialist fan-out or provider requests; make this visible and preserve any existing provider limits.

## Recovery and drain

- Startup registers a unique owner incarnation; every supervisor participates in worker registry and drain acknowledgment. Dead-owner reconciliation never blanket-updates statuses. Legacy running rows without a claim at migration become attention (or completed only for confirmed `stage=done`) with interrupted ModelAttempts.
- Dead claim with checkpoint `done`: preserve/confirm completed; record optional integration uncertainty. Other interrupted running work: attention, preserve encrypted checkpoint and receipts. Cancelled/superseded/terminal status remains terminal; finish resource recovery and release claim. No automatic model rerun or uncertain publication replay.
- Keep queued work queued; due quota is the existing authorized retry, not crash recovery.
- Drain is DB persisted and serialized against claim by pool row. Return acknowledgment only when no claims/futures remain, all cleanup completed, and probes stopped/finished. Count claims, not status=`running`. Require acknowledgment for the current drain generation from all live registered supervisors; never reuse stale token/ack after restart/resume. Preserve `worker_token` compatibility and add a supervisor list in status output. Display active vs configured limit and attention/recovery state.
- Probes need explicit bounded execution and ownership too. Smallest approach: one tracked supervisor probe task only while no review futures, blocking further claims until probe finishes. Drain waits for it; startup must not blanket-fail another live owner’s probe. Quota refresh must not block heartbeat/drain indefinitely.

## File set / deployment

- Models + migration; new `core/worker_pool.py`; workflow, worker, activity, delivery, publication, retries, merging, relevant approvals/journal/admission callers.
- `core/management/commands/review_worker.py`, `worker_control.py`, model_probe; runner/CLI launch labels and owner-scoped orphan cleanup.
- Owner Settings views/forms/templates/routes and existing UI tokens/motion/accessibility; reuse owner authorization, CSRF, strict bound validation. Do not add separate configuration surface.
- PostgreSQL settings/dependency, Compose db healthcheck and persistent DB volume, migrate dependency, env example and backup/restore/deploy scripts/docs. Keep existing shared data volume for encryption-related files, locks and runner resources. Stop SQLite writers and migrate with counts/constraints/sequence checks; preserve original backup and rollback path. No database cutover is performed by this audit.

## Required meaningful checks

Run transactional concurrency tests on real PostgreSQL with independent connections/processes, not SQLite or one TestCase transaction:

1. Racing claimers cannot exceed cap, claim same job, or run different generations of one PR. Different PRs execute simultaneously.
2. Cancel/status-terminal during blocked model finally, Sheets or merge does not free slot/PR. Replacement waits through actual cleanup.
3. Delivery retry reuses Job but fresh token; prior callbacks cannot mutate attempt/activity/checkpoint/receipt/finish or dispatch publication.
4. Resize 2→1 preserves occupants; claims resume only below cap; 1→3 admits additional jobs; invalid 0/9/bool/noninteger and nonowner requests rejected.
5. Drain racing claim: no admission after drain transaction; ack only after futures/probe/finally; repeated drain/resume invalidates stale acknowledgment.
6. Alive owner with expired lease is not reassigned; old heartbeat cannot resurrect it; dead owner recovery leaves other owners untouched and cleans only tagged resources.
7. Crash before/after remote write, receipt, checkpoint done, terminal finish and journal cleanup: fail closed; explicit delivery retry uses existing markers/receipts, merge never blindly replays.
8. Cancellation racing final publication admission, terminal labels, completed Sheets and merge: serialized local semantics; lost ownership prevents later effects.
9. PostgreSQL `select_for_update` with nullable `provider` joins: use `of=("self",)` or separate row acquisition; PostgreSQL rejects locking nullable outer-join side.
10. Provider/team callbacks in concurrent jobs have isolated clients/state; DB connections close on every exit; executor submission failure and cleanup exception do not strand invisible capacity.

No checks executed: design audit only. The essential acceptance criterion is execution ownership through cleanup, not the count of jobs whose UI status happens to be running.
