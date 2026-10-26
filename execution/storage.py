"""Private durable worker state and bounded repository archive extraction."""

import gzip
import io
import json
import os
import re
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


class ReviewCancelled(Exception):
    pass


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


def atomic_json(path, value):
    """Publish private JSON only after file contents and directory entry are durable."""
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"), allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
