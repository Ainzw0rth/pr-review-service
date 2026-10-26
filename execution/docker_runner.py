"""Restricted Docker execution. Only host-configured images and named volumes."""

import base64
import json
import os
import re
import selectors
import subprocess
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

from .storage import ReviewCancelled, RunnerError, extract_archive

IMAGE = re.compile(r"(?:sha256:[a-f0-9]{64}|[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64})\Z")
VOLUME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}\Z")
IDENTIFIER = re.compile(r"[a-f0-9]{64}\Z")
ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent"}
DOCKER = ["docker", "--host=unix:///var/run/docker.sock"]
MAX_OUTPUT = 32 * 1024 * 1024


def docker(args, timeout=30):
    try:
        result = subprocess.run(DOCKER + args, capture_output=True, timeout=timeout, env=ENV)
        if result.returncode or len(result.stdout) > 100000:
            raise RunnerError()
        return result.stdout.decode().strip()
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise RunnerError() from None


class DockerRunner:
    def __init__(self, config):
        self.config = config
        self.label = "auto-reviewer.worker=" + config["worker_id"]

    def owned(self):
        result = docker(["ps", "-aq", "--no-trunc", "--filter", "label=" + self.label])
        identifiers = result.splitlines()
        if any(not IDENTIFIER.fullmatch(value) for value in identifiers):
            raise RunnerError()
        return identifiers

    def cleanup(self):
        """No wildcard removal: verify owner and remove immutable IDs, then confirm absence."""
        for identifier in self.owned():
            info = json.loads(docker(["inspect", identifier]))[0]
            if (
                info.get("Config", {}).get("Labels", {}).get("auto-reviewer.worker")
                != self.config["worker_id"]
            ):
                raise RunnerError()
            docker(["rm", "-f", "-v", identifier])
        if self.owned():
            raise RunnerError()

    def base(self, operation):
        args = [
            "create",
            "--name",
            "review-agent-" + uuid.uuid4().hex,
            "--pull=never",
            "--label=" + self.label,
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=128",
            "--ulimit=nofile=1024:1024",
            "--log-driver=none",
            "--ipc=private",
            "--interactive",
        ]
        if operation == "test":
            args += [
                "--network=none",
                "--user=65534:65534",
                "--memory=1g",
                "--memory-swap=1g",
                "--cpus=1",
                "--workdir=/workspace",
                "--env=HOME=/tmp",
                "--tmpfs=/tmp:rw,nosuid,nodev,size=128m,mode=1777",
                "--tmpfs=/workspace:rw,nosuid,nodev,size=512m,mode=700,uid=65534,gid=65534",
            ]
        else:
            args += [
                "--user=" + ("1000:1000" if operation == "model" else "10001:10001"),
                "--memory=2g",
                "--memory-swap=2g",
                "--cpus=2",
                "--tmpfs=/tmp:rw,nosuid,nodev,size=256m,mode=1777",
            ]
            if operation == "graph":
                args += ["--network=none"]
        return args

    def image_info(self, image):
        info = json.loads(docker(["image", "inspect", image]))[0]
        if info["Config"].get("Volumes"):
            raise RunnerError("Runner images must not declare volumes.")
        return info

    def capabilities(self):
        capabilities = {}
        images = self.config["images"]
        for operation in ("model", "graph"):
            if not images.get(operation):
                continue
            try:
                info = self.image_info(images[operation])
                if operation == "model":
                    labels = info["Config"].get("Labels") or {}
                    if any(
                        labels.get(key) != "v1"
                        for key in ("auto-reviewer.auth-coordination", "auto-reviewer.sessions")
                    ):
                        continue
                    for kind, volume in self.config["auth_volumes"].items():
                        authenticated = False
                        try:
                            docker(["volume", "inspect", volume])
                            field = "tokens" if kind == "codex_cli" else "claudeAiOauth"
                            path = (
                                "/auth/auth.json"
                                if kind == "codex_cli"
                                else "/auth/.credentials.json"
                            )
                            keys = (
                                ["access_token", "refresh_token", "id_token", "account_id"]
                                if kind == "codex_cli"
                                else ["accessToken"]
                            )
                            script = (
                                "const f=require('fs');const p=" + json.dumps(path) + ";"
                                "try{if(f.statSync(p).size>65536)process.exit(1);"
                                "const a=JSON.parse(f.readFileSync(p));if(!a["
                                + json.dumps(field)
                                + "])process.exit(1);const t=a["
                                + json.dumps(field)
                                + "];"
                                "if(!"
                                + json.dumps(keys)
                                + ".every(k=>typeof t[k]==='string'&&t[k]))process.exit(1);"
                                "}catch{process.exit(1)}"
                            )
                            args = self.base("model") + [
                                "--network=none",
                                "--mount",
                                f"type=volume,source={volume},target=/auth,readonly",
                                "--entrypoint=node",
                                images["model"],
                                "-e",
                                script,
                            ]
                            identifier = docker(args)
                            if not IDENTIFIER.fullmatch(identifier):
                                raise RunnerError()
                            try:
                                docker(["start", "-a", identifier])
                                state = json.loads(
                                    docker(["inspect", "--format={{json .State}}", identifier])
                                )
                                authenticated = state.get("ExitCode") == 0 and not state.get(
                                    "Running"
                                )
                            finally:
                                docker(["rm", "-f", "-v", identifier])
                        except (RunnerError, ValueError, KeyError):
                            pass
                        capabilities[kind] = {"authenticated": authenticated}
                else:
                    if self.config.get("graph_cache_volume"):
                        docker(["volume", "inspect", self.config["graph_cache_volume"]])
                    capabilities["graph"] = {"authenticated": True}
            except (RunnerError, ValueError, KeyError, IndexError):
                continue
        if self.config["test_images"]:
            try:
                for image in self.config["test_images"]:
                    self.image_info(image)
                capabilities["test"] = {"authenticated": True}
            except (RunnerError, ValueError, KeyError, IndexError):
                pass
        return capabilities

    def execute(self, operation, payload, emit, cancelled, created):
        if operation not in {"model", "graph", "test"} or not isinstance(payload, dict):
            raise RunnerError()
        with tempfile.TemporaryDirectory(prefix="job-", dir=self.config["state_dir"]) as directory:
            args = self.base(operation)
            source_path = Path(directory) / "input"
            if operation == "test":
                image, command = payload.get("image"), payload.get("command")
                if image not in self.config["test_images"]:
                    raise RunnerError("Test image is not allowlisted.")
                if (
                    not isinstance(command, list)
                    or not 1 <= len(command) <= 100
                    or any(
                        not isinstance(arg, str) or "\x00" in arg or len(arg) > 10000
                        for arg in command
                    )
                    or not command[0]
                ):
                    raise RunnerError("Invalid test command.")
                self.image_info(image)
                extracted = Path(directory) / "source"
                extracted.mkdir()
                archive = payload.get("archive")
                if not isinstance(archive, str) or len(archive) > 140 * 1024 * 1024:
                    raise RunnerError("Invalid test archive.")
                extract_archive(base64.b64decode(archive, validate=True), extracted, cancelled)
                with tarfile.open(source_path, "w") as tar:
                    for path in sorted(extracted.rglob("*")):
                        if cancelled():
                            raise ReviewCancelled()
                        tar.add(path, arcname=str(path.relative_to(extracted)), recursive=False)
                args += [
                    "--entrypoint=/bin/sh",
                    image,
                    "-c",
                    'tar -xf - -C /workspace && exec "$@" </dev/null',
                    "review-tests",
                    *command,
                ]
            else:
                image = self.config["images"].get(operation)
                if not image:
                    raise RunnerError()
                self.image_info(image)
                if operation == "model":
                    volume = self.config["auth_volumes"].get(payload.get("kind"))
                    if not volume:
                        raise RunnerError()
                    target = "/auth"
                else:
                    volume = self.config.get("graph_cache_volume")
                    target = "/graph-cache"
                if volume:
                    docker(["volume", "inspect", volume])
                    args += ["--mount", f"type=volume,source={volume},target={target}"]
                else:
                    args += [
                        "--tmpfs=/graph-cache:rw,nosuid,nodev,size=1g,mode=700,uid=10001,gid=10001"
                    ]
                args.append(image)
                source_path.write_bytes(json.dumps(payload, allow_nan=False).encode())
                source_path.chmod(0o600)
            if cancelled():
                raise ReviewCancelled()
            identifier = docker(args)
            if not IDENTIFIER.fullmatch(identifier):
                raise RunnerError()
            created(identifier)  # Durable before starting the immutable container.
            process = None
            try:
                with source_path.open("rb") as source:
                    process = subprocess.Popen(
                        DOCKER + ["start", "-ai", identifier],
                        stdin=source,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        env=ENV,
                    )
                    total = 0
                    started = time.monotonic()
                    os.set_blocking(process.stdout.fileno(), False)
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        while selector.get_map() or process.poll() is None:
                            if cancelled() or time.monotonic() - started > 3600:
                                raise ReviewCancelled()
                            for key, _ in selector.select(0.25):
                                chunk = os.read(key.fd, 24 * 1024)
                                if not chunk:
                                    selector.unregister(key.fileobj)
                                    continue
                                total += len(chunk)
                                if total > MAX_OUTPUT:
                                    raise RunnerError("Output exceeds limit.")
                                if operation != "test":
                                    emit(chunk)
                    state = json.loads(docker(["inspect", "--format={{json .State}}", identifier]))
                    if state.get("Running") is not False or state.get("Status") != "exited":
                        raise RunnerError()
                    exit_code = state.get("ExitCode", 1)
                    oom = bool(state.get("OOMKilled"))
                    if operation == "test":
                        passed = exit_code == 0 and not oom and not state.get("Error")
                        emit(
                            json.dumps(
                                {
                                    "passed": passed,
                                    "summary": "Isolated local tests passed."
                                    if passed
                                    else "Isolated local tests failed or exhausted resources.",
                                }
                            ).encode()
                        )
                        return {"exit_code": 0, "oom": False}
                    return {"exit_code": exit_code, "oom": oom}
            finally:
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=10)
                    process.stdout.close()
