# First review in three steps

**You need:** Git, Python 3, and Docker Engine with Compose using Linux containers.
Have a Codex/Claude subscription **or** an OpenAI/Anthropic API key, plus a GitHub
account/token authorized for the repository you want to review.

Linux is the supported deployment target. On Windows use WSL/Linux containers;
on macOS use a compatible Linux Docker environment. Those host combinations are not
verified here. Start with a fresh checkout—not an existing production installation.

## 1. Start the guided setup

```sh
git clone https://github.com/Ainzw0rth/pr-review-service.git
cd pr-review-service
python3 scripts/setup.py
```

The wizard asks where the dashboard will run, which provider/model to use, and
what to call your administrator/workspace. Use the **exact model ID** available to
your account, not its display name.

It handles the rest:

- Generates private configuration and starts the application/database.
- Creates your administrator and prints a private password-setup link.
- For Codex/Claude: installs the CLIs inside Docker, starts official login, and saves
  the login for later runs. **You do not install a separate host CLI.**
- For APIs: asks for your key without displaying it and stores it encrypted.
- Registers the provider and offers a small connection test. This uses model quota;
  API calls may be billed. It never publishes a review.

Follow the login prompt yourself. The first build can take several minutes.
No copying image hashes, database IDs or provider records is required.

**Interrupted?** Fix the failed step, then run:

```sh
python3 scripts/setup.py --resume
```

Resume preserves generated secrets. If `.env` has been edited or setup already
completed, the wizard stops rather than modifying that installation. Use the
[manual installation](installation.md) or [troubleshooting guide](troubleshooting.md).

## 2. Add your repository

Open the private password link, set your password, then sign in.
Go to **Settings → Add repository** and enter:

| Field | What to enter |
| --- | --- |
| Workspace | The workspace you just created |
| Repository | `owner/repository`, not a GitHub URL |
| Reviewer | GitHub username of the account that will post reviews |
| GitHub token | A token belonging to that reviewer account |

The token needs repository read access, PR/issues write access, and webhook read/write
access. [Exact permissions and organization authorization](configuration.md#repository-setup-in-settings).
The reviewer cannot approve its own PRs; use a separate account when needed.

**Local dashboard?** GitHub still needs a public HTTPS webhook forwarding address.
**Public server?** DNS and an HTTPS reverse proxy must already route to the application.
The wizard does not provision tunnels, domains or certificates.
[Forwarding instructions](installation.md#4-make-webhook-ingress-reachable).

Follow Settings' connection feedback. Verify a real webhook delivery in GitHub;
“registered” alone does not prove delivery works.

## 3. Turn on reviews

Once model access and webhook delivery work, change this single value in `.env`:

```dotenv
LIVE_WRITES=1
```

Apply it safely from the project directory:

```sh
export COMPOSE_FILE=compose.yaml:compose.postgres.yaml
python3 scripts/deploy.py
```

Then open a non-draft test PR, **request your reviewer** and apply **`on review`**.
Watch the job in the dashboard; the review appears on GitHub.

**Done.** Change repositories and capacity in **Settings**. Change model routing in
**Workflow**. Automatic approval and merging remain off unless you enable them.

<details>
<summary>Why Docker instead of my already-installed CLI?</summary>

Reviews use isolated containers. Your personal CLI configuration and login are not
borrowed. The wizard installs a compatible runner and stores its login in a persistent
Docker volume. Restarts keep that login; expired sessions can require login again.

</details>

<details>
<summary>Already running this service?</summary>

Do not run the first-install wizard over it. Keep your existing `.env`, database and
login volumes. Use [deployment](deployment.md) for updates and
[configuration](configuration.md#model-provider-choose-api-or-cli) for provider changes.

</details>
