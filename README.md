# Auto Reviewer

Self-hosted, label-driven GitHub reviews with an organization-isolated queue dashboard.

**Initial target:** `MarkAny-GaneshaIT/AnyOne-2.0`. **Public hostname:** `auto-reviewer.ainzw0rth.xyz`.

Operator activation steps: [.claude/operator-setup.md](.claude/operator-setup.md).
**Host origin:** `http://127.0.0.1:8040`.

GitHub holds review content. Dashboard members see job metadata only. Humans request runs;
pushes and replies never automatically start a review. One reviewer worker runs globally.

Context follows issues/PRs directly linked from the PR and confirmed task map, plus one hop
from those directly linked issues. Dependency issues are included as evidence but are not
recursively expanded into parent backlogs. Scope/size limits still block incomplete reviews;
recognized context failures show their specific safe cause in the job's operational note.

Read [the team workflow](.claude/team-auto-review-workflow.md) and
[the implementation plan](.claude/implementation-plan.md) before enabling live writes.

## Run locally on this VPS

Python 3.12+, Docker Engine/Compose and the project virtual environment are used. Versions
are pinned in `requirements.lock`; Docker base images are pinned by digest.

```bash
cd /root/projects/pr-review-service
# First install only; .env is created privately and never overwritten:
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/python scripts/init_local.py

docker compose build
docker compose up -d
```

If `.env` already exists, skip `init_local.py`. The web listener binds only to loopback
port **8040**. Startup applies migrations through a one-shot migration service. Events,
worker and hourly cleanup run separately; cancellation processing is not blocked by a model call.

Production HTTPS redirects and secure cookies are enabled. Loopback `/healthz/` is exempt
from redirects. Do not disable production protections merely to use HTTP login.

### Cloudflare Tunnel

For a host-native `cloudflared`, route `auto-reviewer.ainzw0rth.xyz` to
`http://127.0.0.1:8040`. If your tunnel runs in Docker, `localhost` refers to its container;
attach it to the application network and route to `http://web:8040`, or configure an explicit
host gateway. Do not expose port 8040 publicly. Trust forwarded HTTPS headers only behind
this trusted proxy. The underscore spelling is not supported by Django host validation.

