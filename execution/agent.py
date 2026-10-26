"""Outbound-only, stdlib remote executor: python -m execution.agent --config PATH."""

import argparse
import base64
import fcntl
import hashlib
import http.client
import json
import os
import shutil
import signal
import ssl
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .docker_runner import IMAGE, VOLUME, DockerRunner
from .storage import atomic_json

MAX_INPUT = 300 * 1024 * 1024  # Two bounded graph archives plus envelope.
MAX_REPLY = MAX_INPUT + 1024
MIN_DISK = 2 * 1024**3
SOFTWARE_VERSION = "distributed-worker-v1"


def resource_metrics(root):
    """Host memory from Linux procfs; absent/unreadable values stay unknown."""
    metrics = {"disk_free": shutil.disk_usage(root).free}
    try:
        raw = Path("/proc/meminfo").read_text()[:65536]
        memory = {}
        for line in raw.splitlines():
            fields = line.split()
            if (
                len(fields) == 3
                and fields[2] == "kB"
                and fields[0] in {"MemTotal:", "MemAvailable:"}
            ):
                value = int(fields[1]) * 1024
                if not 0 <= value <= 2**63 - 1:
                    raise ValueError()
                memory[fields[0][:-1]] = value
        if "MemTotal" in memory:
            metrics["memory_total"] = memory["MemTotal"]
            if "MemAvailable" in memory and memory["MemAvailable"] <= memory["MemTotal"]:
                metrics["memory_used"] = memory["MemTotal"] - memory["MemAvailable"]
    except (OSError, ValueError):
        pass
    return metrics


class TransportError(Exception):
    pass


class StaleAssignment(TransportError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def load_config(path):
    path = Path(path)
    if path.stat().st_mode & 0o077:
        raise ValueError("Worker configuration must be private (0600).")
    if path.stat().st_size > 65536:
        raise ValueError("Worker configuration too large.")
    config = json.loads(path.read_bytes())
    if not isinstance(config, dict):
        raise ValueError("Invalid worker configuration.")
    config.setdefault("state_dir", "/state")
    config.setdefault("slots", 1)
    config.setdefault("auth_volumes", {})
    config.setdefault("test_images", [])
    origin = urlsplit(config["coordinator"])
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.query
        or origin.fragment
        or origin.path not in ("", "/")
    ):
        raise ValueError("Coordinator must be an HTTPS origin.")
    origin.port  # Validate malformed port before any network access.
    if (
        not isinstance(config["token"], str)
        or not 16 <= len(config["token"]) <= 4096
        or any(ord(c) < 33 or ord(c) > 126 for c in config["token"])
    ):
        raise ValueError("Invalid worker credential.")
    if str(uuid.UUID(config["worker_id"])) != config["worker_id"]:
        raise ValueError("Invalid worker ID.")
    if type(config["slots"]) is not int or config["slots"] != 1:
        raise ValueError("This worker supports exactly one slot.")
    if not isinstance(config["state_dir"], str) or not Path(config["state_dir"]).is_absolute():
        raise ValueError("State directory must be absolute.")
    images = config.get("images")
    if (
        not isinstance(images, dict)
        or set(images) - {"model", "graph"}
        or any(not isinstance(v, str) or not IMAGE.fullmatch(v) for v in images.values())
    ):
        raise ValueError("Runner images must be immutable digests.")
    volumes = config["auth_volumes"]
    if (
        not isinstance(volumes, dict)
        or set(volumes) - {"codex_cli", "claude_cli"}
        or any(not isinstance(v, str) or not VOLUME.fullmatch(v) for v in volumes.values())
    ):
        raise ValueError("Invalid authentication volumes.")
    if config.get("graph_cache_volume") is not None and (
        not isinstance(config["graph_cache_volume"], str)
        or not VOLUME.fullmatch(config["graph_cache_volume"])
    ):
        raise ValueError("Invalid graph cache volume.")
    tests = config["test_images"]
    if (
        not isinstance(tests, list)
        or len(tests) > 100
        or any(not isinstance(v, str) or not IMAGE.fullmatch(v) for v in tests)
    ):
        raise ValueError("Test images must be immutable digests.")
    return config


class Client:
    def __init__(self, config):
        self.origin = urlsplit(config["coordinator"])
        self.token = config["token"]
        self.context = ssl.create_default_context()

    def post(self, endpoint, body):
        connection = http.client.HTTPSConnection(
            self.origin.hostname, self.origin.port or 443, timeout=10, context=self.context
        )
        deadline = time.monotonic() + 10

        def set_timeout():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportError()
            if connection.sock:
                connection.sock.settimeout(remaining)

        try:
            connection.connect()
            set_timeout()
            connection.request(
                "POST",
                "/api/workers/v1/" + endpoint + "/",
                canonical(body),
                {
                    "Authorization": "Bearer " + self.token,
                    "Content-Type": "application/json",
                    "User-Agent": "auto-reviewer-worker/1",
                },
            )
            set_timeout()
            response = connection.getresponse()
            if response.status == 409:
                raise StaleAssignment()
            if response.status != 200:  # Never follow redirects or forward the credential.
                raise TransportError()
            limit = MAX_REPLY if endpoint == "input" else 65536
            length = response.getheader("Content-Length")
            if length and (not length.isdigit() or int(length) > limit):
                raise TransportError()
            chunks = []
            received = 0
            while True:
                set_timeout()
                chunk = response.read1(min(65536, limit + 1 - received))
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
                if received > limit:
                    raise TransportError()
            value = b"".join(chunks)
            decoded = json.loads(value)
            if not isinstance(decoded, dict):
                raise TransportError()
            return decoded
        except (OSError, ValueError, http.client.HTTPException):
            raise TransportError() from None
        finally:
            connection.close()


