# Auto Reviewer — implementation plan

**Status:** approved by user; local implementation and deployment completed, external activation checks pending. See implementation-status.md.
**Date:** 2026-10-06.
**Specification:** [Team auto-review workflow](team-auto-review-workflow.md).
**Project root:** `/root/projects/pr-review-service`.

## 1. Objective

Build a self-hosted, organization-scoped PR review service with a read-only queue dashboard. Human GitHub actions initiate reviews. GitHub holds all review findings and discussion. The service runs one review at a time on this VPS and optionally synchronizes issue status to a Google Sheet.

This plan implements the previously agreed workflow. It does not reopen settled behavior or authorize changes to unrelated VPS services.

## 2. Current state and pause

Historical planning checkpoint: implementation was paused when the user requested this plan. The user subsequently approved the plan and implementation resumed. The following inventory records that earlier checkpoint; current status is in implementation-status.md.

Existing uncommitted artifacts:

- Approved team workflow and this replacement implementation plan.
- New local Git repository on branch `implementation`; no remote, commits or pushes.
- Initial Django settings, data models, entry point, requirements and ignore file.
- Initial dashboard/auth test cases; dashboard implementation remains incomplete.
- Project-local Python virtual environment and installed dependencies.
- System `python3.12-venv` package installed to support the local environment.

No database migration, application listener, public domain route, webhook registration, real account, external review or Sheet write has been created by this implementation. Existing draft code is unverified, not an accepted milestone. Review it against this plan before retaining it. Do not delete it without a reason; do not continue implementing before approval.

## 3. Hosting and port

| Item | Plan |
| --- | --- |
| Requested hostname | `auto_reviewer.ainzw0rth.xyz` |
| Proposed compatible hostname | `auto-reviewer.ainzw0rth.xyz` — requires user confirmation |
| Application origin | `http://127.0.0.1:8040` |
| Port | `8040`, observed free during planning; recheck at launch |
| Public ingress | User-managed Cloudflare Tunnel |
| Dashboard | `/` with authentication |
| Administration | `/admin/`, platform administrators only |
| GitHub ingress | `/webhooks/github/<repository-id>/`, signature authenticated, not browser-login authenticated |
| Health | Minimal `/healthz/` endpoint, no sensitive configuration |

Local check: Django's host parser rejects the requested underscore hostname and accepts the proposed hyphen hostname. Do not weaken host-header validation to accommodate it. Confirm the hostname before public deployment and update allowed hosts, CSRF origins, invitation base URL and webhook URL consistently.

If `cloudflared` runs on the host, its origin can be `http://127.0.0.1:8040`. If it runs in a container, its own `localhost` is not the VPS: use a deliberately configured shared container network/service name or host gateway. Inspect the actual tunnel topology when connecting; do not modify the existing tunnel during initial implementation.

## 4. Architecture

```text
GitHub events ──> signature-verified webhook ──> durable event inbox
                                                    │
                                             event reconciler
                                                    │
                                               SQLite queue
                                                    │
                                        one active review worker
                                         │        │          │
                                     GitHub     model      isolated
                                     context   provider     tests
                                         │        │          │
                                         └────validated result┘
                                                    │
                                     revalidation + publication journal
                                                    │
                                          GitHub comments / labels
                                                    │
                                     optional Sheet status reconciliation

Authenticated members ──> organization-filtered job dashboard
Platform admin ─────────> configuration / invitations / interruption
```

### Technology choices

- Python and Django: native password hashing, sessions, CSRF protection, forms, migrations and admin configuration.
- Server-rendered HTML/CSS with small progressive-enhancement JavaScript; no SPA or frontend build pipeline needed.
- SQLite on local persistent storage for this single-VPS, single-worker scope; short transactions, explicit concurrency controls and backups.
- Separate web, event-reconciliation and review-worker processes. Long model calls must not block cancellation events.
- Single-worker OS lock on this VPS; atomic job claims and durable attempt state. Do not run multiple hosts against this SQLite database.
- Docker-based disposable test execution through a constrained runner. Never expose a Docker socket or publishing credentials to PR code.
- Production web server, loopback-only published port, secure cookies behind the trusted TLS proxy.

