#!/usr/bin/env python3
"""Guided first installation. Uses Python's standard library and Docker Compose."""

import argparse
import fcntl
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = [
    "docker",
    "compose",
    "--env-file",
    ".env",
    "-p",
    "auto-reviewer",
    "-f",
    "compose.yaml",
    "-f",
    "compose.postgres.yaml",
]


def private_write(path, text):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".setup-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def update_env(path, values):
    if any(
        not re.fullmatch(r"[A-Z_]+", k) or not re.fullmatch(r"[A-Za-z0-9_:.-]*", v)
        for k, v in values.items()
    ):
        raise ValueError("Unsafe environment value")
    lines = path.read_text().splitlines()
    found = set()
    for index, line in enumerate(lines):
        key = line.partition("=")[0]
        if key in values:
            lines[index] = f"{key}={values[key]}"
            found.add(key)
    lines.extend(f"{key}={value}" for key, value in values.items() if key not in found)
    private_write(path, "\n".join(lines) + "\n")


def save_state(root, state):
    state["env_hash"] = hashlib.sha256((root / ".env").read_bytes()).hexdigest()
    (root / "var").mkdir(exist_ok=True)
    private_write(root / "var/setup-state.json", json.dumps(state))


def load_state(root):
    try:
        state = json.loads((root / "var/setup-state.json").read_text())
        unchanged = state["env_hash"] == hashlib.sha256((root / ".env").read_bytes()).hexdigest()
    except (OSError, ValueError, KeyError):
        raise SystemExit(
            "No resumable setup found. See docs/installation.md for manual setup."
        ) from None
    if not unchanged or state["step"] == "done":
        raise SystemExit(
            "Setup is complete or .env changed. Refusing to change this installation; use the manual guides."
        )
    return state


def run(args, *, capture=False, payload=None, extra_env=None):
    # Never source .env as shell code or put keys in arguments.
    env = dict(os.environ)
    for line in (ROOT / ".env.example").read_text().splitlines():
        key, sep, _ = line.partition("=")
        if sep and re.fullmatch(r"[A-Z_]+", key):
            env.pop(key, None)
    env.pop("COMPOSE_FILE", None)
    env.update(extra_env or {})
    return subprocess.run(
        args, cwd=ROOT, env=env, check=True, text=True, input=payload, capture_output=capture
    )


def ask(label, default=""):
    while True:
        value = input(f"{label}" + (f" [{default}]" if default else "") + ": ").strip() or default
        if value and not any(ord(c) < 32 for c in value):
            return value
        print("Please enter a value.")


def choose(label, options):
    print(label)
    for index, option in enumerate(options, 1):
        print(f"  {index}. {option}")
    while True:
        answer = ask("Choose a number", "1")
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1


