# Same-host capacity audit

Read-only design audit; implementation unchanged. Scope: `providers.py`, `cli.py`, `quota.py`, `model_probe.py`, `review_orchestration.py`, `graph.py`, `runner.py`, `worker.py`, plus worker entrypoint/activity/flow consumers. No checks run: recommendations require implementation before behavioral tests are meaningful.

## Recommendation

Use one small `core/capacity.py` with nonblocking `fcntl.flock` slot files below shared `DATA_DIR/capacity/`. No resource lease table, heartbeats, TTL stealing, Redis or scheduler. This design is explicitly **one Linux host, one shared local lock-directory inode tree, one Docker daemon**. Every worker, probe and quota caller must participate. Replicas with private DATA_DIRs are not globally coordinated.

| Resource | Scope key | Initial limit | Acquisition boundary |
| --- | --- | --- | --- |
| CLI | Docker daemon identity + actual configured auth-volume name, **not Provider ID or kind** | Exactly 1 | Before volume inspection/create; release after verified container removal |
| API | Provider API family + credential/account capacity group | Configurable positive integer; default 3 | Immediately before HTTP request; release after response/client closure |
| Graph | Deployment-global graph runner | 1 | Before graph create/run; release after container cleanup |
| Tests | Deployment-global test runner | 1 | Before archive extraction/container creation; release after container cleanup |

CLI specialists stay sequential in `run_team`; API trio still uses three futures. Acquire API permits **per request**, never three permits as a batch, never around entire `run_team`. A cap of 1 or 2 should serialize only the excess requests, not deadlock fan-out. Intent and synthesis use the same gate as specialists.

If graph cache supports only a single writer, keep graph limit exactly 1 until that cache's concurrency guarantees are established. Raising a runner CPU limit does not establish cache safety. Separately bounded graph/test pools permit their sum; document host sizing (currently graph 2 CPU/2 GiB, CLI 2 CPU/2 GiB, tests 1 CPU/1 GiB per container).

## Evidence-backed integration hazards

1. **Critical when adding concurrency: global orphan cleanup kills healthy work.** `cli.py:457` and `runner.py:253` select all service-labeled containers and forcibly remove them. `management/commands/review_worker.py:29-33` invokes both at startup under the old single-worker lock. That ownership proof disappears with concurrent workers. Remove bulk cleanup from concurrent startup; migrate to cleanup under the exact acquired resource slot. Existing behavior is safe only under its current single-worker deployment assumption.
2. **High: capacity waiting would become review failure/fallback.** `review_orchestration.py:54` starts serial deadline before `call`; `:79` starts one API tranche deadline before submitting requests; `:120-125` enforces it while requests would queue. Worker classifies `review_timeout` as failed and falls forward (`worker.py:296-330`). A gate inside provider code alone therefore is insufficient.
3. **High: probe and quota paths bypass any worker-only/provider-only gate.** `model_probe.py:27` calls `review_cli` directly. `quota.py:166-184` builds and starts containers directly. `_review_cli_once` must own CLI acquisition; quota must explicitly reuse the identical volume-key resolver/gate. API gate in `providers.review` does not cover these.
4. **High: CLI cleanup currently hides removal failure.** `cli.py:442-446` suppresses `RunnerError`, allowing the owner to disappear while its container still runs and mounts auth writable. `quota.py:182-186` also suppresses cleanup failure. Slot acquisition must clean verified abandoned containers before reuse; Docker inspection/removal uncertainty must fail closed, never start another session against that volume.
5. **Medium: callbacks are deliberately controller-thread-only.** API futures currently receive only `stop.is_set` and queue response/activity (`review_orchestration.py:81-113`). `Progress.cancelled` queries Django (`activity.py:197`); worker diagnostics/activity callbacks also write Django (`worker.py:171-217`). A gate must not call these closures from API threads. Preserve the existing event handoff.
6. **Medium: UI does not currently express capacity.** Phase histories accept only running/done/incomplete (`worker.py:145-168`, `flow.py:46`), and activity codes are allowlisted (`activity.py:131-151`). Emitting arbitrary activity via `on_activity` will fail validation or display a generic event. Add explicit capacity events/state projection; do not repurpose quota, skipped, provider failure or an unvalidated raw string.
7. **Medium: probe budget includes queue time today.** `model_probe.py:21-24` starts its 90-second deadline before CLI invocation. Split drain/cancellation from execution clock so a queued probe does not fail merely because a healthy review holds its login volume.
8. **Medium: quota polling currently blocks intake.** Worker invokes `refresh_due` before claiming work (`review_worker.py:55-67`). An indefinite wait there can prevent all intake behind a long review's CLI permit. Use try-acquire-and-skip for scheduled quota polling; do not write an unavailable snapshot or refresh checked_at when skipped for capacity.

