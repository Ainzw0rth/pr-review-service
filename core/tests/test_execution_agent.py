"""Agent fault injection: no real Docker, accounts or network."""

import hashlib
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from execution.agent import Agent, TransportError, canonical, load_config
from execution.docker_runner import DockerRunner
from execution.storage import atomic_json


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.events = []
        self.completions = []
        self.fail_ack = False
        self.fail_complete = False
        self.renewals = 0

    def post(self, endpoint, body):
        if endpoint == "input":
            return {"payload": self.payload}
        if endpoint == "renew":
            self.renewals += 1
            return {"continue": True}
        if endpoint == "event":
            self.events.append(body.copy())
            if self.fail_ack:
                self.fail_ack = False
                raise TransportError()
            return {"sequence": body["sequence"]}
        if endpoint == "complete":
            self.completions.append(body.copy())
            if self.fail_complete:
                raise TransportError()
            return {"accepted": True}
        return {}


class FakeRunner:
    executions = 0
    cleanup_failed = False

    def cleanup(self):
        if self.cleanup_failed:
            raise RuntimeError("cleanup uncertain")

    def execute(self, operation, payload, emit, cancelled, created):
        self.executions += 1
        created("a" * 64)
        emit(b'{"hello":"world"}\n')
        return {"exit_code": 0, "oom": False}


class ExecutionAgentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = {
            "coordinator": "https://coordinator.example",
            "token": "t" * 32,
            "worker_id": str(uuid.uuid4()),
            "state_dir": self.directory.name,
            "slots": 1,
            "images": {},
            "auth_volumes": {},
            "test_images": [],
        }
        self.payload = {"kind": "codex_cli", "context": {"content": "evidence"}}
        encoded = canonical(self.payload)
        self.assignment = {
            "id": str(uuid.uuid4()),
            "token": "assignment-token",
            "operation": "model",
            "input_hash": hashlib.sha256(encoded).hexdigest(),
            "input_bytes": len(encoded),
        }
        self.client = FakeClient(self.payload)
        self.runner = FakeRunner()
        self.agent = Agent(self.config, self.client, self.runner)
        self.addCleanup(self.agent.lock.close)
        self.addCleanup(self.agent.stop_renewal)

    def test_lost_event_ack_replays_same_sequence_and_bytes(self):
        self.agent.job = {**self.assignment, "incarnation": self.agent.incarnation}
        self.agent.emit(b"output")
        self.client.fail_ack = True
        with self.assertRaises(TransportError):
            self.agent.upload_once()
        self.agent.upload_once()
        self.assertEqual(self.client.events[0], self.client.events[1])
        self.assertEqual(self.client.events[0]["sequence"], 0)
        self.assertEqual(list(self.agent.spool.glob("*.json")), [])

    def test_lost_completion_ack_recovery_does_not_execute_again(self):
        self.client.fail_complete = True
        with self.assertRaises(TransportError):
            self.agent.run_assignment(self.assignment)
        original = self.client.completions[0]
        self.agent.stop_renewal()
        self.agent.lock.close()
        restarted = Agent(self.config, self.client, self.runner)
        self.addCleanup(restarted.lock.close)
        self.addCleanup(restarted.stop_renewal)
        self.client.fail_complete = False
        restarted.recover()
        self.assertEqual(self.runner.executions, 1)
        self.assertEqual(self.client.completions[-1], original)
        self.assertIsNone(restarted.job)

    def test_pending_completion_keeps_lease_renewing_until_ack(self):
        import time

        self.client.fail_complete = True
        with self.assertRaises(TransportError):
            self.agent.run_assignment(self.assignment)
        self.assertTrue(self.agent.job["cleaned"])
        self.assertTrue(self.agent.renewal_thread.is_alive())
        self.agent.last_renewal = time.monotonic() - 16
        deadline = time.monotonic() + 3
        while not self.client.renewals and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertGreater(self.client.renewals, 0)
        self.client.fail_complete = False
        self.agent.deliver()
        self.assertFalse(self.agent.renewal_thread.is_alive())
        self.assertIsNone(self.agent.job)
        self.assertEqual(self.runner.executions, 1)

    def test_restart_mid_execution_cleans_and_reports_failure(self):
        atomic_json(
            Path(self.directory.name) / "assignment.json",
            {**self.assignment, "incarnation": "previous-incarnation"},
        )
        self.agent.recover()
        self.assertEqual(self.runner.executions, 0)
        self.assertEqual(self.client.completions[0]["result"]["error"], "worker_restarted")
        self.assertTrue(self.client.completions[0]["cleaned"])

    def test_uncertain_cleanup_never_reports_completion(self):
        self.runner.cleanup_failed = True
        with self.assertRaises(RuntimeError):
            self.agent.run_assignment(self.assignment)
        self.assertEqual(self.client.completions, [])
        self.assertNotIn("cleaned", self.agent.job)

    def test_bad_input_hash_never_executes(self):
        self.assignment["input_hash"] = "0" * 64
        self.agent.run_assignment(self.assignment)
        self.assertEqual(self.runner.executions, 0)
        self.assertEqual(self.client.completions[0]["result"]["error"], "execution_failed")

    def test_duplicate_agent_is_locked(self):
        with self.assertRaises(BlockingIOError):
            Agent(self.config, self.client, self.runner)

    def test_renewal_loss_fails_closed_after_thirty_seconds(self):
        self.agent.job = {**self.assignment, "incarnation": self.agent.incarnation}
        self.agent.last_renewal = 0
        with (
            patch.object(self.agent.lease_finished, "wait", return_value=False),
            patch.object(self.client, "post", side_effect=TransportError()),
            patch("execution.agent.time.monotonic", return_value=31),
        ):
            self.agent.renew()
        self.assertTrue(self.agent.cancel.is_set())

    def test_https_redirect_is_not_followed(self):
        from unittest.mock import MagicMock

        from execution.agent import Client

        connection = MagicMock()
        connection.getresponse.return_value.status = 302
        with patch("execution.agent.http.client.HTTPSConnection", return_value=connection):
            with self.assertRaises(TransportError):
                Client(self.config).post("claim", {"incarnation": self.agent.incarnation})
        self.assertEqual(connection.request.call_count, 1)
        self.assertEqual(
            connection.request.call_args.args[3]["User-Agent"], "auto-reviewer-worker/1"
        )
        connection.close.assert_called_once()

    def test_private_config_rejects_unpinned_image_and_insecure_origin(self):
        path = Path(self.directory.name) / "worker.json"
        atomic_json(path, self.config)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(load_config(path)["slots"], 1)
        for changes in (
            {"images": {"model": "latest"}},
            {"coordinator": "http://example.com"},
            {"coordinator": "https://user:secret@example.com"},
            {"slots": 2},
            {"auth_volumes": {"codex_cli": "/var/run/docker.sock"}},
        ):
            atomic_json(path, {**self.config, **changes})
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                load_config(path)

    def test_container_arguments_never_mount_host_or_socket(self):
        runner = DockerRunner(self.config)
        for operation in ("model", "test", "graph"):
            arguments = runner.base(operation)
            self.assertIn("--read-only", arguments)
            self.assertIn("--cap-drop=ALL", arguments)
            self.assertIn("--security-opt=no-new-privileges", arguments)
            self.assertFalse(any("docker.sock" in value for value in arguments))
            if operation != "model":
                self.assertIn("--network=none", arguments)

    def test_cleanup_uses_owned_immutable_ids(self):
        runner = DockerRunner(self.config)
        identifier = "a" * 64
        info = json.dumps(
            [{"Config": {"Labels": {"auto-reviewer.worker": self.config["worker_id"]}}}]
        )
        with patch(
            "execution.docker_runner.docker", side_effect=[identifier, info, "", ""]
        ) as docker:
            runner.cleanup()
        self.assertIn(unittest.mock.call(["rm", "-f", "-v", identifier]), docker.call_args_list)