`.env` configures `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `PUBLIC_URL` and `TRUST_PROXY`.
The tunnel and DNS record are managed separately; this repository does not modify them.

### First administrator

```bash
docker compose exec web python manage.py bootstrap_admin --setup-link
```

This prints a private, one-use setup URL valid for one hour. Open it after the tunnel is
connected and choose a strong password. Initial username `louis`, email
`louiscaesapdf@gmail.com`; flags can change these. There is no default password. Rerunning
for an unfinished bootstrap invalidates its old link; existing configured admins are not overwritten.

Interactive bootstrap is also available without `--setup-link`. For recovery:

```bash
docker compose exec web python manage.py access_link --user louis --hours 1
```

Treat setup/reset/invite URLs as secrets. Do not paste them into public tickets or commit them.

## Configure connections in `/admin/`

1. Create the `ganeshait` organization.
2. Create credentials (secret field is write-only and encrypted in the database).
3. Create the repository with exact `owner/name`, Louis's exact GitHub username, GitHub
   credential and an independent random webhook secret credential.
4. Configure required GitHub check/status names and/or a pinned local test image/command.
5. Add model providers, exact model IDs and their priority.
6. Optionally configure Google Sheets using the confirmed mapping below.
7. Generate invitations from the Organization admin action. Members receive no platform-admin rights.

### GitHub account

Use an authorized token for Louis's account, restricted to the target repository where possible.
The integration needs repository contents/metadata and checks/statuses read access, and PR/issues
write access for comments, review threads, request-changes and workflow labels. Configure any
organization authorization requirements. Creating the webhook separately requires repository
administration rights. Do not copy credentials out of another agent's private session files.

Create repository labels `on review`, `feedback`, `approved`. Register webhook:

```text
https://auto-reviewer.ainzw0rth.xyz/webhooks/github/<repository-id>/
Content type: application/json
Secret: the matching webhook secret configured in Credentials
Events: Pull requests
```

Ingress verifies raw-body HMAC, repository identity and delivery ID. It stores only control
metadata; raw PR descriptions are not retained in the event inbox. Processing uses GitHub's
canonical issue timeline, not arrival order, to identify a review cycle.

Review content is posted through Louis's account and visibly marked automated. Formal `APPROVE`
is never submitted. Formal request-changes is permitted; GitHub can remove the outstanding
review request, so developers may need to request Louis again before retoggling.

### Models and authentication

Current adapters: **Anthropic Messages API** and **OpenAI Responses API**, with API keys.
Use provider-console model IDs, not display labels. The requested Opus 5.5 → Sol 6.1 → Opus 4.8
preference is not silently translated to invented IDs. Availability and account access need
connection verification with your actual providers before live operation.

**Subscription OAuth is not implemented:** there is no verified third-party OAuth client/refresh
contract configured for this service. Do not paste subscription/CLI session tokens into the API-key
field. Adding a supported OAuth connection requires the provider's supported registration and
refresh flow; until then use API credentials. This limitation is explicit rather than a token-reuse workaround.

Outage errors may fall through the configured priority. Quota errors do not: dashboard shows
Out of quota, retains the selected provider, releases the worker and shows reset/next-check time.
Unknown reset time defaults to a check 30 minutes later. Provider refusals try the next configured model with the unchanged request, once per model
per pass. Exhausting the route pauses for admin inspection. Authentication/invalid-output failures also require attention.

Each model call retains its status, timestamps and allowlisted diagnostics in the Job admin's
**Model attempts** section. The latest response also appears in **Provider diagnostics**
field in platform admin: HTTP status, recognized stop reason/refusal category, bounded token
counts and validated provider request/message IDs when available. Member pages/activity feeds
do not expose this field. Raw response text, explanations, prompts and credentials are never
stored. Each provider attempt clears stale diagnostics before calling; network failures without
a response have no response metadata. Historical attempts cannot be backfilled from discarded
responses; any separately recorded diagnostic replay must be identified as such.

There is no configured monthly spend cap or whole-review timeout. Individual calls have finite
network timeouts; failure traversal is bounded. Interrupting a job cannot undo provider charges
already incurred. An in-flight model call may take up to its network timeout to return.

### Required checks and isolated tests

`required_checks` is a JSON list of exact GitHub check/status names. Every required check must
succeed on the reviewed SHA; pending/missing checks block clearance. Same-SHA CI evidence is
rechecked before clearance. An empty list without configured local tests does not provide enough
test evidence to approve automatically.

Optional local tests use `test_image` (digest-pinned) and `test_command` (argument array), e.g.:

```json
["python", "-m", "unittest", "discover"]
```

Prebuild and pull the image with needed dependencies. The test container has no network,
publishing credentials, Docker socket or host bind mounts. Source is validated and streamed
into bounded tmpfs. Nonroot UID, readonly root filesystem, dropped capabilities, 1 CPU/1 GiB
memory and PID limits apply. Images need `/bin/sh` and `tar`; images declaring volumes are rejected.
Test logs are not retained or displayed; reviews receive pass/fail evidence only.

The **trusted worker** has Docker-daemon access to launch containers; the test containers do not.
Treat operator access to that worker as privileged. No model tool allows arbitrary host commands.
On worker recovery, only test containers carrying this application's ownership label are removed.

## Optional Google Sheet

Disabled when `sheet_config` is empty. Team creates GitHub issues manually. Share the Sheet with
a Google service account and store its JSON privately in Credentials. Select it as the repository's
Sheet credential. Only designated issue status cells may be written.

Onboarding uses an **explicit admin-confirmed mapping**. Start with only the connection fields:

```json
{"spreadsheet_id": "YOUR_SHEET_ID", "range": "Tasks!A1:D"}
```

Read the actual headers, existing statuses and a status-cell dropdown sample:

```bash
python manage.py inspect_sheet REPOSITORY_ID
# For nonstandard or ambiguous header names:
python manage.py inspect_sheet REPOSITORY_ID --id-column "Task key" --status-column "Workflow"
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

