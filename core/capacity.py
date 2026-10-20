"""Same-host admission. Stable flock inodes; never remove files while deployed.

All workers share DATA_DIR and SECRET_KEY. Drain before changing pool limits or
migrating legacy containers. Generic flock permits are bounded, not FIFO; CLI
admission adds explicit priority/FIFO ordering in cli_queue.
"""

import fcntl
import hashlib
import hmac
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from django.conf import settings


class CapacityError(Exception):
    def __init__(self):
        super().__init__("Resource cleanup unconfirmed; administrator attention required.")


class CapacityBusy(Exception):
    pass


def digest(value):
    return hmac.new(settings.SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


def cli_scope(kind):
    volume = (
        settings.CLI_CODEX_AUTH_VOLUME if kind == "codex_cli" else settings.CLI_CLAUDE_AUTH_VOLUME
    )
    if not isinstance(volume, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}", volume):
        raise CapacityError()
    return volume, "cli-volume:" + volume


def api_scope(provider, credential):
    groups = getattr(settings, "API_ACCOUNT_GROUPS", {})
    group = groups.get(str(getattr(provider, "credential_id", "")))
    # Fingerprints are keyed and private. Duplicate credentials share the default group.
    identity = "group:" + str(group) if group else "key:" + credential
    return "api:" + provider.kind + ":" + digest(identity)


def limit(name, default):
    value = getattr(settings, name, default)
    if type(value) is not int or not 1 <= value <= 32:
        raise CapacityError()
    return value


# Supervisor refreshes this process-local snapshot from ServiceSettings each loop.
# Provider threads remain DB-free; pending admissions read the latest snapshot.
_cli_concurrency = None


def cli_limit():
    return _cli_concurrency if _cli_concurrency is not None else limit("CLI_ACCOUNT_CONCURRENCY", 1)


def set_cli_limit(value):
    global _cli_concurrency
    if value is not None and (type(value) is not int or not 1 <= value <= 32):
        raise CapacityError()
    previous, _cli_concurrency = _cli_concurrency, value
    return previous


@dataclass(frozen=True)
class Slot:
    scope: str
    index: int
    generation: str
    path: object
    admission_limit: int | None = None

    @property
    def namespace(self):
        return digest("capacity-deployment")[:24]

    @property
    def name(self):
        return f"pr-cap-{self.namespace}-{self.scope}-{self.index}"

    @property
    def labels(self):
        from .claims import current_claim_token, current_owner_token

        return {
            "auto-reviewer.namespace": self.namespace,
            "auto-reviewer.scope": self.scope,
            "auto-reviewer.slot": str(self.index),
            "auto-reviewer.generation": self.generation,
            "auto-reviewer.claim": current_claim_token(),
            "auto-reviewer.owner": current_owner_token(),
        }


@contextmanager
def _permit(scope, indices, cancelled, on_wait=None, wait=True):
    from .providers import ReviewCancelled

    waiting = False
    held = None
    try:
        directory = settings.DATA_DIR / "capacity"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        while held is None:
            if cancelled():
                raise ReviewCancelled()
            for index in indices:
                path = directory / f"{scope}-{index}.lock"
                fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if os.fstat(fd).st_ino != path.stat().st_ino:
                        raise CapacityError()
                    held = fd
                    break
                except BlockingIOError:
                    os.close(fd)
                except BaseException:
                    os.close(fd)
                    raise
            if held is not None:
                if cancelled():
                    raise ReviewCancelled()
                if waiting and on_wait:
                    on_wait(False)
                waiting = False
                break
            if not wait:
                raise CapacityBusy()
            if not waiting:
                waiting = True
                if on_wait:
                    on_wait(True)
            time.sleep(0.1)
        yield Slot(scope, index, uuid.uuid4().hex, path)
    except OSError:
        raise CapacityError() from None
    finally:
        if held is not None:
            os.close(held)
        if waiting and on_wait:
            on_wait(False)


def permit(scope, limit, cancelled, *, on_wait=None, wait=True):
    if type(limit) is not int or not 1 <= limit <= 32:
        raise CapacityError()
    return _permit(digest(scope), range(limit), cancelled, on_wait, wait)


class ActiveBudget:
    def __init__(self, seconds, *, clock=None):
        self.seconds = seconds
        self.clock = clock or time.monotonic
        self.elapsed = 0
        self.started = None
        self.lock = threading.Lock()

    def check(self):
        from .providers import ProviderError

        with self.lock:
            elapsed = self.elapsed + (
                self.clock() - self.started if self.started is not None else 0
            )
        if elapsed >= self.seconds:
            raise ProviderError("invalid", detail="review_timeout")

    @contextmanager
    def active(self):
        with self.lock:
            if self.started is not None:
                raise CapacityError()
            self.started = self.clock()
        try:
            self.check()
            yield
            self.check()
        finally:
            with self.lock:
                self.elapsed += self.clock() - self.started
                self.started = None


class Container:
    """Use only while holding slot. Reconcile exact name; operate only on immutable IDs."""

    def __init__(self, slot, docker, *, volume=None):
        self.slot, self.docker, self.volume = slot, docker, volume
        self.identifier = None

    def _existing(self):
        import json

        try:
            ids = self.docker(
                ["ps", "-aq", "--no-trunc", f"--filter=name=^/{self.slot.name}$"]
            ).split()
            if len(ids) > 1 or any(not re.fullmatch("[a-f0-9]{64}", item) for item in ids):
                raise CapacityError()
            if not ids:
                return None
            data = json.loads(self.docker(["inspect", ids[0]]))[0]
            labels = data["Config"]["Labels"] or {}
            expected = {
                "auto-reviewer.namespace": self.slot.namespace,
                "auto-reviewer.scope": self.slot.scope,
                "auto-reviewer.slot": str(self.slot.index),
            }
            if (
                data["Id"] != ids[0]
                or data["Name"] != "/" + self.slot.name
                or any(labels.get(k) != v for k, v in expected.items())
            ):
                raise CapacityError()
            if not re.fullmatch("[a-f0-9]{32}", labels.get("auto-reviewer.generation", "")):
                raise CapacityError()
            mounts = [m for m in data.get("Mounts", []) if m.get("Destination") == "/auth"]
            if self.volume and (
                len(mounts) != 1
                or mounts[0].get("Type") != "volume"
                or mounts[0].get("Name") != self.volume
            ):
                raise CapacityError()
            if mounts and digest("cli-volume:" + mounts[0].get("Name", "")) != self.slot.scope:
                raise CapacityError()
            return ids[0]
        except Exception:
            raise CapacityError() from None

    def cleanup(self):
        try:
            identifier = self._existing()
            if identifier:
                if self.identifier and self.identifier != identifier:
                    raise CapacityError()
                self.docker(["rm", "-f", "-v", identifier], timeout=10)
                if self._existing() is not None:
                    raise CapacityError()
            # Name disappearance alone is insufficient if an operator renamed it.
            removed_id = self.identifier or identifier
            if (
                removed_id
                and self.docker(["ps", "-aq", "--no-trunc", f"--filter=id={removed_id}"]).strip()
            ):
                raise CapacityError()
        except Exception:
            raise CapacityError() from None

    def create(self, args):
        labels = [f"--label={key}={value}" for key, value in self.slot.labels.items()]
        try:
            identifier = self.docker([args[0], *labels, *args[1:]]).strip()
            if not re.fullmatch("[a-f0-9]{64}", identifier):
                raise CapacityError()
            self.identifier = identifier
            return identifier
        except Exception:
            raise CapacityError() from None

    def __enter__(self):
        self.cleanup()
        return self

    def __exit__(self, *exc):
        self.cleanup()


def recover_resources(owner):
    """Caller proved execution quiescence; still acquire each exact resource slot."""
    import json

    from .runner import _docker

    label = "claim" if hasattr(owner, "token") else "owner"
    token = str(owner.token if label == "claim" else owner.pk)
    try:
        ids = _docker(
            [
                "ps",
                "-aq",
                "--no-trunc",
                f"--filter=label=auto-reviewer.namespace={digest('capacity-deployment')[:24]}",
                f"--filter=label=auto-reviewer.{label}={token}",
            ]
        ).split()
        for identifier in ids:
            if not re.fullmatch("[a-f0-9]{64}", identifier):
                raise CapacityError()
            data = json.loads(_docker(["inspect", identifier]))[0]
            labels = data["Config"]["Labels"]
            scope, index = labels["auto-reviewer.scope"], labels["auto-reviewer.slot"]
            if (
                labels.get(f"auto-reviewer.{label}") != token
                or not re.fullmatch("[a-f0-9]{64}", scope)
                or not re.fullmatch(r"\d{1,2}", index)
                or int(index) >= 32
            ):
                raise CapacityError()
            with _permit(scope, [int(index)], lambda: False, wait=False) as slot:
                container = Container(slot, _docker)
                container.identifier = identifier
                container.cleanup()
    except Exception:
        raise CapacityError() from None
