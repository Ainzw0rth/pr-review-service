# Auto Reviewer

Self-hosted, human-triggered GitHub PR reviews. An organization-scoped dashboard shows
queue progress, model activity and operational status; findings stay on GitHub.

- **Explicit review requests:** request the configured reviewer and apply `on review`.
  Pushes and discussion replies alone do not start reviews.
- **Grounded review:** commit-pinned changes, linked issues, documentation and optional
  dependency-graph evidence; code, security and test specialists followed by adjudication.
- **Your model connection:** Anthropic/OpenAI APIs or authenticated Claude Code/Codex CLI runners.
- **Controlled publication:** feedback and workflow labels by default. Formal approval and
  automatic merging are separate, opt-in repository settings, disabled initially.
- **PostgreSQL-backed queue:** one worker supervisor manages configurable concurrent PRs
  and separate provider/resource limits. No Redis required.

## Start here

With Docker Compose and Python 3 installed, run this from a fresh checkout:

```sh
python3 scripts/setup.py
```

**Start → connect model → add repository → first review.** The wizard handles containers,
private configuration, administrator setup and model registration. You complete account
login, then add your repository in Settings. No separate host CLI installation needed.

[Follow the three-step quickstart](docs/quickstart.md). GitHub publication starts disabled;
the final step explicitly enables it. Existing installation? Use the deployment guide instead.

| Guide | Contents |
| --- | --- |
| [Quickstart](docs/quickstart.md) | Guided setup and your first review |
| [Manual installation](docs/installation.md) | Localhost, HTTPS server, first login, public webhook forwarding |
| [Configuration](docs/configuration.md) | Add/Edit/Pause repositories, credentials, providers, review behavior |
| [Deployment](docs/deployment.md) | Proxy, concurrency, drained updates, backups and recovery |
| [Optional integrations](docs/optional-integrations.md) | Google Sheets, summary journal, Telegram and graphs |
| [Troubleshooting](docs/troubleshooting.md) | Startup, login, webhook, provider and queue diagnostics |
| [Existing SQLite migration](docs/postgres-cutover.md) | Supervised transfer of an existing installation |

## Architecture

```mermaid
flowchart LR
    GitHub -->|Signed webhook| Web
    Browser -->|Authenticated dashboard| Web
    Web <--> PostgreSQL
    PostgreSQL <--> Events[Event processor]
    Events <--> GitHub
    PostgreSQL <--> Worker[Worker supervisor]
    Worker <--> GitHub
    Worker --> Models[API providers or isolated CLI runners]
    Worker -.-> Tests[Optional isolated tests and graph parser]
    Maintenance <--> PostgreSQL
```

The trusted worker has Docker-daemon access. Disposable model, test and graph containers
never receive that socket or GitHub publishing credentials. Credentials and retained analysis
checkpoints are encrypted; protect both the database and `.env` encryption key.

## Development

Python 3.12+ with dependencies from `requirements.lock`; PostgreSQL for worker integration.
SQLite remains available for isolated tests, not the recommended service deployment.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install ruff==0.16.10  # Development checks only
.venv/bin/python manage.py test --noinput
.venv/bin/ruff check core reviewdesk manage.py scripts
.venv/bin/ruff format --check core reviewdesk manage.py scripts
DJANGO_SECRET_KEY=local-check-only .venv/bin/python manage.py makemigrations --check --dry-run
```

These checks do not authenticate providers or publish reviews. Live end-to-end verification
requires your own repository, credentials and explicit enablement.
