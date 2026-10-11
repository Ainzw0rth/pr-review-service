"""Optional graph evidence selection; the parser has no credentials or network."""

import base64
import io
import json
import os
import re
import subprocess
import tempfile
import time
import uuid

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


def execute(request, cancelled=lambda: False):
    from .runner import _docker

    image = settings.GRAPH_RUNNER_IMAGE
    volume = settings.GRAPH_CACHE_VOLUME
    if not re.fullmatch(
        r"(?:sha256:[a-f0-9]{64}|[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[a-f0-9]{64})", image
    ):
        raise IntegrationError(FAILURE)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", volume):
        raise IntegrationError(FAILURE)
    name = "pr-review-graph-" + uuid.uuid4().hex
    args = [
        "docker",
        "--host=unix:///var/run/docker.sock",
        "run",
        "--rm",
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
            process = subprocess.Popen(
                args,
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
                try:
                    _docker(["rm", "-f", name])
                finally:
                    process.kill()
                    process.wait()


def collect(
    github,
    repository,
    head_sha,
    base_sha,
    files,
    cancelled=lambda: False,
    progress=lambda *a, **k: None,
):
    if repository not in settings.GRAPH_REPOSITORIES:
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
        {"repository": repository, "snapshots": snapshots, "changed": changed}, cancelled
    )
    return validate(result, repository, head_sha)
