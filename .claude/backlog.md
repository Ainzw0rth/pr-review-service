# Review service backlog

Recorded 2026-10-22. Implemented in the clean release worktree. Verified: 757 tests passed, 6 skipped; 155 PostgreSQL tests passed; lint, formatting, and migration checks passed. Pushed and deployed as 384200f on 2026-10-22; production health and migration checks passed.

## GitHub reconciliation reliability and request efficiency

- [x] Coalesce pending webhooks per PR: share timeline/current-state reads while preserving distinct human review cycles, ordering, deduplication, and head/eligibility guards. Events arriving during processing remain pending for the next pass.
- [x] Add shared alternate issue-events history fallback, retaining canonical identities and all publication guards.
- [ ] Verify PR #142 unblocks once GitHub supplies consistent history. Both feeds previously omitted its changes; if both still disagree, retain the pause and backoff rather than invent event order.
- [x] Back off repeated reconciliation failures and expose meaningful, credential-free diagnostics instead of the generic connection error.

## Temporary GitHub server-error recovery

Evidence:

- PR #431, job `95b27447-fd66-4e60-8977-5efe9cff38fc`: HTTP 503 during context collection.
- PR #411, job `1e55a772-681d-43f0-911e-aee6b12db903`: HTTP 502 during context collection.
- Both failed before AI review began, with no scheduled retry. These recorded errors are not rate-limit responses; the precise upstream cause was not retained.

Scope:

- [x] Add bounded retries for transient failures on safe GitHub reads, using increasing delays and jitter; respect existing rate-limit cooldowns.
- [x] After immediate retries are exhausted, schedule a bounded delayed job retry instead of immediately treating the failure as terminal. Revalidate cancellation, current head, and review eligibility before resuming.
- [x] Record sanitized endpoint, status, and GitHub request ID for diagnosis; never credentials or sensitive response bodies.
- [x] Do not blindly retry writes: preserve publication receipts and duplicate-prevention guards.

Verification required: transient recovery, retry exhaustion, cooldown handling, cancellation/supersession during backoff, and no duplicate publication. Keep total retry budgets bounded across request and job retries.
