# Team workflow: requesting and completing automated PR reviews

**Status:** agreed workflow implemented locally; real repository/model/Sheet activation is still pending. See ../README.md and implementation-status.md for setup and verified scope.
**Last updated:** 2026-10-06.
**Initial GitHub repository:** `MarkAny-GaneshaIT/AnyOne-2.0`. Requested reviewer: `Ainzw0rth`.
Dashboard organization: `ganeshait` (independent of the GitHub organization name).

**Live progress:** open an attempt from the queue to see its Review activity panel. This is a
read-only operations log, not a shell or model reasoning transcript. It refreshes every three
seconds; pause/resume freezes only your display. A separate worker heartbeat indicates process
responsiveness while a provider call is pending, not model token progress. Findings remain on
GitHub. Delayed heartbeats and lost dashboard connections are explicitly indicated; neither
should be mistaken for a completed review. Older attempts may have no recorded activity.
**Configured reviewer:** Louis's GitHub account; exact username must be configured before launch.

This guide describes how developers and reviewers should use the service after its connections and live writes are enabled. GitHub is the home for findings, questions, replies and formal reviews. The dashboard displays review-job progress only. The Google Sheet is the task/requirements source of truth; the service may update only its configured issue-status field.

## 1. Quick start for developers

1. Prepare the PR with its purpose, changes, tests, linked GitHub issue and stable Sheet task ID. Link other PRs required for the same task.
2. Request Louis as a GitHub reviewer.
3. Set the PR label to `on review`, removing conflicting workflow labels.
4. Check the dashboard for queue progress. Read all review findings on GitHub.
5. Address findings in code or reply with clarification/evidence in the relevant GitHub threads.
6. When ready for another review, move to `feedback`, ensure Louis is still requested as reviewer, then set `on review` again.
7. Wait for the current commit to pass review and receive `approved`. A human then performs the formal approval/merge decision under repository rules.

The order of steps 2 and 3 does not matter: either human action can make the PR eligible. Both conditions must hold. The service never adds `on review` to initiate a run.

**Replies and new commits do not, by themselves, request another AI review.** After answering a clarification question, retoggle the labels even when no code changed. The bot should include this reminder in clarification requests.

## 2. Eligibility and scope

A review is eligible only when all of these hold:

- The repository is explicitly allowlisted.
- The PR is open.
- Louis is currently requested as reviewer.
- The PR has `on review`.
- The current human-triggered review attempt has not been cancelled or superseded.

If the reviewer request or `on review` is removed, queued/running work is cancelled. The service does not review PRs merely because Louis is mentioned in a comment or assigned as an issue owner.

This is a strict, continuing eligibility rule—not just a check when tracking begins. Posting a formal request-changes review through Louis's account may affect requested-reviewer state; exact GitHub behavior must be verified before launch. Developers must re-request Louis whenever needed. A label retoggle alone is insufficient if the reviewer request is absent.

### Related-PR exception

The service may read related PRs and post concise dependency/conflict comments on them within the allowlisted repository, even when those PRs do not request Louis. This does not enroll those PRs into automated review, change their labels or submit formal review decisions on them.

No automatic expansion to other repositories. Additional repositories require explicit configuration and authorization.

## 3. Labels and their meaning

These labels summarize the shared review workflow, not GitHub's formal review state. They are mutually exclusive; the service normalizes conflicting labels.

| Label | Meaning | Who sets it |
| --- | --- | --- |
| `on review` | Human requests an automated review; job may be queued, running or waiting on quota. | Human only |
| `feedback` | Changes, clarification, a fresh review request or recovery from failure are needed. Read GitHub for details. | Human or service |
| `approved` | Current commit passed the configured AI checks, or an authorized human explicitly overrode the label state. | Human or service |

The service can set `approved` only after a complete review of the current commit with no unresolved blocking findings. Nonblocking suggestions may remain. Required tests must pass; missing required evidence, ambiguous requirements or incomplete analysis cannot result in AI approval. If current-commit CI is still running, the worker waits before starting the model (admin cancellation remains available). Missing checks with no active CI or failed required checks remain blocking gates. Required CI is rechecked before publication; the dashboard shows the latest observed result. An earlier pending result cannot override a later success, and CI success cannot override failed local tests or actual code findings.