## Dashboard and invitations

Each attempt page includes a read-only **Review activity** panel. It polls every three seconds
while visible, showing timestamped real operations: context-file/document counts, CI and local
test checks, provider calls/fallback/quota, validated-response/findings counts, publication and
optional Sheet synchronization. Pause/resume affects only the display, never the worker.

A separate worker-process heartbeat updates every ten seconds during a running attempt,
including while waiting on a blocking API call. It is **not** a model token stream or proof of
remote inference progress. The UI flags heartbeat delays and feed disconnections separately.
No chain-of-thought, prompts, raw model output, source text, credentials or terminal commands
are stored in this feed. Review explanations/findings still appear only on GitHub.

Activity uses the same organization access checks as job details. Up to 300 predefined events
are retained per job (latest 100 displayed); they cascade-delete with 90-day terminal-job cleanup.
Attempts predating this feature have no historical activity log. Current status, provider,
start/finish times, operational note and quota retry time update without a page reload.

Members see only their organizations' PR links, commits, trigger actor, status, timestamps and
sanitized operational messages. Job details link back to GitHub; there is no review-results view.
Queue positions are organization-visible positions, not a leak of another organization's work.

Invite/reset links are single-use, expiring and revocable. Invitations grant dashboard membership,
not GitHub membership. Remove membership in admin to revoke access. Removing someone from GitHub
alone does not revoke their dashboard membership. Login throttling uses account + direct peer;
behind the tunnel the peer bucket may be shared (100 attempts/15 minutes). No email delivery required.

## Operations

```bash
docker compose ps
docker compose logs --tail 100 web events worker
docker compose exec web python manage.py check --deploy
docker compose exec web python manage.py backup
docker compose exec web python manage.py cleanup
```

Backups use SQLite's consistent backup API under `/app/var/backups/`. Securely copy them off-VPS,
with a **separate encrypted backup of `.env`/CREDENTIAL_KEY**. Losing the encryption key makes stored
credentials unreadable. To restore, stop worker/events/web/maintenance, preserve the current volume,
replace only its database with the selected backup, restore matching keys and restart/migrate. Never
copy a live SQLite file without its transaction state; use the backup command.

Hourly maintenance removes terminal jobs/receipts after 90 days and expired operational records.
Active/quota/attention jobs are not silently expired. Worker restarts mark interrupted jobs Needs
attention rather than automatically replaying uncertain publication. Inspect GitHub before retoggling.

## Development verification

```bash
.venv/bin/pip install -r requirements.lock ruff
.venv/bin/python manage.py test --noinput
.venv/bin/ruff check core reviewdesk manage.py scripts
.venv/bin/ruff format --check core reviewdesk manage.py scripts
DJANGO_SECRET_KEY=local-check-only .venv/bin/python manage.py makemigrations --check --dry-run
```

Tests use a temporary database and fake external transports; no production reviews or API charges.
Local browser-smoke artifacts live under ignored `.claude/verification/` and `var/`.

## Before enabling real reviews

`LIVE_WRITES=0` is the shipped default. No real external comments or Sheet writes were used to
validate this release. Set `LIVE_WRITES=1` and recreate services only after:

- GitHub credentials, exact reviewer username and webhook signature delivery are verified.
- Formal review/request-removal behavior is checked on a dedicated test PR.
- Your exact models/authentication and required check configuration are verified.
- Optional Sheet mapping and permissions are confirmed.
- Hostname/tunnel/login and administrator access work.

Use a dedicated test repository first. Admin configuration deliberately starts empty; model IDs,
organization secrets and example task mappings are never invented or pre-enabled.

### Live review workflow

The read-only job page shows connected stage nodes: file and documentation counters, discussions,
CI, optional tests, ordered model attempts, and GitHub publication. Each model call has its own
result and duration. Quota checks remain on the selected provider. Detailed events are collapsed
by default. Historical jobs show only recorded evidence; deployment does not resume them.
