# Distributed-worker rollout

- Product revision: `60e790b3f8ad720cbdc3ec354cc1a733d12e7852`; pushed branch `feat/distributed-workers`.
- Release: [v0.1.0](https://github.com/Ainzw0rth/pr-review-service/releases/tag/v0.1.0); amd64 image publication workflow succeeded.
- Production: [Workers](https://auto-reviewer.ainzw0rth.xyz/workers/), owner-only. Contabo coordinator/local worker plus one enabled InstaBox model worker.
- Shared Claude/Codex accounts: independent private host logins; global account concurrency retained.
- InstaBox working state: `/data/workspace/pr-review-service/var/worker`; no coordinator DB/encryption/publishing credentials.
- Test/graph phases default local. Remote test transport validated separately; production image allowlists require explicit host configuration.
- Keepalive: dedicated coordinator service, verified SSH host key, 20-minute interval, encrypted connection config; prior host timer disabled after first integrated success.
- Deployment: drained with zero jobs, additive migration 0028, backup/rollback receipt retained, all services healthy, intake resumed. No review cancelled.

## Verification

- Frozen PostgreSQL suite: **927 passed, zero skips**.
- Incremental audited reconciliation: **6 PostgreSQL tests passed**.
- Final installer: **28 tests passed**.
- Node CLI suite: **28 passed, 3 native/opt-in skipped**.
- Ruff/check-format and migration consistency passed.
- Real nonpublishing InstaBox Codex, Claude, archive/test transport and confirmed cleanup passed. No GitHub publication triggered solely for testing.
- Production UI: 375/768/1440px, keyboard, polling focus, reduced motion, overflow and no JS errors passed; temporary owner session removed.
- Published digests anonymously readable; actual manifest install/pull/configuration smoke passed without starting a test agent.
- Ainzw0rth-config installed on InstaBox; skills/plugins/profile launchers verified. Required sync pushed successfully at `a8faf21`; no Contabo credentials or local config edits copied.

## Idle observation

Observed 61.0 minutes after user closed SSH sessions (2026-10-26T15:19:30.167211+00:00 to 2026-10-26T16:20:29.032950+00:00): 213 coordinator-only observations, maximum heartbeat age 15.08s, one unchanged agent incarnation, zero active-work samples and zero observer errors. Three scheduled 20-minute SSH checks succeeded. No suspension-related interruption observed; provider-internal pause state was unavailable, so this is observed availability, not a permanent-uptime guarantee.

Private detailed evidence: `.claude/verification/distributed-workers/`.
