"""Disposable Docker tests. Docker CLI/daemon remain on the trusted worker side."""

import gzip
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

from core.providers import ReviewCancelled

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_EXTRACTED_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_FILES = 20000
MAX_TAR_BYTES = MAX_EXTRACTED_BYTES + MAX_FILES * 2048
IMAGE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[0-9a-f]{64}\Z")


class RunnerError(Exception):
    def __init__(self, message="Isolated tests unavailable; administrator attention required."):
        super().__init__(message)


class _BoundedTarStream:
    def __init__(self, archive):
        self.source = gzip.GzipFile(fileobj=io.BytesIO(archive))
        self.remaining = MAX_TAR_BYTES

    def read(self, size):
        chunk = self.source.read(min(size, self.remaining + 1))
        self.remaining -= len(chunk)
        if self.remaining < 0:
            raise RunnerError("Repository archive exceeds allowed size.")
        return chunk


def extract_archive(archive, destination, cancelled=lambda: False):
    """Extract only ordinary files/directories, stripping GitHub's one root directory."""
    if not isinstance(archive, bytes) or len(archive) > MAX_ARCHIVE_BYTES:
        raise RunnerError("Repository archive exceeds allowed size.")
    root = None
    total = 0
    seen = set()
    try:
        with tarfile.open(fileobj=_BoundedTarStream(archive), mode="r|") as tar:
            for count, member in enumerate(tar, start=1):
                if cancelled():
                    raise ReviewCancelled()
                path = PurePosixPath(member.name)
                parts = path.parts
                if (
                    count > MAX_FILES
                    or not parts
                    or path.is_absolute()
                    or ".." in parts
                    or "\\" in member.name
                    or any(ord(char) < 32 for char in member.name)
                    or len(member.name) > 1000
                    or len(parts) > 40
                    or not (member.isdir() or member.isfile())
                    or member.issparse()
                ):
                    raise RunnerError("Repository archive contains unsupported entries.")
                root = root or parts[0]
                if parts[0] != root or (len(parts) == 1 and not member.isdir()):
                    raise RunnerError("Repository archive must have one root directory.")
                if len(parts) == 1:
                    continue
                relative = Path(*parts[1:])
                if relative in seen:
                    raise RunnerError("Repository archive contains duplicate entries.")
                seen.add(relative)
                target = destination / relative
                total += member.size
                if member.size < 0 or member.size > MAX_FILE_BYTES or total > MAX_EXTRACTED_BYTES:
                    raise RunnerError("Repository archive exceeds allowed size.")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                if member.isdir():
                    target.mkdir(exist_ok=True, mode=0o755)
                else:
                    with tar.extractfile(member) as source, target.open("xb") as output:
                        remaining = member.size
                        while remaining:
                            if cancelled():
                                raise ReviewCancelled()
                            chunk = source.read(min(remaining, 1024 * 1024))
                            if not chunk:
                                raise RunnerError("Repository archive is incomplete.")
                            output.write(chunk)
                            remaining -= len(chunk)
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)
        if not seen:
            raise RunnerError("Repository archive is empty.")
    except (tarfile.TarError, OSError, EOFError, ValueError):
        raise RunnerError("Repository archive is invalid.") from None


