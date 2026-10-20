# Parallel CLI specialist reviews — approved implementation plan

Goal: run existing code, security and test reviewers concurrently without credential races; preserve synthesis, validation and publication guards. No additional reviewer roles in this release.

Architecture: retain the existing controller-thread callback queue and three-worker pool. Carry execution ContextVars into worker threads. Apply one account-wide CLI slot pool across PRs, models and quota probes. Default to one slot until an auth-coordinated runner image is selected; operators can enable three. Codex uses private CODEX_HOME and a loopback refresh bridge: a stable flock protects reading shared auth, deduplicating rotated refresh requests, native OAuth exchange and atomic persistence; inference never holds the auth lock. Claude retains its pinned native cross-process OAuth lock. Refresh tokens stay inside trusted runner containers, never model evidence, arguments or logs.

## Constraints

- No new dependencies, API billing fallback, model inference for testing, or live GitHub writes.
- Preserve 30-minute active phase budgets, cancellation, exact-slot cleanup, claims, evidence checks and failure precedence.
- Auth mismatch/revocation fails closed; transient refresh failures remain retryable provider errors. Never overwrite newer shared credentials with a delayed per-session write.
- Validate runner image capability before enabling concurrent account sessions. Existing one-slot deployments remain compatible.
- Keep approved artifacts under .claude; temporary verification output under .claude/verification.

## Execution

1. [x] Baseline: 38 team/capacity tests and two real-wrapper tests passed. Inspect pinned Codex Rust auth and Claude embedded refresh code.
2. [x] Add runnable Codex refresh bridge tests: simultaneous processes reuse one rotated token; isolated snapshots; account-switch refusal; upstream failure; malformed/oversized input; lock release on process death. Implement cli/auth.mjs and integrate entry/quota wrappers. Use node:test and real flock with synthetic credentials/local HTTP upstream only.
3. [x] Add failing fan-out tests for both CLI kinds, callback thread affinity, claim ContextVar propagation, quota/auth precedence and cancellation cleanup. Reuse existing queue path; update serial-only assertions. Add global CLI slot setting, image capability validation and quota sharing. Test actual admission across PR-equivalent calls.
4. [x] Update runtime progress accounting and docs. Run focused Django tests, full Django suite, Node tests, Ruff and container build/synthetic wrapper checks. Review whole affected flow before integration.
5. [x] Record verification and rollout instructions. No production model probes or review replay.

## Verification and rollout record

- Full Django suite: 681 tests in 63.487s, OK, six PostgreSQL/optional skips. No fresh PostgreSQL run for this change.
- Focused orchestration/worker tests: 37 pass; capacity/CLI/quota tests: 49 pass.
- Node auth tests include real flock contention, atomic rotation/isolation, account-switch rejection, malformed/oversized input, transient failures and process-death lock release.
- Actual pinned Codex 0.160.0 integration: two native app-server sessions share one synthetic OAuth rotation and both persist privately; stale private writes leave authoritative auth unchanged. Docker network disabled, no real credentials/inference. Offline workspace routing discovery is an expected post-refresh error and is checked explicitly.
- Ruff check passes repository-wide; changed Python files pass formatting; four unrelated pre-existing formatting failures remain in capacity-audit.md, concurrent-reviews-plan.md, flush_journal.py and transcript.py.
- Independent full-flow review: no actionable findings. Reviewed credential snapshot/refresh, slots, claims, cancellation/join, fallback, cleanup and publication boundaries.
- Runner image builds with auth-coordination v1 capability. CLI versions remain pinned unchanged.
- No rendered UI/browser checks: no templates, styles or controls changed; progress projection/worker integration exercised by tests.
- No live OAuth/provider/GitHub writes, production settings changes, database changes, retries or deployment performed.

Production currently uses the older serial SQLite app and has not rolled out the separately approved concurrent-PR/PostgreSQL subsystem already present in the source base. Do not deploy the whole source tree as a small hotfix: follow the existing stopped-writer PostgreSQL cutover, or separately backport this feature to the deployed serial release. Deploy the coordinated runner digest together with app support, then explicitly set CLI_ACCOUNT_CONCURRENCY=3 under a full drain. Keep default1 until that coordinated rollout. Adding new specialist roles remains a later change.
