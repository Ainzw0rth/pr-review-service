# Implementation status

## Local release

Implemented and running on this VPS at **127.0.0.1:8040**. Expected public hostname:
**auto-reviewer.ainzw0rth.xyz**. Public health/login now return HTTP 200. User changed the
Cloudflare origin to `http://auto-reviewer-web-1:8040`; the VPS-local Compose override connects
the web service to the tunnel's existing Docker network.

- Django accounts, administrator configuration and organization-isolated queue dashboard.
- Private admin bootstrap, invitation/reset/revocation flows; read-only member access.
- Signed GitHub ingress, durable inbox and canonical-timeline review-cycle reconciliation.
- Serial worker, cancellation/supersession, quota waits, refusal pauses and availability fallback.
- Commit-pinned context, relevant docs, linked issues/PRs, thread replies and guarded publication.
- Required-check validation and disposable, restricted Docker test execution.
- Optional confirmed Sheet mapping, read-only mapping inspection and status-only updates.
- Persistent SQLite, pinned dependencies/images, production Compose, backup and hourly retention cleanup.

Initial dashboard organization `ganeshait` exists. Administrator username `louis`, email
`louiscaesapdf@gmail.com`, exists with an unusable password until private setup completes.
A one-hour initial setup URL is stored in the ignored, mode-0600 `var/admin-setup.txt` file.
Regenerate with `docker compose exec web python manage.py bootstrap_admin --setup-link`
if unused and expired. Once configured, use `access_link --user louis` for recovery.

## Verification

- Full Django suite: **186 tests passed** (including protocol diagnostics and member-access isolation).
- Ruff check and formatting check passed.
- Django deployment check passed with production settings; migrations consistent.
- Production image built and running web service is healthy; proxied HTTPS login returns 200.
- Real Chromium checks: login, queue/detail, invitation/replay rejection, logout and admin interruption.
- Mobile document width verified at 390 pixels, without page overflow.
- Real Docker isolation smoke: nonroot execution, writable isolated workspace, readonly root and no Docker socket inside tests.
- Detailed browser evidence: `.claude/verification/dashboard-smoke.md` (ignored local artifacts).

Initial local verification used no real GitHub comments, paid provider calls or Google Sheet
writes. Later operator-authorized debugging included a real provider replay (below).
Mocked external integrations are not evidence of real account/model availability.

## Provider refusal diagnostics

- Original refused-response payloads were discarded and cannot be reconstructed.
- One explicitly diagnostic replay used the same configured Opus 5.5 model and normal review
  request flow, without fallback, publication or GitHub label changes.
- Replay returned HTTP 200, `stop_reason=refusal`, `stop_details.category=cyber`.
  This confirms a provider refusal, not transport, credential, context-limit or parsing failure
  for the replay; it does not identify an exact triggering source line.
- Replay metadata saved on the original Job, explicitly marked `source=diagnostic_replay` and
  `original_response_retained=false`; request/message IDs remain admin-only, not in this repo.
- Future provider attempts retain allowlisted protocol diagnostics in platform admin. Tests
  cover callback delivery, worker persistence, no fallback/publication on refusal, suppression
  of arbitrary text/headers and exclusion from member pages/activity feeds.

Read-only replay against the real PR confirmed a recursive issue-link expansion bug. Scope now
includes direct references plus one issue-reference hop, not a recursive repository backlog.
The previously failing PR's context now completes: 24 changed files, 2 issues, 1 related PR,
27 relevant documents and 76 source entries. No provider calls or publication in this replay.
Recognized context failures now retain a safe specific operational reason and activity event.
Failure cleanup now reuses the guarded terminal label transition, avoiding the duplicate
`on review` + `feedback` labels exposed by this real-world context failure. Regression verified.

## Live activity visibility

- Attempt-detail terminal-style activity panel polls every three seconds; pause/resume and
  hidden-tab suspension affect only the display.
- Actual context/CI/test/provider/publication stages, counts and timestamps; no private model
  reasoning, prompts, source content, raw API responses or credentials.
- Worker-process heartbeat every ten seconds, including blocking provider waits; explicitly
  not presented as remote model token progress. Stale feed/heartbeat states visible separately.
- Organization-scoped GET-only activity endpoint; last 100 events displayed, 300 retained per
  job; cascade retention with the parent job.
- Chromium verified pause, newly recorded event catch-up after resume, live status rendering,
  and mobile 390px document width. Screenshots in ignored `.claude/verification/activity-*.png`.

## External activation progress

- Configured `MarkAny-GaneshaIT/AnyOne-2.0`, requested reviewer `Ainzw0rth`, using the user's
  expressly authorized GitHub CLI credential (encrypted application storage).
- Verified live GitHub identity and reads; fixed double decompression of real gzip responses.
- Created the three workflow labels. Signed webhook registered; GitHub ping delivery HTTP 200.
- Required check configured as `required`, the existing CI aggregate job. The sampled PR's
  pending/missing aggregate correctly blocked clearance. No branch protection changed;
  GitHub's protection API reported a plan restriction.
- Read supplied Sheet; confirmed New Timeline tab/status mapping with user. Added explicit
  header-row support with absolute-cell regression tests. Private proposal saved under `var/`.
- Sheet writes remain disabled: service-account credential and confirmed task/PR sets missing.
- Fresh unused administrator setup link stored privately. No password configured yet.

## Activation inputs still required

1. Complete private administrator password setup.
2. Configure and verify actual provider model IDs and API credentials.
3. Optionally provide the Google service-account credential and confirm task/required-PR links;
   recheck the Sheet schema/dropdown through the authenticated API.
4. Verify GitHub formal-review/reviewer-removal behavior in a dedicated test repository.
5. Enable `LIVE_WRITES=1` and recreate services only after rollout checks.

Detailed steps: [operator setup](operator-setup.md).

**Provider subscription OAuth remains unavailable:** no supported OAuth client/refresh contract
has been configured/verified for this service. API adapters are implemented; personal session-token
reuse is not. The requested model display names have not been silently replaced with guessed IDs.

## Deliberate limitations

- Single VPS and single global review worker; SQLite is not a multi-host queue.
- Context limits fail closed; oversized/incomplete changes need narrower review scope.
- Human task/required-PR confirmation is required before Sheet writes.
- Sheet mapping inspection suggests an unconfirmed configuration; admin confirms actual values.
- GitHub and Sheets offer no transaction spanning remote reads and writes. Guards/rechecks minimize
  the residual race window but cannot eliminate it; partial published comments remain visible.
- Policy refusals never trigger bypass rotation. Quota waits keep their selected provider.
- No overall job deadline or spend cap; finite network operations and bounded provider traversal.
- Test-output transcripts are discarded; only pass/fail operational evidence is kept.