`approved` is not a formal GitHub approval, does not bypass branch protection and never authorizes automatic merging. Humans decide whether to merge only once this label is present; normal repository requirements still apply.

Any human with appropriate repository permissions may approve or change workflow labels. This is not restricted to Louis. The service does not grant new permissions.

## 4. Initial review, step by step

### Developer preparation

Include enough information to establish intended behavior:

- Stable task ID and the relevant GitHub issue/Sheet reference.
- What the PR changes and why.
- Acceptance criteria, deliberately excluded edge cases and explicit agreements.
- Relevant `docs/` references.
- Tests performed and any known limitations.
- Related backend/frontend/dependency PRs and which are required to complete the task.

AI may infer candidate task/issue links, but an explicit identifier/link or human confirmation is required before writing Sheet status. Ambiguous matches are raised on GitHub rather than silently chosen.

### Human trigger

Request Louis and add `on review`. The service records a distinct attempt and the PR's current head commit. Duplicate delivery of the same event must not create duplicate jobs. A deliberate retoggle creates a new attempt, even on the same commit.

### Queue and execution

One review job executes at a time, globally. Eligible jobs are processed oldest first. A cancelled-and-retoggled job rejoins at the back rather than retaining its former place.

The worker reviews the captured commit. Tests run in an isolated, disposable environment without publishing credentials. Serial execution does not replace execution isolation.

### Publication

Immediately before publication, recheck the PR is open, current commit is unchanged, Louis remains requested, `on review` remains present and this attempt is still current. Stale/cancelled attempts must not publish findings or apply status changes.

The service posts concise, actionable findings on GitHub, including inline review threads where appropriate. Every completed review also posts a concise summary naming the reviewed commit, coverage, test results and outcome—even when no defects are found.

Service-generated comments must be clearly identified as automated because they are published through Louis's account. The service never submits formal `APPROVE`. Formal `REQUEST_CHANGES`, comments, replies and permitted label operations may be automated.

## 5. What the AI reviews

- Correctness and concrete bugs in changed behavior.
- Intended edge cases, respecting explicitly agreed exclusions.
- Relevant security defects and material test gaps.
- Linked issue requirements and the confirmed task context from the Sheet.
- Relevant documentation under `docs/`, including code/documentation contradictions and contradictions among relevant documents.
- Relevant dependencies and compatibility with related PRs.
- Every unresolved review thread on the current PR, plus relevant threads on related PRs.

This is not a full unrelated-documentation audit or a style-comment generator. Follow references as needed, but keep analysis tied to the change and task.

Findings should identify the affected location, concrete consequence and supporting evidence. Use inline threads for location-specific defects. Keep summaries concise. Do not repeat unchanged findings or reply to every thread merely to acknowledge it.

If requirements or documents conflict, flag the contradiction and ask developers to settle on one agreement. Do not invent precedence. Human replies become context for the next explicitly requested run.

## 6. Feedback and re-review loop

1. Read the GitHub summary and affected threads.
2. Implement fixes or explain why the reported behavior is intentional, with evidence or an updated agreement.
3. Update relevant documentation when needed.
4. Move to `feedback` to cancel any active attempt.
5. Ensure Louis is requested as reviewer; re-request him if necessary.
6. Set `on review` to request a fresh run on the latest commit.

Follow-up reviews check prior findings and new changes. Moving a line or resolving a thread does not prove a defect is fixed. On each explicitly triggered review, the service assesses every open review thread on the current PR, including human-authored threads. When the current commit demonstrably addresses every concern, it posts a concise explanation citing the reviewed commit and resolves the thread. If the fix is incomplete or evidence is uncertain, it leaves the thread open and replies with the remaining gap or a focused question. Optional suggestions can stay open without blocking clearance.

Thread resolution is not formal PR approval. Outdated lines, passing CI alone and a developer saying “fixed” are not proof. New or edited comments, a changed commit, or a cancelled review stop stale resolution. A human reopening a thread during publication is not overridden. Already-resolved threads are left alone. Related-PR threads are read for context but never automatically resolved by this PR's review. If GitHub denies resolution permission, the explanation asks a human to resolve it manually. Ordinary PR comments have no resolvable state.