def manage(*args, capture=False, payload=None):
    return run(
        COMPOSE + ["exec", "-T", "web", "python", "manage.py", *args],
        capture=capture,
        payload=payload,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["standalone", "worker"], default="standalone")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue this wizard after an interruption; never overwrite a changed .env.",
    )
    args, extra = parser.parse_known_args(argv)
    if args.mode == "worker":
        if __package__:
            from scripts import setup_worker
        else:
            import setup_worker
        return setup_worker.main((["--resume"] if args.resume else []) + extra)
    if extra:
        parser.error("unrecognized arguments: " + " ".join(extra))
    if not sys.stdin.isatty():
        parser.exit(1, "Run setup in an interactive terminal. Manual setup: docs/installation.md\n")
    if not args.resume and (ROOT / ".env").exists():
        parser.exit(
            1,
            "Existing .env preserved. Use --resume only for an interrupted wizard, or the manual guides for an existing installation.\n",
        )
    print(
        "\nAuto Reviewer — guided setup\nDocker + Python 3 required. No host Codex/Claude installation needed.\n"
    )
    try:
        run(["docker", "info"], capture=True)
        run(["docker", "compose", "version"], capture=True)
    except (OSError, subprocess.CalledProcessError):
        raise SystemExit(
            "Docker is unavailable. Start Docker Engine, check access with docker info, and install the Compose plugin. No configuration was changed."
        ) from None
    if args.resume:
        state = load_state(ROOT)
    else:
        for resource in ("container", "volume"):
            if run(
                [
                    "docker",
                    resource,
                    "ls",
                    "-aq" if resource == "container" else "-q",
                    "--filter",
                    "label=com.docker.compose.project=auto-reviewer",
                ],
                capture=True,
            ).stdout.strip():
                raise SystemExit(
                    "An auto-reviewer Docker installation already exists. Setup will not reuse its data. See docs/deployment.md."
                )
        print("[1/5] Where will it run?")
        server = (
            choose(
                "Dashboard access", ["This computer / private VPS via SSH", "Public HTTPS domain"]
            )
            == 1
        )
        init = [sys.executable, "scripts/init_local.py"]
        url = "http://127.0.0.1:8040"
        if server:
            url = ask("Your HTTPS URL (DNS and reverse proxy must already be configured)")
            init += ["--mode", "server", "--public-url", url]
        print(
            "GitHub needs a public HTTPS address to send PR events, even when your dashboard is local."
        )
        webhook = input("Public webhook forwarding URL (Enter to configure later): ").strip()
        if webhook:
            init += ["--webhook-url", webhook]
        kind = ["codex_cli", "claude_cli", "openai", "anthropic"][
            choose(
                "Connect a model",
                [
                    "Codex subscription",
                    "Claude Code subscription",
                    "OpenAI API key (usage billed)",
                    "Anthropic API key (usage billed)",
                ],
            )
        ]
        print(
            "Use an exact model ID supported by your account. You can change routing later in Workflow."
        )
        model = ask("Model ID")
        if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,120}", model):
            raise SystemExit("Invalid model ID; use the model identifier, not its display name.")
        username = ask("Administrator username", "admin")
        workspace = ask("Workspace name", "My workspace")
        slug = unicodedata.normalize("NFKD", workspace).encode("ascii", "ignore").decode().lower()
        slug = re.sub(r"[-\s]+", "-", re.sub(r"[^\w\s-]", "", slug)).strip("-_")
        if (
            not re.fullmatch(r"[\w.@+-]{1,150}", username)
            or not slug
            or len(slug) > 50
            or len(workspace) > 100
        ):
            raise SystemExit(
                "Use a valid username and a workspace name with a short ASCII name. No configuration was created."
            )
        run(init)
        state = dict(
            step="start",
            kind=kind,
            model=model,
            username=username,
            workspace=workspace,
            url=url.rstrip("/"),
        )
        save_state(ROOT, state)
    if state["step"] == "start":
        print(
            "\n[2/5] Starting the application and PostgreSQL. First build can take several minutes."
        )
        run(COMPOSE + ["up", "-d", "--build"])
        for attempt in range(60):
            try:
                manage("check", capture=True)
                break
            except subprocess.CalledProcessError:
                if attempt == 59:
                    raise SystemExit(
                        "Application did not become ready. Inspect docker compose logs; then run python3 scripts/setup.py --resume."
                    )
                time.sleep(2)
        state["step"] = "account"
        save_state(ROOT, state)
    if state["step"] == "account":
        print("\n[3/5] Creating your administrator and workspace.")
        result = manage(
            "bootstrap_admin",
            "--setup-link",
            "--json",
            "--username",
            state["username"],
            "--organization",
            state["workspace"],
            capture=True,
        )
        account = json.loads(result.stdout)
        update_env(ROOT / ".env", {"DASHBOARD_RETRY_USER_ID": str(account["owner_id"])})
        state["step"] = "model"
        save_state(ROOT, state)
        print("Private password setup link (expires in one hour):\n" + account["setup_url"])
        print("Keep this terminal output private. Set your password after setup finishes.")
    if state["step"] == "model":
        print(
            "\n[4/5] Connecting your model. Login stays in this installation, separate from your personal CLI."
        )
        manage("setup_provider", "--check")
        payload = dict(kind=state["kind"], model=state["model"])
        if state["kind"].endswith("_cli"):
            run(["docker", "build", "-f", "cli/Dockerfile", "-t", "auto-reviewer-cli:local", "."])
            image = run(
                ["docker", "image", "inspect", "auto-reviewer-cli:local", "--format", "{{.Id}}"],
                capture=True,
            ).stdout.strip()
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
                raise SystemExit("Docker did not return a valid immutable image ID.")
            update_env(ROOT / ".env", {"CLI_RUNNER_IMAGE": image})
            save_state(ROOT, state)
            run(
                ["bash", "scripts/cli_login.sh", state["kind"].removesuffix("_cli")],
                extra_env={"CLI_RUNNER_IMAGE": image},
            )
        else:
            payload["key"] = getpass.getpass("API key (hidden; stored encrypted): ")
        manage("setup_provider", payload=json.dumps(payload))
        run(
            COMPOSE
            + [
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "web",
                "events",
                "worker",
                "maintenance",
                "keepalive",
            ]
        )
        state["step"] = "verify"
        save_state(ROOT, state)
    if state["step"] == "verify":
        print("\n[5/5] Check the model connection.")
        print(
            "This small probe uses model quota (API calls may be billed), but never posts to GitHub."
        )
        if choose("Test model access now?", ["Yes (recommended)", "Skip; verify later"]) == 0:
            run(COMPOSE + ["exec", "-T", "worker", "python", "manage.py", "probe_provider"])
            print("Model connection verified.")
        else:
            print("Connection test skipped; model access is NOT verified.")
        state["step"] = "done"
        save_state(ROOT, state)
    print("\nInstalled. Finish in your dashboard: " + state["url"])
    print("  1. Set your password using the private link printed above, then sign in.")
    print("  2. Settings → Add repository. Follow its webhook connection feedback.")
    print(
        "  3. Enable publication only after webhook/model checks: docs/configuration.md#enable-real-publication"
    )
    print("GitHub review publication is OFF. HTTPS proxy/tunnel setup is not automated.")
    print("Lost/expired password link? See docs/troubleshooting.md. Advanced routing: Workflow.")


if __name__ == "__main__":
    try:
        (ROOT / "var").mkdir(exist_ok=True)
        with os.fdopen(
            os.open(ROOT / "var/setup.lock", os.O_CREAT | os.O_RDWR, 0o600), "w"
        ) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            raise SystemExit(main())
    except (subprocess.CalledProcessError, OSError) as exc:
        # Child output may contain tokens or setup links; do not dump captured stderr.
        print(
            f"Setup stopped ({type(exc).__name__}). Your saved secrets were not replaced. Fix the failed step, then run: python3 scripts/setup.py --resume",
            file=sys.stderr,
        )
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print(
            "\nSetup interrupted. Continue with: python3 scripts/setup.py --resume", file=sys.stderr
        )
        sys.exit(1)
