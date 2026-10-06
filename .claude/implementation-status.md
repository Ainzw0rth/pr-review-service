# Implementation status

## Local release

Implemented and running on this VPS at **127.0.0.1:8040**. Expected public hostname:
**auto-reviewer.ainzw0rth.xyz**. Cloudflare/DNS routing has not been modified.

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

- Full Django suite: **171 tests passed** (before final documentation-only changes).
- Ruff check and formatting check passed.
- Django deployment check passed with production settings; migrations consistent.
- Production image built and running web service is healthy; proxied HTTPS login returns 200.
- Real Chromium checks: login, queue/detail, invitation/replay rejection, logout and admin interruption.
- Mobile document width verified at 390 pixels, without page overflow.
- Real Docker isolation smoke: nonroot execution, writable isolated workspace, readonly root and no Docker socket inside tests.
- Detailed browser evidence: `.claude/verification/dashboard-smoke.md` (ignored local artifacts).

No real GitHub comments, paid provider calls or Google Sheet writes were used in verification.
Mocked external integrations are not evidence of real account/model availability.

## Activation inputs still required

1. Connect the public tunnel hostname to the appropriate application origin/network.
2. Complete private administrator password setup.
3. Configure exact GitHub username/repository and privately supplied authorized credentials.
4. Register and test the signed webhook.
5. Configure actual provider model IDs, API credentials and required tests/checks.
6. Optionally inspect and confirm the real Sheet schema/status vocabulary/task links.
7. Verify GitHub formal-review/reviewer-removal behavior in a dedicated test repository.
8. Enable `LIVE_WRITES=1` and recreate services only after those checks.

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