After pushing fixes, re-request the designated reviewer if necessary and toggle `feedback` → `on review`. A push alone does not start a new review or close discussions.

Comments alone do not start work. The service does not continuously participate in discussion between runs. It replies during explicitly triggered reviews when there is new evidence or an unanswered point.

### Example: fixes while an old review exists

```text
Commit A + reviewer requested + on review → attempt 1 queued/running
Developer pushes commit B               → attempt 1 superseded; no stale publication
Developer sets feedback                 → old attempt cancelled if still active
Developer ensures reviewer requested
Developer sets on review                → attempt 2 reviews commit B
Attempt 2 completes                     → GitHub findings/summary; feedback or approved
```

If a new commit arrives without a retoggle, the service does not review it automatically. The dashboard explains that the previous attempt was superseded and a new review must be requested. If `approved` was present, remove it and set `feedback` so old clearance is not mistaken for coverage of new code.

## 7. Cancellation and human decisions

- Removing `on review` or Louis's reviewer request cancels queued/running work.
- Setting `feedback` or manually setting `approved` moves away from `on review` and cancels the active attempt.
- Closing or merging the PR cancels remaining work.
- Admin interruption is available independently of labels.
- A formal human GitHub approval alone does **not** cancel the AI job or automatically set `approved`; it remains independent of the label workflow. Continuing eligibility still applies.

Cancellation stops local processing where possible and prevents subsequent publication. A provider request already in flight may still finish or incur charges. Comments already published remain on GitHub.

The implementation must distinguish its own expected label transitions from human changes to prevent webhook loops. Multi-step GitHub publication is not assumed atomic: each external write needs current-attempt checks and idempotency so cancellation/retries do not duplicate or overwrite results.

## 8. Waiting, failures and provider routing

### Quota exhaustion

- Dashboard displays **Out of quota**.
- Keep the job waiting on its current provider; do not rotate merely because quota is exhausted.
- Show the provider's reset time when available. Otherwise show the next availability check, initially 30 minutes later; this is not a promise of recovery.
- Release the worker so other eligible jobs can run.
- Before retrying, recheck the reviewer request, label, current commit and attempt state.

### Availability failures

Use bounded retries and authorized fallback providers for outages/availability failures. No overall review deadline or configured spending cap is imposed, but individual network calls have finite timeouts and retries are bounded. If no provider can serve the review, fail rather than looping forever.

User-requested preference order is **Opus 5.5 → Sol 6.1 → Opus 4.8**. These are preference labels, not verified API model identifiers. Actual availability, provider IDs and supported API/OAuth authentication must be validated before deployment. Admin configures credentials privately; no credentials belong in this guide, GitHub comments or logs.

### Policy/security refusal

Try the next configured model with the unchanged review request: Opus 5.5 → Sol 6.1 → Opus 4.8. Each model gets one call per pass; never rewrite or disguise the request. The dashboard records each decline and fallback separately. If all remaining models decline or are unavailable, pause as **Needs attention** without publishing. Authentication and invalid-output errors also pause. Quota exhaustion waits on the same provider. Ordinary comments do not resume paused work; a fresh human label request is the retry route.

### Technical failure or missing required checks

Never mark the PR approved. Dashboard shows failure and GitHub receives a concise operational explanation, distinguishing infrastructure failure from a code defect. Move the PR to `feedback` when still authorized/current; the developer retries with the normal human trigger.

Required tests that fail or cannot run block approval. Optional unavailable checks are disclosed in the summary but need not block. The exact required check set is configured per repository before launch.

## 9. Google Sheet, GitHub issues and multiple PRs

The team manually creates GitHub issues from the Sheet. The service does not create issues or infer human work hours. Optional timeline/Sheet integration is inactive unless supplied and configured.

During setup, the service reads the Sheet and proposes a mapping of its real status values. Admin confirms that mapping before writes are enabled. Changes to relevant columns or allowed values require reconfirmation. Do not hard-code assumed status names such as "Done" if the Sheet uses different terminology.

Only the configured issue-status field may change automatically. Requirements, priority, deadlines and other Sheet fields remain untouched. Exact task matching uses stable IDs, never row position or fuzzy title matching alone.

