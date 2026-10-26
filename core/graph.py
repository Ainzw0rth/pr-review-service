"""Optional graph evidence selection; the parser has no credentials or network."""

import base64
import io
import json
import os
import re
import subprocess
import tempfile
import time

from django.conf import settings

from .github import IntegrationError
from .providers import ReviewCancelled

FAILURE = "Code graph unavailable or incomplete; no model review was started."
VERSION = "crg-2.3.9-adapter-1"


def validate(data, repository, sha):
    from .context import _path

    try:
        if (
            not isinstance(data, dict)
            or data.get("version") != VERSION
            or data.get("repository") != repository
            or data.get("head_sha") != sha
            or not isinstance(data.get("paths"), list)
            or data.get("depth") != 2
            or not isinstance(data.get("snapshots"), list)
        ):
            raise ValueError()
        for path in data["paths"]:
            _path(path)
        return data
    except (ValueError, TypeError, IntegrationError):
        raise IntegrationError(FAILURE) from None


def execute(request, cancelled=lambda: False, *, on_capacity=None):
    from .capacity import Container, limit, permit
    from .runner import _docker

    with permit("graph", limit("GRAPH_CONCURRENCY", 1), cancelled, on_wait=on_capacity) as slot:
        with Container(slot, _docker) as container:
            return _execute(request, cancelled, container=container)


def _execute(request, cancelled, *, container):
    from .execution_dispatch import try_run

    output = bytearray()

    def receive(chunk):
        if len(output) + len(chunk) > 32 * 1024 * 1024:
            raise IntegrationError(FAILURE)
        output.extend(chunk)

    terminal = try_run("graph", request, container.slot, cancelled, receive)
    if terminal is not None:
        if cancelled():
            raise ReviewCancelled()
        if terminal.get("error") or terminal["exit_code"] or terminal["oom"]:
            raise IntegrationError(FAILURE)
        try:
            return json.loads(output)
        except (ValueError, UnicodeError, RecursionError):
            raise IntegrationError(FAILURE) from None

    image = settings.GRAPH_RUNNER_IMAGE
    volume = settings.GRAPH_CACHE_VOLUME
    if not re.fullmatch(
        r"(?:sha256:[a-f0-9]{64}|[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[a-f0-9]{64})", image
    ):
        raise IntegrationError(FAILURE)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", volume):
        raise IntegrationError(FAILURE)
    name = container.slot.name
    args = [
        "create",
        "--pull=never",
        "-i",
        "--name",
        name,
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=10001:10001",
        "--cpus=2",
        "--memory=2g",
        "--pids-limit=128",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=128m",
        "--mount",
        f"type=volume,src={volume},dst=/graph-cache",
        image,
    ]
    process = None
    with (
        tempfile.TemporaryFile() as source,
        tempfile.TemporaryFile() as output,
        tempfile.TemporaryFile() as error,
    ):
        text = io.TextIOWrapper(source, encoding="utf-8")
        json.dump(request, text)
        text.flush()
        text.detach()
        source.seek(0)
        try:
            if cancelled():
                raise ReviewCancelled()
            identifier = container.create(args)
            process = subprocess.Popen(
                ["docker", "--host=unix:///var/run/docker.sock", "start", "-ai", identifier],
                stdin=source,
                stdout=output,
                stderr=error,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            )
            started = time.monotonic()
            while process.poll() is None:
                if cancelled():
                    raise ReviewCancelled()
                if time.monotonic() - started > settings.GRAPH_TIMEOUT_SECONDS:
                    raise IntegrationError(FAILURE)
                if output.tell() > 32 * 1024 * 1024 or error.tell() > 16 * 1024 * 1024:
                    raise IntegrationError(FAILURE)
                time.sleep(0.25)
            if process.returncode:
                raise IntegrationError(FAILURE)
            output.seek(0)
            return json.loads(output.read(32 * 1024 * 1024))
        except (OSError, ValueError):
            raise IntegrationError(FAILURE) from None
        finally:
            if process and process.poll() is None:
                process.kill()
                process.wait()


def enabled(repository):
    """Explicit per-repository override; legacy name-only callers keep env behavior."""
    if not isinstance(repository, str):
        override = getattr(repository, "graph_enabled", None)
        if type(override) is bool:
            return override
        repository = repository.full_name
    return repository in settings.GRAPH_REPOSITORIES


def collect(
    github,
    repository,
    head_sha,
    base_sha,
    files,
    cancelled=lambda: False,
    progress=lambda *a, **k: None,
):
    configured = getattr(github, "repository", None)
    target = configured if getattr(configured, "full_name", None) == repository else repository
    if not enabled(target):
        return None
    progress("graph_started")
    snapshots = []
    for sha in dict.fromkeys([base_sha, head_sha]):
        if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise IntegrationError(FAILURE)
        if cancelled():
            raise ReviewCancelled()
        snapshots.append({"sha": sha, "archive": base64.b64encode(github.archive(sha)).decode()})
    changed = sorted(
        {
            path
            for file in files
            for path in (file["filename"], file.get("previous_filename"))
            if path
        }
    )
    result = execute(
        {"repository": repository, "snapshots": snapshots, "changed": changed},
        cancelled,
        on_capacity=lambda waiting: progress("graph_waiting" if waiting else "graph_resumed"),
    )
    result = validate(result, repository, head_sha)
    for summary in result["snapshots"]:
        if not isinstance(summary, dict) or summary.get("mode") not in {"full", "incremental"}:
            raise IntegrationError(FAILURE)
        parsed = summary.get("files_parsed")
        indexed = summary.get("indexed_files")
        if any(type(n) is not int or not 0 <= n <= 1_000_000 for n in (parsed, indexed)):
            raise IntegrationError(FAILURE)
        progress("graph_" + summary["mode"], count=parsed)
    if result["snapshots"]:
        progress("graph_indexed", count=result["snapshots"][-1]["indexed_files"])
    progress("graph_impacted", count=len(result["paths"]))
    return result