def _docker(args, *, timeout=30):
    # Do not pass worker credentials or connection overrides into the Docker client.
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/nonexistent",
    }
    try:
        result = subprocess.run(
            ["docker", "--host=unix:///var/run/docker.sock", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
            check=False,
        )
        if result.returncode:
            raise RunnerError()
        if len(result.stdout) > 100000:
            raise RunnerError()
        return result.stdout
    except (OSError, subprocess.TimeoutExpired):
        raise RunnerError() from None


def _validate_tests(repository):
    image, command = repository.test_image, repository.test_command
    if not image and not command:
        return {"passed": True, "summary": "Local tests not configured."}
    if not isinstance(image, str) or not IMAGE.fullmatch(image):
        raise RunnerError("Test image must be pinned by sha256 digest.")
    if (
        not isinstance(command, list)
        or not command
        or len(command) > 100
        or any(not isinstance(arg, str) or "\x00" in arg or len(arg) > 10000 for arg in command)
        or not command[0]
    ):
        raise RunnerError("Test command must be a nonempty argument array.")


def run_tests(repository, archive, cancelled, *, on_capacity=None):
    from .capacity import Container, limit, permit

    if cancelled():
        raise ReviewCancelled()
    if not repository.test_image and not repository.test_command:
        return {"passed": True, "summary": "Local tests not configured."}
    _validate_tests(repository)
    with permit("tests", limit("TEST_CONCURRENCY", 1), cancelled, on_wait=on_capacity) as slot:
        with Container(slot, _docker) as container:
            return _run_tests(repository, archive, cancelled, container=container)


def _run_tests(repository, archive, cancelled, *, container):
    if cancelled():
        raise ReviewCancelled()
    image, command = repository.test_image, repository.test_command
    name = container.slot.name
    # stdin transfers through the daemon API: no shared worker/host filesystem paths.
    with tempfile.TemporaryDirectory(prefix="pr-review-") as directory:
        source = Path(directory) / "input"
        source.mkdir(mode=0o755)
        extract_archive(archive, source, cancelled)
        try:
            metadata = json.loads(_docker(["image", "inspect", image]))
            if metadata[0]["Config"].get("Volumes"):
                raise RunnerError("Test images must not declare volumes.")
        except (ValueError, KeyError, IndexError, TypeError):
            raise RunnerError() from None
        if cancelled():
            raise ReviewCancelled()
        packed = Path(directory) / "source.tar"
        with tarfile.open(packed, "w") as tar:
            for path in sorted(source.rglob("*")):
                if cancelled():
                    raise ReviewCancelled()
                tar.add(path, arcname=str(path.relative_to(source)), recursive=False)
        process = None
        try:
            name = container.create(
                [
                    "create",
                    "--name",
                    name,
                    "--pull=never",
                    "--label=auto-reviewer.test=true",
                    "--network=none",
                    "--user=65534:65534",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--pids-limit=128",
                    "--memory=1g",
                    "--memory-swap=1g",
                    "--cpus=1",
                    "--ulimit=nofile=1024:1024",
                    "--log-driver=none",
                    "--ipc=private",
                    "--workdir=/workspace",
                    "--env=HOME=/tmp",
                    "--tmpfs=/tmp:rw,nosuid,nodev,size=128m,mode=1777",
                    "--tmpfs=/workspace:rw,nosuid,nodev,size=512m,mode=700,uid=65534,gid=65534",
                    "--interactive",
                    "--entrypoint=/bin/sh",
                    image,
                    "-c",
                    'tar -xf - -C /workspace && exec "$@" </dev/null',
                    "review-tests",
                    *command,
                ]
            )
            if cancelled():
                raise ReviewCancelled()
            with packed.open("rb") as source_tar:
                process = subprocess.Popen(
                    [
                        "docker",
                        "--host=unix:///var/run/docker.sock",
                        "start",
                        "-ai",
                        name,
                    ],
                    stdin=source_tar,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={
                        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                        "HOME": "/nonexistent",
                    },
                )
                while True:
                    if cancelled():
                        raise ReviewCancelled()
                    if process.poll() is not None:
                        break
                    time.sleep(1)
            try:
                state = json.loads(_docker(["inspect", "--format={{json .State}}", name]))
                if state["Running"] is not False:
                    raise RunnerError()
                passed = (
                    state["ExitCode"] == 0 and not state.get("OOMKilled") and not state.get("Error")
                )
                return {
                    "passed": passed,
                    "summary": "Isolated local tests passed."
                    if passed
                    else "Isolated local tests failed or exhausted resources.",
                }
            except (ValueError, KeyError, TypeError):
                raise RunnerError() from None
        except OSError:
            raise RunnerError() from None
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)


def cleanup_orphans():
    """Concurrent workers must use exact slot cleanup, never global label cleanup."""
    from .capacity import CapacityError

    raise CapacityError()
