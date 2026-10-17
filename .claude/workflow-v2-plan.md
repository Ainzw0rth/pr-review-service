# Workflow v2 implementation plan

Approved scope: structured, separately executed intent synthesis; PR summary,
inline findings, discussion replies, related-PR comments, related-issue comments,
and workflow-label nodes. No additional validation, approval, merge or resolution
nodes. Existing diagrams retain their saved stage membership.

## Constraints and interfaces

- No new dependencies, direct model GitHub access, or changes to eligibility guards.
- New jobs snapshot revision `v2`. Legacy/v1/v1.1 jobs retain old review behavior.
- Model transport accepts `task="intent"` or `task="review_v2"`; default `review`
  preserves existing adapters and historical result schemas.
- Intent uses only fetched PR/issue/discussion/task evidence, with source IDs;
  distinguish stated requirements from inferred intent and explicit unknowns.
- V2 review includes the original context plus `intent`; summary cannot replace evidence.
- Every open thread is assessed. Replies add `action` (`reply`/`not_needed`),
  `reason`, and `blocking`; skipped replies cannot resolve a thread.
- `related_comments` may address supplied related PRs or issues only.
- `action_plan` contains `inline_findings`, `discussion_replies`,
  `related_pr_comments`, `related_issue_comments`; each is
  `{decision: publish|not_needed, reason: nonempty string}`. Decisions must agree
  with validated proposed writes. Summary and labels remain required delivery.
- Persist text/action plans only in encrypted checkpoints. Dashboard receives
  allowlisted events/counts, never model justifications, source text or intent prose.
- Progress codes: `intent_started`, `intent_ready`; delivery categories use
  `summary`, `inline`, `discussion`, `related_pr`, `related_issue`, `labels`, with
  suffixes `_started`, `_done`, `_unused`. Keep all codes <=30 characters.
- Dashboard step IDs: `intent`, `pr_summary`, `inline_findings`,
  `discussion_replies`, `related_pr_comments`, `related_issue_comments`, `labels`.
- Record completed writes from receipts; retries must not duplicate publication
  or regress a confirmed node. A label failure must retain completed earlier nodes.

## Tasks and checks

- [x] Model contracts + execution: failing tests for grounded intent, rejected
  unknown references and inconsistent action plans; generalize existing API/CLI
  transport to select fixed schemas/prompts/validators; run intent then review
  inside each provider attempt, preserving fallback/quota/cancellation and usage.
- [x] Publication: tests for optional assessed replies, issue target validation,
  progress and receipt-aware retries; instrument existing publication without
  weakening guards or changing v1 saved payload semantics.
- [x] Projection/UI: tests for v2-only membership, explicit not-needed states,
  completed/failure/resume projection; update schema snapshot, safe activity
  messages, lanes and labels.
- [x] Integration: worker tests for two real phases, fallback, invalid output,
  failure before publication, privacy and delivery-only reuse; preserve legacy tests.
- [x] Verification: focused tests first, full Django suite, Node tests, Ruff,
  formatting, migration drift check, diff check, independent review; document
  actual results and deployment requirements. Do not deploy or alter live DB.

## Decisions

Ruling: retain existing `ModelAttempt` rows, tagging diagnostic phase and aggregating
provider-reported usage across intent/review sessions. No extra model-history table.
Ruling: keep approved prior work in the shared checkout; do not move or overwrite
the existing uncommitted versioning and AXI interface changes.


## Verification results

- Full Django suite: 548 tests passed (43.334 seconds).
- Node v24.21.0: 16 CLI tests passed.
- Ruff check: passed repository-wide; formatting check passed on all 20 changed Python files.
- JavaScript dashboard syntax and git diff whitespace checks: passed.
- Django migration drift: no changes detected; 0018 remains the required schema migration.
- Independent integration and publication/UI reviews: no important findings.
- No live model calls, live database migration, commits, or deployment performed.

## Resolved verification findings

- Phase transition initially inherited intent token counters on a review error with no
  diagnostics. Reset current-phase diagnostics before each call; regression test passed.
- Historical CI test fixtures supplied a single legacy result. Updated to grounded
  intent plus v2 output so tests exercise CI gating through both phases.
- Retry test's fake GitHub omitted captured PR metadata and GraphQL thread reads.
  Aligned external fixtures with its evidence; actual freshness checks remain intact.
- New source-array uniqueness/minimum constraints are validated locally, omitted from
  portable wire schemas. Regression checks cover duplicate sources and API payloads.