## Minimal interface

```python
@contextmanager
def permit(scope, limit, cancelled, *, on_wait=None):
    # Yield slot identity, not secrets. Nonblocking scan + cancellable 100-250 ms wait.
    ...

def cli_scope(kind):
    # Return validated actual volume and its opaque scope digest.
    ...

def api_scope(provider):
    # Controller resolves credential/account policy before futures start.
    ...
```

Prefer a separate `try_permit` or `wait=False` for quota polling; do not model ordinary contention as `ProviderError`. On cancellation raise existing `ReviewCancelled`. Lock-filesystem/Docker cleanup failure is an operational capacity error, not provider quota/outage. Catch it at job/probe boundary and report administrator attention, without provider fallback or a skipped attempt. Never return a fake empty model result.

`on_wait(waiting: bool)` fires only on state changes, with a final false in `finally` when leaving a wait. Add a phase-aware queued event to orchestration (`capacity`, phase, waiting); controller persists phase status/activity. Pass a dedicated callback rather than overloading provider transcript events. State needs phase/resource class, not auth-volume name, account identifier, filesystem path, token, container output or exception text. Suggested public text: **Waiting for capacity**; keep job/attempt running. Record acquisition/resume to clear stale waiting state. Multiple API phases need independent flags; one phase resuming must not hide another phase's wait. If some specialists run and others wait, display waiting on affected phase, not claim the whole attempt is idle.

API scope cannot be Provider ID: several model rows share `Credential`. Default group can be credential ID when duplicate keys are prohibited operationally. For an actual per-account cap across multiple keys/credential rows, provide an explicit operator-controlled account group mapping (settings keyed by credential ID is sufficient initially) and a per-group limit. Canonicalize provider family; do not group OpenAI and Anthropic accidentally. Duplicate secrets in separate credential records must map to the same group or use a stable keyed secret fingerprint internally; do not claim credential-ID grouping alone enforces an account-wide cap. Avoid raw secret/deterministic public secret hashes in filenames or logs. Resolve Django descriptors/configuration on controller thread before sharing provider with futures (`review_orchestration.py:114-116` already materializes credential).

Validate all caps as bounded positive integers; no implicit zero/unlimited fallback. Every process must use identical scope/cap configuration. Lowering a live slot count ignores holders in retired slots: change limits only after drain/restart, or take a separate configuration barrier before resizing. A documented drain requirement is the minimal robust first deployment.

## Budgets and cancellation

Separate external cancellation from execution-budget checks. Waiting polls cancellation/drain, **not** the current `check(deadline)` wrapper. Active work checks both. Never pause an entire parallel team's shared clock because one peer waits: that would give already-running peers unlimited time.

Recommended interpretation: each model phase gets 1800 seconds of active execution, excluding only that phase's capacity wait. Each CLI role and synthesis already constitute separate serial phases. Give API code/security/tests independent active clocks, not one absolute deadline covering their admission queue. This preserves 30 minutes per phase while permitting cap < 3; total tranche wall time may exceed 30 minutes. Document this intentional change from the current one-deadline-per-API-tranche implementation.

Implement a small invocation budget/state object, created by orchestration and shared with only that phase's thread. Initially unstarted/paused; start after permit acquisition and before transport/container setup. Pause on release if a second recovery pass must reacquire; preserve accumulated active time across the two CLI recovery sessions. Do not reset 1800 seconds for recovery. Cleanup counts as active time; raw output never determines budget state. Container orphan cleanup is capacity housekeeping before execution, so it should not consume a new review's budget.