### Completion rule

- A task's explicitly linked required PR set determines completion.
- All required PRs merged → apply the configured completed status.
- Any required PR open, draft or closed without merge → do not mark complete.
- No confirmed required PRs → do not automatically mark complete.
- A human explicitly removes/replaces abandoned or superseded required PRs.

The service must continue tracking merge/close lifecycle events for task status even after a PR's review finishes; this is not a new AI review trigger.

### Backend/frontend example

A backend PR can receive `approved` while the frontend PR remains unfinished. Post concise dependency comments on the relevant backend/frontend PRs explaining that the overall issue is not complete until all required PRs merge. Avoid repeating the same reminder without new information.

If related work later introduces a compatibility conflict, comment on the affected PRs and request a human retoggle. Cross-PR analysis does not remove another PR's `approved` label, automatically request a review or edit its other workflow state.

## 10. Queue dashboard and access

The dashboard is available at a public hostname through an admin-configured Cloudflare Tunnel, but its contents require login. The tunnel does not replace application authentication.

Organization members see only their organization's operational job information:

- Repository and PR link.
- Attempt identifier and reviewed commit.
- Triggering actor, queue position where applicable and status.
- Queued/started/completed timestamps.
- Next quota retry/check time.
- Sanitized failure, cancellation or pause reason.

Expected statuses include queued, reviewing, out of quota, needs attention, completed, failed, cancelled and superseded. Completed means the job finished—not necessarily that the PR passed. GitHub remains the place to read the review outcome and findings.

No findings, diffs, discussion system, raw model transcripts or credentials are displayed. No member retry/cancel/review controls. Historical jobs remain visible for 90 days; temporary checkouts and raw model context are deleted after each run. Retained operational logs must be sanitized.

### Membership and administration

- Platform admin creates organizations, repository mappings and invitations.
- Initial dashboard organization is `ganeshait`.
- Invitations are single-use, expiring, revocable and organization-scoped.
- Invitation grants dashboard access only; GitHub membership is not separately required or granted.
- No public self-registration.
- Invitees choose their own credentials; platform admin is bootstrapped privately, not with a password embedded in a document.
- Admin can revoke membership, generate one-time password-reset links and interrupt jobs.
- No outbound email integration initially; admin recovery uses a VPS-local command.
- Removing someone from GitHub does not automatically revoke dashboard access; admin must revoke it separately.

## 11. Admin setup checklist before inviting the team

- [x] Verify `MarkAny-GaneshaIT/AnyOne-2.0` and configure requested reviewer `Ainzw0rth`.
- [ ] Configure authentication through Louis's GitHub account with the required, scoped permissions.
- [ ] Verify actual GitHub behavior for comment-only and request-changes reviews, including requested-reviewer changes and follow-up re-requests.
- [ ] Configure label names and mutual-exclusion behavior.
- [ ] Configure provider IDs, permitted authentication and availability fallback order; validate quota/refusal classification.
- [ ] Define repository-required tests and disposable execution environment.
- [ ] Configure GitHub event delivery, authentication/verification and duplicate-event handling.
- [ ] Verify stale-commit/cancellation guards and recovery after partial publication.
- [ ] If enabled, connect the Sheet, identify stable task IDs, confirm required-PR linking and approve its status mapping.
- [ ] Configure authenticated dashboard hostname, organization isolation and 90-day retention.
- [ ] Bootstrap admin privately and test invite/reset/revocation flows.
- [ ] Exercise initial review, same-commit retoggle, new-commit supersession, quota wait, refusal fallback and exhausted-route pause, technical failure and multi-PR completion before rollout.

This checklist identifies implementation/verification work still required. It is not a claim that any of these integrations, accounts or services already exist.

## Owner-only retry button

Louis may use **Retry review** on the job page without retoggling labels when the same commit
is still eligible (`on review`, requested reviewer, same human review cycle). This creates a
new, audited attempt from the first configured model; other members and admins remain read-only.
Published/running/completed attempts are excluded. Changed commits or review cycles still require
the normal GitHub request flow. Insufficient API credits display **Out of quota** and wait on
the selected provider, including Anthropic's HTTP 400 credit-balance response.