class Agent:
    def __init__(self, config, client=None, runner=None):
        self.config = config
        self.root = Path(config["state_dir"])
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.lock = (self.root / "agent.lock").open("a")
        os.chmod(self.lock.name, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise
        self.client = client or Client(config)
        self.runner = runner or DockerRunner(config)
        self.incarnation = uuid.uuid4().hex
        self.shutdown = threading.Event()
        self.cancel = threading.Event()
        self.job = None
        self.mutex = threading.RLock()
        self.capabilities = {}
        self.sequence = -1
        self.last_renewal = 0
        self.lease_finished = threading.Event()
        self.renewal_thread = None
        self.upload_finished = threading.Event()
        self.upload_thread = None
        self.spool = self.root / "events"
        self.spool.mkdir(mode=0o700, exist_ok=True)

    def persist(self):
        with self.mutex:
            atomic_json(self.root / "assignment.json", self.job)

    def credentials(self):
        return {key: self.job[key] for key in ("id", "token", "incarnation")}

    def heartbeat(self):
        while not self.shutdown.is_set():
            try:
                metrics = resource_metrics(self.root)
                capabilities = [name for name in ("test", "graph") if name in self.capabilities]
                auth = [
                    value["authenticated"]
                    for name, value in self.capabilities.items()
                    if name.endswith("_cli")
                ]
                if auth and all(auth):
                    capabilities.append("model")
                self.client.post(
                    "heartbeat",
                    {
                        "version": 1,
                        "incarnation": self.incarnation,
                        "slots": 1,
                        "active": int(self.job is not None),
                        "capabilities": capabilities,
                        "authenticated": bool(capabilities),
                        "metrics": metrics,
                        "software_version": SOFTWARE_VERSION,
                    },
                )
            except (TransportError, OSError):
                pass
            self.shutdown.wait(15)

    def start_renewal(self):
        if self.renewal_thread and self.renewal_thread.is_alive():
            return
        self.lease_finished.clear()
        self.last_renewal = time.monotonic()
        self.renewal_thread = threading.Thread(target=self.renew, daemon=True)
        self.renewal_thread.start()

    def stop_renewal(self):
        self.lease_finished.set()
        if self.renewal_thread:
            self.renewal_thread.join(timeout=12)

    def renew(self):
        while not self.lease_finished.wait(1):
            if time.monotonic() - self.last_renewal >= 15:
                try:
                    if self.client.post("renew", self.credentials()).get("continue") is not True:
                        self.cancel.set()
                        return
                    self.last_renewal = time.monotonic()
                except StaleAssignment:
                    self.cancel.set()
                    return
                except TransportError:
                    pass
            if time.monotonic() - self.last_renewal >= 30:
                self.cancel.set()
                return

    def cancelled(self):
        return (
            self.shutdown.is_set()
            or self.cancel.is_set()
            or time.monotonic() - self.last_renewal >= 30
        )

    def emit(self, chunk):
        if not isinstance(chunk, bytes) or len(chunk) > 24 * 1024:
            raise ValueError("Invalid output chunk")
        if shutil.disk_usage(self.root).free < 64 * 1024 * 1024:
            raise OSError("Output storage exhausted")
        with self.mutex:
            self.sequence += 1
            atomic_json(
                self.spool / f"{self.sequence:010d}.json",
                {
                    "sequence": self.sequence,
                    "data": base64.b64encode(chunk).decode(),
                },
            )

    def upload_once(self, stop_event=None):
        for path in sorted(self.spool.glob("*.json")):
            if stop_event and stop_event.is_set():
                return
            event = json.loads(path.read_bytes())
            reply = self.client.post("event", {**self.credentials(), **event})
            if reply.get("sequence", reply.get("ack")) != event["sequence"]:
                raise TransportError()
            path.unlink()  # Lost ACK retains identical persisted event and sequence.

    def upload(self):
        while not self.upload_finished.wait(0.25):
            try:
                self.upload_once(self.upload_finished)
            except StaleAssignment:
                self.cancel.set()
                return
            except TransportError:
                self.upload_finished.wait(2)
            except (OSError, ValueError):
                self.cancel.set()
                return

    def cleanup(self):
        self.runner.cleanup()
        self.job["cleaned"] = True
        self.persist()

    def deliver(self):
        """Completion only after durable events and proven container removal."""
        if not self.job.get("cleaned"):
            self.cleanup()
        if self.upload_thread and self.upload_thread.is_alive():
            raise TransportError()  # One output sender at a time, including slow request teardown.
        try:
            self.upload_once()
        except StaleAssignment:
            # Fenced output is never usable, but cleanup still needs acknowledgement.
            pass
        result = self.client.post(
            "complete", {**self.credentials(), "result": self.job["result"], "cleaned": True}
        )
        if type(result.get("accepted")) is not bool:
            raise TransportError()
        self.stop_renewal()  # Never clear credentials under a live renewal request.
        for path in self.spool.glob("*.json"):
            path.unlink()
        self.job = None
        self.persist()

    def recover(self):
        path = self.root / "assignment.json"
        if path.exists():
            self.job = json.loads(path.read_bytes())
        # Includes create-before-state crash windows; never trust name or prior PID.
        self.runner.cleanup()
        for directory in self.root.glob("job-*"):
            if directory.is_dir() and not directory.is_symlink():
                shutil.rmtree(directory)
        if self.job:
            self.job.setdefault(
                "result", {"exit_code": 1, "oom": False, "error": "worker_restarted"}
            )
            self.job["cleaned"] = True
            self.persist()
            self.start_renewal()
            self.deliver()
        else:
            for event in self.spool.glob("*.json"):
                event.unlink()

    def run_assignment(self, assignment):
        required = {"id", "token", "operation", "input_hash", "input_bytes"}
        if not isinstance(assignment, dict) or not required <= assignment.keys():
            raise TransportError()
        if (
            not isinstance(assignment["id"], str)
            or str(uuid.UUID(assignment["id"])) != assignment["id"]
            or not isinstance(assignment["token"], str)
            or not 1 <= len(assignment["token"]) <= 4096
            or assignment["operation"] not in {"model", "test", "graph"}
            or type(assignment["input_bytes"]) is not int
            or not 0 <= assignment["input_bytes"] <= MAX_INPUT
        ):
            raise TransportError()
        self.job = {**assignment, "incarnation": self.incarnation}
        self.sequence = -1
        self.persist()  # Crash anywhere below must reconcile, never re-execute.
        self.cancel.clear()
        self.upload_finished.clear()
        self.start_renewal()
        uploader = threading.Thread(target=self.upload, daemon=True)
        self.upload_thread = uploader
        uploader.start()
        try:
            payload = self.client.post("input", self.credentials()).get("payload")
            encoded = canonical(payload)
            if (
                not isinstance(payload, dict)
                or len(encoded) != assignment["input_bytes"]
                or hashlib.sha256(encoded).hexdigest() != assignment["input_hash"]
            ):
                raise ValueError("Input integrity mismatch")

            def created(identifier):
                self.job["container_id"] = identifier
                self.persist()

            self.job["result"] = self.runner.execute(
                assignment["operation"], payload, self.emit, self.cancelled, created
            )
        except Exception:
            # Deliberately don't serialize exceptions: requests may contain secrets/evidence.
            self.job["result"] = {
                "exit_code": 1,
                "oom": False,
                "error": "cancelled" if self.cancelled() else "execution_failed",
            }
        finally:
            self.upload_finished.set()
            uploader.join(timeout=12)
            self.persist()
        self.cleanup()
        self.deliver()

    def run(self):
        delay = 1
        # Do not advertise readiness while old children or durable completion remain.
        while not self.shutdown.is_set():
            try:
                self.recover()
                break
            except Exception:
                self.shutdown.wait(delay)
                delay = min(delay * 2, 30)
        if self.shutdown.is_set():
            self.stop_renewal()
            return
        self.capabilities = self.runner.capabilities()
        self.runner.cleanup()  # Also reconcile a timed-out capability probe creation.
        last_capability_check = time.monotonic()
        heartbeat = threading.Thread(target=self.heartbeat, daemon=True)
        heartbeat.start()
        try:
            while not self.shutdown.is_set():
                try:
                    if self.job:
                        self.deliver()
                    elif shutil.disk_usage(self.root).free >= MIN_DISK:
                        if time.monotonic() - last_capability_check >= 60:
                            self.capabilities = self.runner.capabilities()
                            self.runner.cleanup()
                            last_capability_check = time.monotonic()
                        assignment = self.client.post(
                            "claim", {"incarnation": self.incarnation}
                        ).get("assignment")
                        if assignment:
                            self.run_assignment(assignment)
                    delay = 2
                except Exception:
                    delay = min(delay * 2, 30)
                self.shutdown.wait(delay)
        finally:
            self.shutdown.set()
            self.stop_renewal()
            heartbeat.join(timeout=12)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.check_config:
            print("Worker configuration valid.")
            return 0
        agent = Agent(config)
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: agent.shutdown.set())
        agent.run()
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print(
            "Worker startup failed; check private configuration and state lock.",
            file=__import__("sys").stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