API controller currently checks one deadline. Replace that check with cancellation plus inspection of each phase's active budget. Threads must also check their own budgets during streaming, even if controller queue is backpressured. Expired budget sets the common stop event; controller joins before returning. Keep auth/quota precedence after joining (`review_orchestration.py:143-161`) intact. Avoid holding budget locks while callbacks/enqueue/blocking I/O execute.

For serial CLI, passing the current `lambda: check(deadline)` into the gate is wrong: wait cancellation itself would raise review_timeout. Supply independent `cancelled` and `budget.check` hooks (or a budget-aware invocation object with separate methods); low-level gate takes external cancellation only. Apply the same separation to the 90-second probe timer. Existing non-team/intent calls currently have no orchestration phase deadline: either preserve that policy explicitly or apply the same active-time budget; do not silently establish a wall-clock queue deadline.

The existing httpx cancellation check runs only after a response chunk arrives (`providers.py:526-528`); blocking reads can last up to the configured read timeout (360 seconds). Capacity queues can be promptly cancellable independently, but do not claim end-to-end sub-second API cancellation. Retain permit until real request/client shutdown, not merely when controller stop is set. Tighten transport cancellation separately if required; releasing early overcounts available capacity while requests are still active.

Graph timeout already starts after spawn (`graph.py:96`); acquire before spawn, preserve that placement. Tests have no dedicated execution timeout (`runner.py:217-222`); admission limits bound concurrency, not runtime. Adding test runtime policy is separate scope. Acquire test permit before extraction if the intended bound includes host-side packing CPU/disk; otherwise explicitly describe the cap as containers only. Graph currently downloads archives before `execute`; a container cap does not bound those in-memory snapshots.

## Lock and container lifecycle

1. Create trusted service-owned `DATA_DIR/capacity` once. Open a separate stable file for each slot/acquisition; `LOCK_EX | LOCK_NB`, independent opens even within one process. Never cache a shared FD across API threads: flock belongs to the open-file description. Set close-on-exec; do not leak permit FD into `docker` or model children. Never truncate/delete/recreate lock files during release or stale-owner cleanup: distinct inodes split the lock domain.
2. Scan a fixed number of slots; if all busy, emit waiting once, poll external cancellation at 100-250 ms, retry. Close every unsuccessful candidate FD. Confirm cancellation again immediately after acquisition and before any resource creation. Kernel releases lock when owner exits; no PID-age/TTL lease stealing. Fair FIFO admission is not guaranteed by flock; acceptable initially, documented throughput ceiling.
3. While holding the acquired slot, inspect only containers bearing exact deployment namespace + opaque resource digest + slot labels. For CLI also verify expected actual `/auth` named-volume mount. Add a fresh opaque generation label per acquisition. Use a deterministic **per-slot container name**, and use returned immutable container IDs for subsequent start/inspect/remove, not names. Atomic Docker name reservation prevents a late create request from a dead process racing a new same-slot create into two simultaneous containers. Handle create-name conflict conservatively; do not force-remove an unverified container.
4. A matching container from an earlier generation is abandoned only because this exact slot lock is acquired and all supported launch paths hold that lock throughout container lifetime. No second healthy conforming owner can hold it. Remove only that positively identified old generation, re-inspect absence, then create the new container under the same slot. Do not scan all labels, inspect PID liveness across namespaces, infer ownership from age, or delete unrelated containers/volumes. Running orphan may be removed after proof; healthy holder cannot be touched because its lock is not acquirable.
5. Hold permit through container create/start/parse/finally removal. Verify cleanup even if Docker client exited; losing an attached Docker client does not prove its container stopped. Graph `docker run --rm` only removes on actual container exit; CLI/test existing labels need slot/generation/namespace additions. Graph needs labels as well. Docker create may succeed but client fail before recording success; cleanup-on-next-acquire must discover that container by scoped labels/name, not solely a local `created` boolean.
6. On removal/inspection uncertainty, do not proceed. Propagate an operational error and leave the tagged object discoverable for the next acquisition to retry cleanup. Releasing flock after error does not authorize reuse: every acquirer performs mandatory reconciliation before starting anything. Successful request results must not publish if associated cleanup has failed; tests already treat cleanup failure as blocking (`runner.py:241-250`). No persistent poison/lease DB needed when the tagged container itself supplies the reconciliation evidence.
7. Keep external auth-volume login/repair utilities coordinated with the same gate or require full worker drain while editing credentials. Validate/inspect named auth volume **before create on quota path too**: review path does so (`cli.py:364`), quota currently does not. Never copy auth volume per worker, invent volume per Provider row, silently create a missing login volume, delete auth volumes, or mount auth into graph/tests. Two CLI kinds configured to the exact same volume must contend on one lock, even if that configuration is otherwise unsupported.

