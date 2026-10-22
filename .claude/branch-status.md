# Branch layout

Updated 2026-10-22 after consolidation.

- `main`: canonical base for future work and deployment. Contains production release `384200f` (GitHub recovery and per-PR reconciliation). Subsequent branch-cleanup documentation does not change deployed application code.
- `wip/ui-and-specialist-settings`: preserved unfinished work at `c6c953e`, based on `32fdbbe`. Separate checkout: `.worktrees/ui-and-specialist-settings`. Pushed for safekeeping, not approved for deployment.

Before continuing the WIP, integrate current main carefully. Its earlier phase-checkpoint implementation overlaps released code, and its migration numbering/dependencies conflict with released migrations. Do not merge/deploy it unchanged. Preserve the latest GitHub reliability fixes during integration, then run relevant checks.

For future changes: branch from main, verify and review, merge into main, then deploy main. Retire completed branches after confirming unique work is preserved.

All seven retired branch tips were ancestors of production release `384200f`. Six retired worktrees were clean; their complete files (including ignored local files) were archived and compared before removal. Private local recovery archives and manifest: `var/branch-cleanup-20261022/`. Production data volumes and running containers were not changed.

Settings/repository management and generic public setup were subsequently integrated separately into main. The WIP branch remains a historical snapshot for unfinished Workflow/custom-specialist changes; do not merge it wholesale or reintroduce its old Settings/checkpoint implementation.