### Module boundaries

- `core/models.py`: organizations, membership, connection metadata, PR snapshots, attempts, event inbox, publication receipts and task mappings.
- `core/workflow.py`: eligibility, attempts, cancellation, label transitions and queue ordering.
- `core/webhooks.py`: signature validation, input limits and durable deduplicated ingestion.
- `core/github.py`: paginated GitHub reads and narrowly scoped writes.
- `core/context.py`: commit-pinned context selection, task links, docs and discussion history.
- `core/providers.py`: provider requests, structured result validation and failure classification.
- `core/runner.py`: disposable test execution and cancellation.
- `core/worker.py`: serial orchestration, retries and guarded publication.
- `core/sheets.py`: confirmed Sheet mapping, stable task identity and status-only writes.
- `core/secrets.py`: encrypted credentials, never returned through ordinary forms or responses.
- `core/views.py`, `forms.py`, `admin.py`, templates/static: accounts, administration and read-only dashboard.
- Management commands: private bootstrap, invite/reset, event processing, worker, cleanup and diagnostics.

## 5. Binding workflow rules

1. Repository allowlisted, PR open, Louis currently requested, and `on review` present are all required.
2. A human adding the label or requesting Louis may make the PR eligible. The service never initiates a run by adding `on review`.
3. Every deliberate entry into eligibility creates one attempt pinned to the current head commit. Redelivery is not a new attempt; an intentional same-commit retoggle is.
4. New commits supersede old attempts but do not start new ones. Clear stale approval to `feedback` where appropriate.
5. Removing either eligibility condition cancels work. A human label override wins over a running job.
6. Human formal GitHub approval is independent of label state and does not by itself cancel the AI job.
7. Formal bot `APPROVE` is forbidden. Automated `REQUEST_CHANGES`, comments and allowed label changes use Louis's account and identify themselves as automated.
8. Replies alone do not trigger review. Clarification messages remind developers to retoggle and re-request Louis when necessary.
9. All result publication and status writes require fresh eligibility/current-attempt checks, except independently authorized task-merge reconciliation.
10. Cross-PR effects are relevant comments only within allowed repositories; no secondary label changes or review enrollment.
11. Required checks failing or unavailable block approval; zero findings alone is insufficient.
12. Every completed review posts a concise GitHub summary. Dashboard never stores/displays the review text as product content.

## 6. Implementation milestones and acceptance gates

### Milestone 1 — project foundation and access

Deliver:

- Reconcile draft scaffold with this plan; pin dependency versions and generate migrations.
- Organizations, explicit membership, repository configuration and encrypted credential storage.
- Private administrator bootstrap; no hard-coded initial password.
- Login/logout, password validation, request throttling and organization-scoped authorization.
- Single-use expiring/revocable invitations; username/password registration without public signup.
- Admin-issued one-time reset links and VPS-local administrator recovery.
- Revocation immediately prevents access to organization resources.

Verification:

- Tests for expired/replayed invitations, reset reuse, CSRF, password/session invalidation and cross-organization access.
- Members cannot use admin actions, cancel jobs or modify settings.
- Secret values are absent from ordinary responses, logs and re-rendered configuration forms.

### Milestone 2 — event ingestion and durable review lifecycle

Deliver:

- Verify GitHub webhook HMAC against raw request bytes; reject oversized, malformed and mismatched-repository events.
- Persist delivery IDs and event-processing state before acknowledging acceptance.
- Process label/reviewer/synchronize/close/reopen events, distinguishing triggers from cancellations.
- Reconcile event intent with current GitHub state; stale or out-of-order events cannot resurrect cancelled attempts.
- Transactional attempt generation, FIFO claiming, cancellation and supersession.
- Distinguish service-generated label events from manual changes, including same-account operations.
- Recover interrupted processes without silently publishing twice or leaving the queue permanently occupied.

Verification:

