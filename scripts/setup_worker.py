#!/usr/bin/env python3
"""Worker-only source/release installer. No Django, database or coordinator secrets."""

import argparse
import fcntl
import getpass
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIN_FREE_BYTES = 5 * 1024**3
PIN = re.compile(r"(?:sha256:[0-9a-f]{64}|[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64})\Z")


class SetupError(Exception):
    """Safe, operator-facing error; never includes captured process or HTTP output."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SetupError("Coordinator redirected the request. Use its canonical HTTPS origin.")


def pairing_code():
    # getpass otherwise falls back to visible stdin when no controlling terminal exists.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass("Pairing code (hidden): ")
        except getpass.GetPassWarning:
            raise SetupError("A terminal with hidden input is required for pairing.") from None


def coordinator_origin(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
            and not any(c.isspace() for c in value)
        )
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise SetupError("Coordinator must be an HTTPS origin without credentials or a path.")
    return value.rstrip("/")


def request_json(url, payload=None):
    request = urllib.request.Request(
        url,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "auto-reviewer-worker/1",
        },
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise SetupError("Coordinator response exceeded the protocol limit.")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise SetupError("Coordinator rate limit reached. Wait, then resume setup.") from None
        raise SetupError(
            f"Coordinator rejected request (HTTP {exc.code}). Check URL and pairing code."
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise SetupError(
            "Coordinator unreachable or invalid response. Check HTTPS/TLS and DNS."
        ) from None


def run(args, *, interactive=False, env=None):
    try:
        result = subprocess.run(
            args,
            cwd=ROOT,
            env=env,
            check=True,
            text=True,
            stdout=None if interactive else subprocess.PIPE,
            stderr=None if interactive else subprocess.PIPE,
        )
        return result.stdout or ""
    except (OSError, subprocess.CalledProcessError):
        raise SetupError(
            "Docker operation failed. Check Docker access, available disk and network; resume setup."
        ) from None


def private_directory(path):
    if path.is_symlink():
        raise SetupError("State directory must not be a symbolic link.")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise SetupError("State directory must be owned by this user with mode 0700.")


def private_write(path, value, *, create=False):
    fd, name = tempfile.mkstemp(prefix=".worker-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            os.link(name, path)  # Exclusive creation; never clobber an enrolled identity.
        else:
            os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_config(path):
    if path.is_symlink() or not path.is_file():
        raise SetupError("No private worker configuration found; use initial setup first.")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise SetupError("Worker configuration must be owned by this user with mode 0600.")
    try:
        config = json.loads(path.read_text())
        coordinator_origin(config["coordinator"])
        uuid.UUID(config["worker_id"])
        if not isinstance(config["token"], str) or len(config["token"]) < 16:
            raise ValueError
        if config["slots"] != 1 or config["state_dir"] != "/state":
            raise ValueError
        if not isinstance(config["images"], dict):
            raise ValueError
        for image in config["images"].values():
            if not isinstance(image, str) or not PIN.fullmatch(image):
                raise ValueError
        if not isinstance(config["account_kinds"], list) or any(
            kind not in ("codex_cli", "claude_cli") for kind in config["account_kinds"]
        ):
            raise ValueError
        for volume in config["auth_volumes"].values():
            if not isinstance(volume, str) or not re.fullmatch(
                r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", volume
            ):
                raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise SetupError("Invalid worker configuration; refusing to overwrite it.") from None
    return config


def preflight(state):
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise SetupError("Only Linux amd64 workers are currently verified.")
    if shutil.disk_usage(state).free < MIN_FREE_BYTES:
        raise SetupError("At least 5 GiB free in worker state storage is required.")
    try:
        info = json.loads(run(["docker", "info", "--format", "{{json .}}"]))
    except ValueError:
        raise SetupError("Cannot inspect Docker daemon.") from None
    if info.get("OSType") != "linux" or info.get("Architecture") not in ("x86_64", "amd64"):
        raise SetupError("Docker must run Linux amd64 containers.")
    # Bind paths refer to the local daemon; a remote Docker endpoint is unsupported.
    endpoint = run(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"])
    endpoint = os.environ.get("DOCKER_HOST") or endpoint.strip()
    if endpoint != "unix:///var/run/docker.sock":
        raise SetupError("Use the local Docker daemon at unix:///var/run/docker.sock.")
    docker_root = info.get("DockerRootDir")
    if not docker_root or not Path(docker_root).is_dir():
        raise SetupError("Cannot inspect Docker storage path; run with Docker storage read access.")
    if shutil.disk_usage(docker_root).free < MIN_FREE_BYTES:
        raise SetupError("At least 5 GiB free in Docker image storage is required.")
    run(["docker", "compose", "version"])


def enroll(origin, name, code, providers):
    response = request_json(origin + "/api/workers/v1/enroll/", {"code": code, "name": name})
    try:
        worker_id = str(uuid.UUID(response["worker_id"]))
        token = response["token"]
        if response["version"] != 1 or not isinstance(token, str) or len(token) < 16:
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise SetupError(
            "Invalid enrollment response. Ask the owner to inspect the worker registry."
        ) from None
    return {
        "coordinator": origin,
        "worker_id": worker_id,
        "token": token,
        "state_dir": "/state",
        "slots": 1,
        "images": {},
        "auth_volumes": {kind: f"worker-{worker_id}-{kind}" for kind in providers},
        "test_images": [],
        "account_kinds": providers,
        "graph_cache_volume": f"worker-{worker_id}-graph-cache",
    }


RELEASE_REGISTRY = "ghcr.io/ainzw0rth/pr-review-service"


def _checkout_revision():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        raise SetupError("Cannot verify checkout revision for the release manifest.") from None


def _manifest_object(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("Duplicate manifest key")
        value[key] = item
    return value


def load_release_manifest(path):
    """A downloaded trusted release asset, never a source of arbitrary image repositories."""
    try:
        with Path(path).open("rb") as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        manifest = json.loads(raw, object_pairs_hook=_manifest_object)
        if not isinstance(manifest, dict) or set(manifest) != {
            "version",
            "protocol",
            "platform",
            "revision",
            "images",
        }:
            raise ValueError()
        if (
            not isinstance(manifest["version"], str)
            or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", manifest["version"])
            or type(manifest["protocol"]) is not int
            or manifest["protocol"] != 1
            or manifest["platform"] != "linux/amd64"
            or not isinstance(manifest["revision"], str)
            or not re.fullmatch(r"[0-9a-f]{40}", manifest["revision"])
        ):
            raise ValueError()
        roles = {"agent": "agent", "model": "cli", "coordinator": "coordinator"}
        images = manifest["images"]
        if not isinstance(images, dict) or set(images) != set(roles):
            raise ValueError()
        for role, component in roles.items():
            if not isinstance(images[role], str) or not re.fullmatch(
                re.escape(f"{RELEASE_REGISTRY}/{component}") + r"@sha256:[0-9a-f]{64}",
                images[role],
            ):
                raise ValueError()
    except (OSError, ValueError, TypeError, RecursionError):
        raise SetupError(
            "Invalid release manifest: expected verified amd64 protocol-1 release digests from the trusted repository."
        ) from None
    if manifest["revision"] != _checkout_revision():
        raise SetupError(
            "Release source revision does not match this checkout. Check out the manifest's release revision first."
        )
    return manifest


def pull_release_images(config, path, manifest):
    """Pull only required immutable images; preserve all worker identity and authentication state."""
    images = manifest["images"]
    print(f"Pulling verified release {manifest['version']} for linux/amd64...")
    run(["docker", "pull", "--platform=linux/amd64", images["agent"]])
    runtime = image_id(images["agent"])
    if config["account_kinds"]:
        run(["docker", "pull", "--platform=linux/amd64", images["model"]])
        config["images"]["model"] = image_id(images["model"])
        private_write(path, config)
    return runtime


def image_id(tag):
    value = run(["docker", "image", "inspect", "--format", "{{.Id}}", tag]).strip()
    if not PIN.fullmatch(value):
        raise SetupError("Docker did not return an immutable image identity.")
    return value


def build_images(config, path):
    # Source-build default: resolve every image to its immutable local content ID.
    cli_tag = "auto-reviewer-worker-cli:" + config["worker_id"]
    agent_tag = "auto-reviewer-agent:" + config["worker_id"]
    if config["account_kinds"]:
        print("Building pinned CLI runner from this checkout...")
        run(
            [
                "docker",
                "build",
                "--platform=linux/amd64",
                "-f",
                "cli/Dockerfile",
                "-t",
                cli_tag,
                ".",
            ]
        )
        config["images"]["model"] = image_id(cli_tag)
        private_write(path, config)
    print("Building worker runtime from this checkout...")
    run(
        [
            "docker",
            "build",
            "--platform=linux/amd64",
            "-f",
            "execution/Dockerfile",
            "-t",
            agent_tag,
            ".",
        ]
    )
    return image_id(agent_tag)


def login(config, kind):
    image = config["images"]["model"]
    volume = config["auth_volumes"][kind]
    run(["docker", "volume", "create", volume])
    run(
        [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--user=0:0",
            "--read-only",
            "--mount",
            f"type=volume,source={volume},target=/auth",
            "--entrypoint=node",
            image,
            "-e",
            'const fs=require("fs");fs.chownSync("/auth",1000,1000);fs.chmodSync("/auth",0o700);',
        ]
    )
    command = (
        ["flock", "--exclusive", "/auth/.codex-refresh.lock", "codex", "login", "--device-auth"]
        if kind == "codex_cli"
        else ["claude", "auth", "login"]
    )
    variable = "CODEX_HOME" if kind == "codex_cli" else "CLAUDE_CONFIG_DIR"
    print(f"Authenticate {kind} independently on this host using the intended provider account.")
    run(
        [
            "docker",
            "run",
            "--rm",
            "-it",
            "--user=1000:1000",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=128m,mode=1777",
            "--mount",
            f"type=volume,source={volume},target=/auth",
            "--env",
            "HOME=/tmp",
            "--env",
            f"{variable}=/auth",
            "--entrypoint",
            "/usr/bin/env",
            image,
            *command,
        ],
        interactive=True,
    )


def compose(config, state, runtime, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_")}
    env.update(
        WORKER_STATE_DIR=str(state),
        WORKER_AGENT_IMAGE=runtime,
        WORKER_UID=str(os.getuid()),
        WORKER_GID=str(os.getgid()),
        WORKER_DOCKER_GID=str(os.stat("/var/run/docker.sock").st_gid),
    )
    return run(
        [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "--project-directory",
            str(ROOT),
            "-p",
            "worker-" + config["worker_id"],
            "-f",
            str(ROOT / "compose.worker.yaml"),
            *args,
        ],
        env=env,
    )


def install(args):
    state = Path(args.state_dir).expanduser().absolute()
    private_directory(state)
    # One installer owns enrollment/configuration at a time. No credential in argv/env/logs.
    with (
        open(state / ".setup.lock", "a", opener=lambda p, flags: os.open(p, flags, 0o600)) as lock,
        open(
            state / "agent.lock", "a", opener=lambda p, flags: os.open(p, flags, 0o600)
        ) as agent_lock,
    ):
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError("Another worker installer is using this state directory.") from None
        # Share the runtime's process lock: config/image/auth updates must never
        # race a live agent, even when its heartbeat appears idle or unreachable.
        try:
            fcntl.flock(agent_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError(
                "Worker agent is running. Drain it, confirm all work and cleanup idle, "
                "then stop the agent before running setup with --resume."
            ) from None
        manifest_path = getattr(args, "release_manifest", None)
        manifest = load_release_manifest(manifest_path) if manifest_path else None
        path = state / "worker.json"
        if args.resume:
            config = load_config(path)
        else:
            if path.exists() or path.is_symlink():
                raise SetupError(
                    "Worker already enrolled here. Use --resume; identity will not be overwritten."
                )
            if args.noninteractive:
                raise SetupError(
                    "Initial enrollment requires interactive secret input. Use --resume for automation."
                )
            origin = coordinator_origin(input("Coordinator HTTPS origin: ").strip())
            name = input("Worker name: ").strip()
            if not name or len(name) > 80:
                raise SetupError("Worker name must contain 1–80 characters.")
            selected = (
                input("Local CLI providers [codex,claude] (or none): ").strip() or "codex,claude"
            )
            providers = (
                []
                if selected == "none"
                else [f"{item.strip()}_cli" for item in selected.split(",")]
            )
            if len(set(providers)) != len(providers) or any(
                p not in ("codex_cli", "claude_cli") for p in providers
            ):
                raise SetupError("Choose codex, claude, codex,claude, or none.")
            preflight(state)
            # Probe TLS and reachability before asking for a one-use secret.
            request_json(origin + "/healthz/")
            config = enroll(origin, name, pairing_code(), providers)
            private_write(path, config, create=True)
            print("Enrollment saved privately. Interrupted setup can now use --resume.")
        if args.resume:
            preflight(state)
        runtime = (
            pull_release_images(config, path, manifest)
            if manifest is not None
            else build_images(config, path)
        )
        receipt = state / "setup.json"
        done = json.loads(receipt.read_text()) if receipt.exists() else {"logged_in": []}
        if not args.skip_login:
            for kind in config["account_kinds"]:
                if kind in done["logged_in"]:
                    continue
                if args.noninteractive:
                    raise SetupError(
                        "Provider login incomplete. Resume interactively, or use --skip-login to defer it."
                    )
                login(config, kind)
                done["logged_in"].append(kind)
                private_write(receipt, done)
        compose(
            config,
            state,
            runtime,
            "run",
            "--rm",
            "agent",
            "--config",
            "/state/worker.json",
            "--check-config",
        )
        receipt_data = {"image": runtime, "source": str(ROOT)}
        if manifest is not None:
            receipt_data.update(version=manifest["version"], revision=manifest["revision"])
        private_write(state / "runtime.json", receipt_data)
        if not args.no_start:
            # All private-state/auth mutations are finished. The new agent must
            # acquire this same lock; retain the installer lock through startup.
            fcntl.flock(agent_lock, fcntl.LOCK_UN)
            compose(config, state, runtime, "up", "-d", "--no-build")
        print("Worker configured" + ("; agent started." if not args.no_start else "; not started."))
        print(
            "Owner: bind the same provider account groups in Workers. Confirm fresh heartbeat and readiness there."
        )
        print(
            "No Ready claim made: login, account bindings and coordinator remote scheduling must be verified."
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default=str(ROOT / "var" / "worker"))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--release-manifest",
        type=Path,
        help="Downloaded worker-release.json from the matching trusted release; otherwise build from source",
    )
    parser.add_argument("--noninteractive", action="store_true")
    parser.add_argument(
        "--skip-login",
        action="store_true",
        help="Defer local authentication; does not establish readiness",
    )
    parser.add_argument("--no-start", action="store_true")
    args = parser.parse_args(argv)
    try:
        install(args)
    except (SetupError, OSError, ValueError, EOFError, KeyboardInterrupt) as exc:
        message = (
            str(exc)
            if isinstance(exc, SetupError)
            else "Setup interrupted or local state unreadable. Fix permissions/storage and resume."
        )
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
