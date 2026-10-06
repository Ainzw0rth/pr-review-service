# Subscription CLI Review Implementation Plan

Approved by Louis: proceed with isolated CLI validation, then queue integration.

Goal: Native Claude Code / Codex subscription-authenticated review backends, retaining the
existing deterministic eligibility, CI, schema validation, publication and cancellation guards.

Architecture: The trusted worker starts a disposable non-root Docker runner with a pinned
image and one private provider-auth volume. Only bounded JSON review context enters stdin.
The runner invokes the official non-interactive CLI with structured output and no model shell,
arbitrary MCP, web or repository-config access. The existing collected commit-pinned sources/docs remain
the evidence boundary. No GitHub key, service secrets, Docker socket or host home enters it.
A fixed read-only evidence MCP tool serves bounded ranges from the collected JSON; no arbitrary paths or network calls. The runner independently checks full evidence coverage before accepting success. This avoids the observed 1,048,576-character Codex turn-input ceiling without truncating evidence.
The controller parses structured results, classifies failures, and stores only allowlisted
progress counters/diagnostics. Review text is validated and remains on GitHub, not the dashboard.

- [x] Verify installed CLI versions and Codex subscription authentication/model support.
- [x] Verify Claude subscription authentication and both requested Opus models (completed after VPS login).
- [x] Build CLI image and authenticated no-publication Codex smoke/full-PR tests.
- [x] Add optional CLI provider kinds, nullable API credential for CLI-only providers and admin validation.
- [x] Add bounded event parsing, schema validation, error classification and cancellable Docker execution.
- [x] Show backend and structured-event progress on model nodes; retain model fallback semantics.
- [x] Test malformed streams, missing auth, quota/refusal, cancellation, cleanup, and guard preservation.
- [x] Deploy, verify actual isolated review without publication, configure only verified connections,
      document runner login/refresh/recovery, commit and push.

Limits: No CLI wall-clock deadline (admin can interrupt). Memory/process/output bounds still apply.
Quota waits stay on selected connection; no silent API billing fallback. Missing subscription login
requires user interaction. Existing paused jobs are never automatically restarted by deployment.

## Verification — 2026-10-06

- 215 Django tests passed; 2 Node evidence tests passed; Ruff, JS syntax and migration drift checks passed.
- Browser fixture verified subscription backend/event/evidence counters on desktop and mobile.
- Codex full PR157 probe supplied 138/138 evidence items and returned a schema-valid complete
  review with 4 findings. No GitHub/Sheets publication.
- Fixed observed missing CA certificates, native input-size limit and evidence-tool permission denial.
- API connections disabled; Sol 6.1 CLI enabled. Opus CLI entries created disabled pending actual
  subscription login and verification. User reported login complete, but root auth status still
  reports false and credential cache is absent; clarification requested.
