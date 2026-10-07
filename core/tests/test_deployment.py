import json
import tempfile
from io import StringIO
from itertools import chain, repeat
from pathlib import Path
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import TestCase, override_settings


class DrainTests(TestCase):
    def status(self, action):
        output = StringIO()
        call_command("worker_control", action, json=True, stdout=output)
        return json.loads(output.getvalue())

    def test_zero_running_jobs_is_not_idle_until_worker_acknowledges(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(DATA_DIR=Path(directory), CLI_RUNNER_IMAGE=""),
        ):
            self.assertFalse(self.status("drain")["idle"])
            with patch("core.management.commands.review_worker.claim") as claim:
                call_command("review_worker", once=True)
            claim.assert_not_called()
            self.assertTrue(self.status("status")["idle"])
            self.assertTrue(self.status("drain")["idle"])
            self.assertFalse(self.status("resume")["idle"])
            self.assertFalse((Path(directory) / "worker.drained").exists())

    def test_acknowledgment_happens_after_job_cleanup_returns(self):
        from core.management.commands import review_worker

        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(DATA_DIR=Path(directory), CLI_RUNNER_IMAGE=""),
        ):

            def run_job(job):
                # Simulate a job with terminal DB status, still inside finally cleanup.
                self.assertFalse(self.status("drain")["idle"])
                self.assertFalse((Path(directory) / "worker.drained").exists())

            with (
                patch.object(review_worker, "claim", return_value=object()),
                patch.object(review_worker, "run_job", side_effect=run_job),
                patch("core.quota.refresh_due"),
                patch.object(review_worker.time, "sleep", side_effect=KeyboardInterrupt),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    call_command("review_worker")
            self.assertTrue(self.status("status")["idle"])


class DeployScriptTests(TestCase):
    def execute(self, failure=None, ready=True):
        from scripts.deploy import deploy

        calls = []
        polls = chain(
            [(False, ""), (True, "old-worker"), (False, "")],
            repeat((True, "new-worker") if ready else (False, "")),
        )

        def run(*args):
            calls.append(args)
            if failure and failure(args):
                raise RuntimeError("simulated deployment failure")
            if args[-2:] == ("status", "--json"):
                # Initial preflight, then worker cleanup still running, then acknowledged.
                if len([x for x in calls if x[-2:] == ("status", "--json")]) == 1:
                    return json.dumps({"draining": False, "idle": False, "active_jobs": 0})
                idle, token = next(polls)
                return json.dumps(
                    {"draining": True, "idle": idle, "active_jobs": 0, "worker_token": token}
                )
            if args[-2:] == ("-q", "worker"):
                return "worker-container"
            if args[:2] == ("docker", "inspect"):
                return "sha256:old-image"
            return ""

        try:
            deploy(run=run, sleep=lambda _: None)
        except RuntimeError:
            pass
        return calls

    def test_waits_for_ack_before_replacing_and_resumes_after_health_checks(self):
        calls = self.execute()

        def index(part):
            return next(i for i, cmd in enumerate(calls) if part in cmd)

        self.assertLess(index("build"), index("drain"))
        self.assertLess(index("backup"), index("up"))
        polls = [i for i, c in enumerate(calls) if c[-2:] == ("status", "--json")]
        self.assertEqual(len(polls), 5)
        self.assertLess(polls[2], index("up"))
        self.assertGreater(polls[-1], index("up"))
        self.assertIn("--wait", calls[index("up")])
        self.assertLess(index("up"), index("resume"))

    def test_failed_rollout_leaves_intake_paused(self):
        calls = self.execute(lambda c: "up" in c)
        self.assertTrue(any("drain" in c for c in calls))
        self.assertFalse(any("resume" in c for c in calls))

    def test_failed_candidate_checks_do_not_pause_worker(self):
        calls = self.execute(lambda c: "migrate" in c)
        self.assertFalse(any("drain" in c for c in calls))

    def test_replacement_worker_not_ready_keeps_intake_paused(self):
        calls = self.execute(ready=False)
        self.assertTrue(any("up" in c for c in calls))
        self.assertFalse(any("resume" in c for c in calls))

    def test_preexisting_pause_and_old_protocol_stop_before_build(self):
        from scripts.deploy import deploy

        for response in [{"draining": True, "idle": True, "active_jobs": 0}, {}]:
            with self.subTest(response=response):
                run = Mock(return_value=json.dumps(response))
                with self.assertRaises(RuntimeError):
                    deploy(run=run)
                self.assertEqual(run.call_count, 1)
