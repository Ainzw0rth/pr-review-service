# Optional integrations

None is required for basic GitHub reviews. Commands assume the Compose environment from
[installation](installation.md). Configure repository integration fields and encrypted Credentials
in `/admin/`; environment changes require a drained [deployment](deployment.md).

## Google Sheets


Disabled when `sheet_config` is empty. Team creates GitHub issues manually. Share the Sheet with
a Google service account and store its JSON privately in Credentials. Select it as the repository's
Sheet credential. Only designated issue status cells may be written.

Onboarding uses an **explicit admin-confirmed mapping**. Start with only the connection fields:

```json
{"spreadsheet_id": "YOUR_SHEET_ID", "range": "Tasks!A1:D"}
```

Read the actual headers, existing statuses and a status-cell dropdown sample:

```bash
docker compose exec -T web python manage.py inspect_sheet REPOSITORY_ID
# For nonstandard or ambiguous header names:
docker compose exec -T web python manage.py inspect_sheet REPOSITORY_ID --id-column "Task key" --status-column "Workflow"
```

This command uses a read-only Google scope, prints an **unconfirmed** proposal, and never saves
configuration or writes to the Sheet. It does not infer task/PR links. Review the sampled dropdown
and select the actual outcome mappings; every target cell's dropdown is checked again before writes.
The complete confirmed configuration looks like this (replace all examples):

```json
{
  "confirmed": true,
  "spreadsheet_id": "YOUR_SHEET_ID",
  "range": "Tasks!A1:D",
  "headers": ["ID", "Title", "Status", "Target date"],
  "id_column": "ID",
  "status_column": "Status",
  "allowed_statuses": ["Backlog", "In review", "Changes requested", "Ready to merge", "Done"],
  "statuses": {
    "review": "In review",
    "feedback": "Changes requested",
    "ready": "Ready to merge",
    "done": "Done"
  },
  "tasks": {
    "TASK-42": {"issue": 42, "prs": [51, 52]}
  }
}
```

The range begins at A1/A and includes the header. For a timeline with title rows, set
`"header_row": 3` (defaults to 1). Keep the range starting at A1; absolute row numbers are
preserved when locating and updating status cells. Headers on that row must be unique/nonempty.
`allowed_statuses` is the complete, confirmed
explicit dropdown (`ONE_OF_LIST`) vocabulary, including values such as Backlog that the service
never writes. It must contain every mapped status. If omitted, it defaults to the unique mapped
values for backwards compatibility. Multiple nonterminal outcomes may share a real value—for
example, both `review` and `ready` can map to In review. The `done` value must remain distinct so
an incomplete task cannot receive the completed value.

The real header and full dropdown vocabulary must match the confirmed mapping. Existing cells
may contain any allowed value. Row reordering is safe because writes find unique stable task IDs;
ambiguous/missing IDs or schema/status changes pause synchronization.

AI may inspect linked issues and related PRs, but task writes require the explicit confirmed map.
A nonempty required-PR set completes only when every PR is merged. Closed-unmerged/draft/open PRs
cannot complete a task. Humans maintain the required set when PRs are abandoned or replaced.

Status synchronization rechecks the source and review attempt before writing. Google Sheets has
no compare-and-set for a cell: a narrow concurrent-human-edit race remains between final reread and
write. Do not give the service ownership of requirements, target dates or other task columns.

## Final-summary journal

For a compatible journal submission API, store its submission-only token as an encrypted
Credential, then configure:

```dotenv
JOURNAL_ENABLED=1
JOURNAL_ENDPOINT=https://journal.example.com/api/v1/sessions
JOURNAL_CREDENTIAL_ID=123
JOURNAL_REPOSITORIES=your-org/your-repository
```

The ID must identify your saved credential. Only allowlisted repositories are included.
Completed reviews send their final summary, counts, outcome, model and links; unsuccessful
runs send operational status and links. Prompts, source, private reasoning, raw transcripts
and individual findings are excluded. Encrypted outbox entries survive restarts and pending
entries survive job cleanup. Maintenance retries separately; journal failures do not change
GitHub outcomes. The receiver must support stable event IDs for deduplication.

Inspect **Administration → Journal entries**. Acknowledged payloads are cleared locally;
receipts expire after 90 days. Disabling `JOURNAL_ENABLED` stops capture and pending delivery.
To attempt pending delivery manually:

```sh
docker compose exec -T maintenance python manage.py flush_journal
```

## Telegram approval alerts

Optional outbound notifications can reuse an existing Hermes Telegram bot; no Hermes
inference, polling or webhook changes are required. Store this JSON in a Credential:

```json
{"token":"BOT_TOKEN","chat_id":"PRIVATE_CHAT_ID"}
```

Set `TELEGRAM_APPROVAL_ENABLED=1`, `TELEGRAM_APPROVAL_CREDENTIAL_ID` to that credential's ID,
`TELEGRAM_APPROVAL_SINCE` to your explicit timezone-aware activation timestamp, and
`TELEGRAM_APPROVAL_REPOSITORIES` to a comma-separated repository allowlist. Only positive
private-chat IDs are accepted. No historical backfill occurs.

Maintenance sends repository/PR, commit, attempt, timestamp and links after confirmed ready
publication. Alerts distinguish a workflow label from formal approval; no findings or source
are sent. Inspect **Administration → Approval notifications**. Uncertain delivery responses
are not automatically replayed, because Telegram sends have no application idempotency key.
Notification failure never changes review status.

## Structural dependency graph

Optional code-review-graph evidence augments GitHub evidence; it does not replace CI or prove
complete runtime dependency coverage. The isolated parser has no network or credentials.
Graph-selected source is fetched at the reviewed commit. Start with the integration disabled.

```sh
docker build -f graph/Dockerfile -t auto-reviewer-graph:local .
docker volume create auto-reviewer-graph-cache
docker run --rm --network none --user 0:0 --entrypoint chown \
  --mount type=volume,src=auto-reviewer-graph-cache,dst=/graph-cache \
  auto-reviewer-graph:local 10001:10001 /graph-cache
docker image inspect --format '{{.Id}}' auto-reviewer-graph:local
```

Put the immutable image ID in `GRAPH_RUNNER_IMAGE`; set
`GRAPH_CACHE_VOLUME=auto-reviewer-graph-cache`. In **Settings → Edit repository**, enable
**Dependency graph** for each intended repository after the runner is ready. New repositories
start with it disabled. The legacy `GRAPH_REPOSITORIES` allowlist applies only to repositories
without an explicit per-repository override; an empty allowlist disables that legacy default.
Apply using the drained deployment procedure. Prewarm an exact commit, without a review:

```sh
docker compose exec -T worker python manage.py warm_code_graph \
  your-org/your-repository REPLACE_WITH_40_CHARACTER_COMMIT_SHA
```

Prewarming is allowed even with an empty review allowlist. The dashboard reports cache
readiness and commit; ready does not necessarily mean the latest default-branch commit.
Failed or invalid caches rebuild on the next eligible review. Retain the cache volume and
rebuild/pin the runner when changing `graph/entry.py`. `GRAPH_TIMEOUT_SECONDS` defaults to 1200.