Unknown legacy/unscoped containers cannot be safely declared abandoned under new slot locks: they may be old healthy workers. Deploy once under full old-worker drain, reconcile/remove legacy containers after proving no old worker remains, then enable new workers. Rollback to broad startup cleanup likewise requires full drain. Do not promise safe mixed-version rolling deployment. Shared host control plane is trusted; labels alone are not a security boundary against another Docker administrator. Persist lock directory across process/container restarts; never run temporary-file cleanup over active locks.

## Verification matrix for implementation

Use real temporary lockfiles with multiprocessing, plus real separate thread opens; Docker operations mocked for most tests. One opt-in disposable-Docker integration case verifies lifecycle. Fake monotonic clock for long waits; never wait 30 minutes in tests.

| Test | Required assertion |
| --- | --- |
| Two Provider rows, same CLI volume | Exactly one container/session at once across processes |
| Same volume under different CLI kinds | Still one; scope derived from volume, not kind |
| Different CLI volumes | Independent progress; no accidental deployment-wide CLI serialization |
| Probe + review + quota contention | Probe waits visibly; scheduled quota skips without updating availability; none bypass gate |
| API cap 1/2/3 and two jobs sharing credential/account | Observed in-flight <= cap; three tasks still submitted, no batch-permit deadlock |
| Duplicate credentials / account aliases | Configured aliases share permits; separate accounts do not |
| Capacity cancellation/drain | Prompt cancellation before admission; no container/request, no quota/skipped/fallback; no leaked FD |
| Long serial/API admission wait | >1800 simulated seconds waiting does not time out; phase gets full active budget |
| Running peer plus queued peer | Running peer still times out at own 1800 active seconds; one waiting peer never pauses everyone |
| CLI recovery reacquisition | Both executions share one active budget; intermediate waiting excluded |
| Queued probe | Waiting >90 simulated seconds does not fail; >90 active seconds does |
| Callback affinity/privacy | All Django callbacks execute on controller thread; public event only allowlisted phase/resource state; no secret/path/volume/container output |
| API fatal peer + waiting peer | Quota/auth precedence preserved; waiting thread stops/joins without making a request |
| Owner SIGKILL | Lock becomes acquirable; next holder removes exact abandoned container before replacement |
| Healthy owner / another deployment | Cleanup cannot get healthy slot; cannot remove other scope/generation/deployment containers |
| Late/orphan create race | Fixed slot name prevents overlapping containers; old caller starts/removes only own immutable ID |
| Cleanup/inspect/create timeout | Fail closed, no replacement/no successful publication until reconciliation succeeds |
| Unlinked lock / mixed cap config | Prevent/document unsupported operation; drain prerequisite tested for configuration deployment |
| Graph/test limits | Cross-process count bounded separately; graph timeout begins after admission; cancellation removes own container only |
| UI waiting state | Waiting shown at correct phase; clears on resume/cancel; busy healthy worker heartbeat preserved; no skipped provider |

Existing suites to extend: `core/tests/test_cli.py`, `test_cli_recovery.py`, `test_quota.py`, `test_runner.py`, `test_graph.py`, `test_review_team.py`, `test_worker_team.py`, `test_dashboard.py`, and flow tests. Run relevant selected suites, Django system checks, then existing project-required gates. This audit does not certify current concurrency, safe orphan recovery, or any passing implementation tests.
