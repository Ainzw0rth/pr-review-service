# Configuration and first review

Complete the [guided quickstart](quickstart.md) or [manual installation](installation.md) first. Commands assume its `COMPOSE_FILE` export.

## Repository setup in Settings

Sign in as the configured owner, open **Settings**, and choose **Add repository**.
Supply the workspace, exact `owner/repository`, reviewer GitHub username, authorized GitHub
token (or select an existing saved connection). Use **Edit** for connection changes and **Pause**
to disable review processing without deleting history. Secret fields are write-only; do not
expect saved tokens to appear in forms. Follow the form's connection feedback before continuing.

The token must belong to the configured reviewer account. Grant repository contents/metadata,
checks and commit-status read access, pull-request/issues write access for reviews, comments,
thread resolution and labels, and **Webhooks: read and write** for automatic webhook setup.
Organization authorization may also be required. Use a dedicated authorized
account when the reviewer must approve PRs authored by your usual account.

Saving attempts to configure labels and the webhook when a public HTTPS ingress origin is
available. These explicit setup writes are separate from `LIVE_WRITES`, which gates review
publication. Local-only setup saves the repository and explains that forwarding is still needed.
Use **Verify connection** to retry setup after correcting configuration. A configured webhook
is not confirmed delivery: inspect a real delivery in GitHub.

The service generates an independent encrypted webhook secret; it is not a form input and is
not displayed. Automatic setup registers the repository's URL with JSON content and **Pull
requests** events. The URL has this shape:

```text
https://YOUR_PUBLIC_INGRESS/webhooks/github/REPOSITORY_ID/
```

A local dashboard URL is not a public webhook URL. See [forwarding](installation.md#4-make-webhook-ingress-reachable).
Webhook signatures, repository identity and delivery IDs are checked. Successful delivery does
not itself guarantee review eligibility: the processor reconciles current GitHub state and timeline.

Advanced repository fields remain available in `/admin/`, including optional Sheet mappings,
exact required-check names, test containers and instructions. Organization invitations are
created through the Organization admin action. Members receive dashboard access, not platform
administrator privileges.

## Model provider: choose API or CLI

The setup wizard creates your first provider automatically. **Wizard users can skip the manual
provider creation below.** It is for manual installs and later provider changes.

Manual provider creation and credentials live in `/admin/`. **Workflow** controls the owner's model
routing; **Settings** does not create provider accounts or store subscription login sessions.
Use exact model IDs available to your account, not marketing display names.

### API key

1. Create an encrypted Credential containing the provider's API key.
2. Create a Provider with kind **Anthropic Messages API** or **OpenAI Responses API**.
3. Select the credential, set the exact model ID and priority, and enable it.

API calls incur your provider's charges. There is no configured monthly spend cap. Do not put
subscription tokens into API-key fields. Enable only the routes you intend to use; configured
API fallbacks can incur API charges.

### Subscription CLI

Build the pinned CLI image and authenticate the provider you intend to use:

```sh
docker build -f cli/Dockerfile -t auto-reviewer-cli:local .
docker image inspect auto-reviewer-cli:local --format '{{.Id}}'
bash scripts/cli_login.sh codex
# Or, for Claude:
bash scripts/cli_login.sh claude
```

Complete the official browser/device login yourself. The scripts use the default private volumes
`auto-reviewer_codex-auth` and `auto-reviewer_claude-auth`; retain matching `.env` volume settings.
Put the printed immutable `sha256:...` image ID in `CLI_RUNNER_IMAGE`. Apply environment changes
before use (initial service recreation, or a drained deployment on an active installation).

Create a Provider of kind **Codex subscription CLI** or **Claude Code subscription CLI**, leave
its credential empty, and set the exact model ID and priority. Probe using your model ID:

```sh
docker compose exec -T worker python manage.py probe_cli codex_cli YOUR_MODEL_ID
# Or:
docker compose exec -T worker python manage.py probe_cli claude_cli YOUR_MODEL_ID
```

Probes make model calls but never enqueue reviews or publish to GitHub/Sheets. Synthetic probes
can return `complete: false`; they verify connectivity/structured output, not real review quality.
Drain workers and probes before later login repair, credential replacement or runner image changes.
No host personal settings or GitHub credentials are mounted in model containers.

### Failure behavior

Outages, refusals and invalid review output may advance through the configured route. Incomplete
CLI evidence gets one fresh recovery pass before fallback. API quota waits on the selected
provider; exhausted CLI logins skip models sharing that login. Authentication/configuration errors
need operator action. Inspect model-attempt diagnostics; raw responses and private reasoning are
not exposed. There is no whole-review deadline; specialist/adjudication phases have individual
budgets. Interrupting work cannot undo incurred provider charges.

## Enable real publication

Keep `LIVE_WRITES=0` until repository credentials, labels, webhook delivery and model access are
configured. Set `LIVE_WRITES=1` in `.env`, then apply a drained [deployment](deployment.md).
This permits GitHub comments, reviews, thread resolution and configured label changes.

For a real, authorized test PR:

1. Open a non-draft PR; request the configured reviewer and apply `on review` (either order).
2. Watch the dashboard job and progress, then inspect GitHub for the review.
3. Push fixes; re-request the reviewer if GitHub consumed the earlier request and retoggle
   `feedback` to `on review`. A push alone does not request another review.

A review can reply to and resolve verified fixed current-PR review threads, including human
threads. It does not resolve related-PR threads or ordinary issue comments. Owner retries create
a new attempt after eligibility checks; deployment or credential repair alone does not retry jobs.

## Approval, CI and merging

By default, ready reviews apply the `approved` workflow label, **not** a formal GitHub approval.
Blocking feedback can submit `REQUEST_CHANGES`. Owner Settings can separately enable automatic
formal approval and automatic merging per repository; both default off.

Formal approval requires complete analysis, no blocking findings and passing configured local
tests. It is pinned to the reviewed commit, cannot approve the connected account's own PR and
is refused when GitHub native auto-merge is already enabled. Turning approval on is not retroactive.
Tracked stale service approvals are dismissed after pushes; human approvals are not dismissed.

Configured GitHub CI checks are advisory for review and formal approval, and their same-commit
status appears in the summary. Missing/failing CI does not block model analysis. Automatic merging
additionally requires passing configured CI, current approval, matching commit and GitHub merge
rules/state. It makes one guarded attempt, not a background merge queue; blocked or uncertain
attempts require inspection. No branch-rule bypass or automatic branch deletion occurs.

## Optional local tests

In repository admin, set a digest-pinned `test_image` and argument-array `test_command`, for example
`["python", "-m", "unittest", "discover"]`. Prebuild the image with all dependencies and `/bin/sh`
and `tar`; images declaring volumes are rejected. Tests run without network, publishing credentials,
Docker socket or host mounts, with a read-only root and bounded resources. Test failures block
clearance. Logs are not retained; the review receives pass/fail evidence.

See [optional integrations](optional-integrations.md) for Sheets, journal, Telegram and graphs.