- Both trigger orders; removal of either condition; same-commit retoggle; duplicate and reordered events.
- New commit during queued/running/quota states; human overrides; closed/merged PRs.
- No concurrent reviews; cancellation remains processable while a provider call is in flight.
- Service label updates do not generate self-triggering loops.

### Milestone 3 — GitHub context and review analysis

Deliver:

- Read commit-pinned diff, relevant complete files, surrounding code, issue context and relevant `docs/` references.
- Gather unresolved current-PR threads and relevant related-PR context with pagination.
- Keep PR content as untrusted data; it cannot redefine permissions, tools or publishing policy.
- Stable issue/task links are explicit or human-confirmed before external task writes; inference can suggest candidates.
- Incremental context retrieval for large changes; omitted/truncated critical context prevents clearance rather than producing a false pass.
- Per-repository required checks, optional test command and review instructions.
- Structured output with evidence, location, severity, blocking disposition and thread actions, validated before use.

Verification:

- Context uses the captured SHA, not a changing branch tip.
- Prompt-injection content cannot authorize cross-repository writes or secret access.
- Invalid paths, off-diff inline locations, unknown thread IDs and unrelated PR destinations are rejected.
- Conflicting requirements yield clarification and no approval.

### Milestone 4 — provider connections and failure handling

Deliver:

- Admin-managed provider priority and verified exact model identifiers.
- Initial preference retained as Opus 5.5 → Sol 6.1 → Opus 4.8; never silently substitute a model when those identifiers are unavailable.
- API-key support and only provider-supported OAuth flows whose authorization/refresh requirements can be verified. Do not scrape or copy personal CLI session credentials.
- Quota exhaustion pauses on the current provider, shows reset/next-check time and releases the worker; default unknown-reset check interval 30 minutes.
- Availability errors use finite retries and authorized fallback providers.
- Policy/security refusals pause as Needs attention; no refusal-driven rotation or prompt disguise.
- Invalid authentication requests reconnection. No total-job timeout or configured spend cap, but finite individual network timeouts.

Verification:

- Simulated quota, retry headers, outage, expired authentication, refusal and malformed model output.
- Quota does not switch models; refusal does not fall through to a backup.
- Same provider is retried after quota wait only if the attempt remains eligible/current.

### Milestone 5 — isolated tests and safe GitHub publication

Deliver:

- Admin-selected test image and command; checkout only the captured commit into disposable storage.
- No host/project secrets, Docker socket or write credentials inside test containers; resource/network constraints and cancellation cleanup.
- Required checks validated for the current commit; optional missing checks named in GitHub summary.
- Publication journal and stable markers for inline findings, replies, cross-PR comments and summary.
- Detect earlier publication after uncertain HTTP outcomes before retrying.
- Fresh checks before every external write; avoid claiming impossible atomicity across GitHub endpoints.
- Publish formal request-changes only when appropriate, with reviewer-removal side effects handled explicitly.
- Preserve human threads and avoid repeating unchanged findings. Do not automatically resolve human discussions.

Verification:

- Cancellation/new commit between model completion and publication yields no stale result.
- Partial publication/restart does not duplicate comments or incorrectly set approved.
- Validate actual GitHub request-changes/comment-only behavior in a test repository before production activation.
- Test execution cannot reach publishing credentials or arbitrary host paths.

### Milestone 6 — queue dashboard and administration

Deliver:

- Responsive operational dashboard: current work, waiting queue, recent history, filtering and pagination.
- Job detail: repository/PR link, actor, commit, timestamps, status and sanitized operational reason only.
- Visible quota retry/check time and distinction between Completed and code approval.
- Organization-scoped read-only member views; admin interrupt and configuration screens.
- States: queued, reviewing, out of quota, needs attention, completed, failed, cancelled and superseded.
- No review findings, code diffs, chat UI or model transcripts on the dashboard.
- Accessible labels, keyboard focus, readable contrast, mobile layout and empty/loading/error states.

Verification:

- Cross-tenant requests return no private metadata, including direct job URLs.
- Browser checks for login, invitation, queue, job details, admin interruption and narrow screens.
- No mutation endpoints available to ordinary members.

