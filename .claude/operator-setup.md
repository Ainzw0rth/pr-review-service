# Operator activation guide

## Current VPS configuration

- Public dashboard: https://auto-reviewer.ainzw0rth.xyz/
- Admin: https://auto-reviewer.ainzw0rth.xyz/admin/
- Host port: `127.0.0.1:8040`.
- Docker tunnel origin: `http://auto-reviewer-web-1:8040`.
- VPS-local, ignored `compose.override.yaml` joins the web container to the existing
  `tsunagu_default` network. Preserve/recreate this override when moving the deployment.
- Monitored repository: `MarkAny-GaneshaIT/AnyOne-2.0`.
- Requested reviewer: `Ainzw0rth`; dashboard organization: `ganeshait`.
- Existing authorized GitHub CLI token stored encrypted in application Credentials.
  It is a snapshot: replacing/revoking the CLI token does not update this connection automatically.
- Required CI check: `required`, the repository CI workflow's aggregate gate.
  Missing, pending, cancelled or failed gate blocks service clearance. This does not install
  GitHub branch protection or prevent humans from merging outside the agreed workflow.
- Workflow labels created; signed Pull requests webhook registered and ping returned HTTP 200.
- `LIVE_WRITES=0`: no live reviews or Sheet updates until activation checks finish.

## 1. Set the administrator password

On the VPS, read the private one-hour setup link:

```bash
cat /root/projects/pr-review-service/var/admin-setup.txt
```

Open it in a browser and choose a password. Username: `louis`.
If the unused link expired, regenerate it privately:

```bash
cd /root/projects/pr-review-service
umask 077
docker compose exec -T web python manage.py bootstrap_admin --setup-link > var/admin-setup.txt
```

For an already configured account, use `access_link --user louis` instead of bootstrap.

## 2. Anthropic API key

1. Sign in to [Claude Console](https://platform.claude.com/).
2. Configure API billing/credits in the Console billing settings.
3. Open [API keys](https://platform.claude.com/settings/keys), create a key named
   `pr-review-service`, scoped to one workspace. This adapter does not send a multi-workspace
   selector. One key can serve both Anthropic provider rows if both models are available.
4. Open the service admin → Credentials → Add. Name: `anthropic-review`.
   Paste the key into **Secret**, then save. Do not put it in chat or Git.

[Official Claude setup documentation](https://platform.claude.com/docs/en/get-started).
Claude chat subscriptions and Console API billing are separate:
[billing explanation](https://support.claude.com/en/articles/9876003-i-have-a-paid-claude-subscription-pro-max-team-or-enterprise-plans-why-do-i-have-to-pay-separately-to-use-the-claude-api-and-console).

## 3. OpenAI API key

1. Sign in to [OpenAI Platform](https://platform.openai.com/).
2. Create/select a dedicated project, e.g. `pr-review-service`.
3. Configure API billing/credits for the organization.
4. Open [API keys](https://platform.openai.com/api-keys), create a project key named
   `pr-review-service`. If using restricted permissions, allow the Responses endpoint;
   model-list read access is useful for connection verification.
5. Service admin → Credentials → Add. Name: `openai-review`.
   Paste the key into **Secret**, then save.

[Official OpenAI API quickstart](https://developers.openai.com/api/docs/quickstart).
ChatGPT subscription billing is separate from API usage:
[billing explanation](https://help.openai.com/en/articles/9039756-managing-billing-settings-on-the-chatgpt-web-and-api-platform).

Only one provider key is necessary to begin. Both enable cross-provider availability fallback.
This implementation uses API keys, not subscription OAuth sessions. Quota exhaustion waits;
policy refusals pause. Neither is treated as a reason to bypass the provider through rotation.

## 4. Select provider models

Admin → Providers → Add each enabled model using its exact available API model ID:

| Order | API adapter | Credential |
| --- | --- | --- |
| 10 | Anthropic Messages | anthropic-review |
| 20 | OpenAI Responses | openai-review |
| 30 | Anthropic Messages | anthropic-review |

The requested display labels Opus 5.5 / Sol 6.1 / Opus 4.8 have not been assumed to be API IDs.
Verify available IDs against the connected accounts before populating these rows. Missing a
provider key is fine: omit that provider row. Validate authentication, model access and structured
output compatibility before enabling real reviews.

## 5. Google Sheets

The supplied spreadsheet is readable. User confirmed **New Timeline** and this mapping:

| Service outcome | Sheet Status |
| --- | --- |
| Reviewing | In review |
| Feedback | In progress |
| Approved / awaiting merge | On PR |
| Every required linked PR merged | Completed |

Headers are on row 3; ID column B, Status column K. Keep title/section rows intact.
Local, unconfirmed connection proposal: `var/sheet-connection-proposal.json` (ignored).
Task → GitHub issue → required PR links still require confirmation. Sheet sync remains disabled.

1. Create/select a Google Cloud project and [enable Google Sheets API](https://developers.google.com/workspace/sheets/api/quickstart/python#enable_the_api).
2. IAM & Admin → Service Accounts → Create service account, e.g. `pr-review-service`.
   No broad Google Cloud role or domain-wide delegation is required for one shared Sheet.
3. Open the account → Keys → Add key → Create new key → JSON. Download privately.
4. Share only the target spreadsheet with the service-account email as **Editor**.
5. Service admin → Credentials → Add: name `google-sheets-review`; paste the complete JSON
   into Secret. Select that credential on the Repository record during Sheet setup.
6. Inspect with `python manage.py inspect_sheet 1` after supplying the connection fields.
   Confirm actual dropdown vocabulary, stable task IDs and their required PR sets. Only then
   install the complete `confirmed: true` mapping. Keep live writes disabled during onboarding.

[Official service-account and sharing instructions](https://developers.google.com/workspace/guides/create-credentials#service-account).

## 6. Activation

After provider verification and any optional Sheet setup, complete the rollout checks in
[the team workflow](team-auto-review-workflow.md). Then set `LIVE_WRITES=1` in private `.env`
and run `docker compose up -d` from the project directory. Developers request `Ainzw0rth` and
toggle `on review` for a fresh attempt. Do not represent mocked tests or webhook pings as a
successful end-to-end model review.