class WorkerMetricsTests(unittest.TestCase):
    def test_memory_is_host_usage_not_missing_as_zero(self):
        from execution.agent import resource_metrics

        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "execution.agent.Path.read_text",
                return_value="MemTotal: 1024 kB\nMemAvailable: 256 kB\n",
            ):
                metrics = resource_metrics(directory)
            self.assertEqual(metrics["memory_total"], 1024 * 1024)
            self.assertEqual(metrics["memory_used"], 768 * 1024)
            with patch("execution.agent.Path.read_text", side_effect=OSError()):
                metrics = resource_metrics(directory)
            self.assertNotIn("memory_total", metrics)
            self.assertNotIn("memory_used", metrics)
            self.assertGreater(metrics["disk_free"], 0)


class DockerOperationTests(unittest.TestCase):
    def test_all_operations_use_stdin_and_stream_or_summarize_stdout(self):
        import base64
        import sys
        import tarfile

        from core.tests.test_worker_artifacts import archive

        digest = "sha256:" + "a" * 64
        for operation in ("model", "graph", "test"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                config = {
                    "worker_id": str(uuid.uuid4()),
                    "state_dir": directory,
                    "images": {"model": digest, "graph": digest},
                    "auth_volumes": {"codex_cli": "auth"},
                    "graph_cache_volume": "cache",
                    "test_images": [digest],
                }
                payload = {"kind": "codex_cli"}
                if operation == "test":
                    payload = {
                        "image": digest,
                        "command": ["false"],
                        "archive": base64.b64encode(
                            archive([("repo/a", tarfile.REGTYPE, b"source")])
                        ).decode(),
                    }
                metadata = json.dumps([{"Config": {"Volumes": None}}])
                state = json.dumps(
                    {
                        "Running": False,
                        "Status": "exited",
                        "ExitCode": 1 if operation == "test" else 0,
                    }
                )
                replies = (
                    [metadata, "", "a" * 64, state]
                    if operation != "test"
                    else [metadata, "a" * 64, state]
                )
                chunks = []
                created = []
                # Real child process exercises stdin and selector-driven streaming, no Docker needed.
                command = [
                    sys.executable,
                    "-c",
                    "import sys; assert sys.stdin.buffer.read(); print('{\"output\":true}')",
                ]
                with (
                    patch("execution.docker_runner.docker", side_effect=replies),
                    patch("execution.docker_runner.DOCKER", command),
                ):
                    result = DockerRunner(config).execute(
                        operation, payload, chunks.append, lambda: False, created.append
                    )
                self.assertEqual(created, ["a" * 64])
                self.assertEqual(result, {"exit_code": 0, "oom": False})
                output = json.loads(b"".join(chunks))
                self.assertEqual(
                    output.get("passed") if operation == "test" else output["output"],
                    False if operation == "test" else True,
                )

    def test_cancelled_before_start_never_creates_container(self):
        from execution.storage import ReviewCancelled

        with tempfile.TemporaryDirectory() as directory:
            config = {
                "worker_id": str(uuid.uuid4()),
                "state_dir": directory,
                "images": {"model": "sha256:" + "a" * 64},
                "auth_volumes": {"codex_cli": "auth"},
            }
            with patch(
                "execution.docker_runner.docker", side_effect=[json.dumps([{"Config": {}}]), ""]
            ) as docker:
                with self.assertRaises(ReviewCancelled):
                    DockerRunner(config).execute(
                        "model", {"kind": "codex_cli"}, lambda _: None, lambda: True, lambda _: None
                    )
            self.assertFalse(any(call.args[0][0] == "create" for call in docker.call_args_list))