### Milestone 7 — optional Sheet issue-status synchronization

Deliver:

- Disabled unless a Sheet connection and mapping are configured.
- Read existing columns/statuses; propose mapping and require explicit admin confirmation.
- Stable task IDs; protect against row movement and schema/status vocabulary changes.
- Task-to-required-PR mapping includes human-confirmed associations and human removal of abandoned/superseded PRs.
- Update only the configured issue-status field, never requirements, deadlines, priority or hours.
- Complete only when the nonempty explicit required-PR set is fully merged.
- Merge/close reconciliation operates independently of whether review eligibility remains present.
- Recheck current Sheet/task state before writes; pause on ambiguity rather than overwrite conflicting human edits.

Verification:

- Backend approved while frontend open does not complete the task.
- Closed-unmerged required PR blocks completion; zero confirmed links never completes.
- Row reordering still targets the stable ID; renamed columns pause writes.
- No writes to non-status fields and no fuzzy-match-only writes.

### Milestone 8 — VPS packaging, recovery and release validation

Deliver:

- Production image/Compose and documented local commands; isolate from existing VPS stack.
- Persistent database storage and separately protected credential encryption key.
- Bootstrapping, connection setup, invitation, recovery, backup/restore and 90-day cleanup runbook.
- Cleanup of temporary code/model context; sanitized operational history retained for 90 days.
- Loopback binding on port 8040; trusted-proxy configuration only for the actual tunnel setup.
- Update the team guide to reflect implemented, verified behavior rather than target-only claims.

Verification:

- Unit/integration suite, lint, Django system/deployment checks and migration consistency.
- Container build, local health/login smoke and authenticated browser flow.
- Restart during review; database backup/restore; retention without deleting active-job safety state.
- No credentials in tracked files or generated user-facing output.

## 7. External setup inputs and activation gates

Development and mocked integration verification do not need real organization credentials. Live activation does.

Before live activation collect through private setup, not chat:

- Confirmed public hostname and actual tunnel connectivity.
- Exact GitHub repository identifier and Louis's GitHub username.
- Scoped GitHub user authorization and webhook secret, with organization policy requirements satisfied.
- Provider credentials, supported authentication route and exact model IDs.
- Required test/check configuration for AnyOne-2.0.
- Optional Sheet identifier, authenticated access, task-ID/status columns, confirmed status mapping and required-PR associations.

Safe release order:

1. Complete local implementation and automated checks.
2. Bootstrap administrator privately and configure connections.
3. Exercise real GitHub semantics in a dedicated test repository with representative PRs.
4. Confirm hostname; user configures tunnel to the reachable app origin.
5. Verify authenticated dashboard and signature-protected webhook publicly.
6. Enable AnyOne-2.0 live writes only after the integration checks succeed.
7. Invite team members and share the team workflow.

No public demo credentials, default weak password, real review comments or organization-wide installation are needed to demonstrate local progress.

## 8. Decisions not silently expanded

- Dashboard is operational and read-only for members, not a review viewer or alternate discussion platform.
- Personal GitHub account is intentional; no mandatory GitHub App substitution.
- No formal AI approval or automatic merge.
- No autonomous issue creation, deadline edits or work-hour estimation.
- No automatic review on push or discussion replies.
- No quota-based provider rotation and no policy-refusal evasion.
- No enforced total review deadline or monthly spending limit.
- No full unrelated repository/documentation sweep.
- No speculative distributed queue, Kubernetes, SPA or plugin framework.

## 9. Completion criteria

Implementation is ready for team use only when the agreed lifecycle passes automated tests, real GitHub integration semantics are verified, cancellation/stale-result guards work, all member data is organization-scoped, and the public authenticated deployment is reachable through the confirmed hostname. Missing credentials or unresolved provider availability must be reported explicitly, never disguised as a working integration.

**Approval received:** user approved this plan, including `auto-reviewer.ainzw0rth.xyz`. Live integration activation remains subject to the credential/configuration and test-repository checks above.
