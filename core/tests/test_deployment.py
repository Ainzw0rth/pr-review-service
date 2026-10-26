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
                call_command("review_worker", allow_sqlite=True, once=True)
            claim.assert_not_called()
            self.assertTrue(self.status("status")["idle"])
            self.assertTrue(self.status("drain")["idle"])
            self.assertFalse(self.status("resume")["idle"])
            self.assertFalse((Path(directory) / "worker.drained").exists())

    def test_acknowledgment_happens_after_job_cleanup_returns(self):
        from core.claims import register_owner, release
        from core.models import ServiceSettings, WorkerOwner
        from core.tests.test_claims import ClaimTests
        from core.workflow import claim, finish

        ClaimTests.setUp(self)
        ClaimTests.job(self, 1)
        owner = register_owner()
        job = claim(owner)
        finish(job, "completed")
        self.assertFalse(self.status("drain")["idle"])
        generation = ServiceSettings.objects.get(pk=1).drain_generation
        WorkerOwner.objects.filter(pk=owner.pk).update(drain_ack=generation)
        self.assertFalse(self.status("status")["idle"])  # Terminal status is not cleanup.
        release(job)
        self.assertTrue(self.status("status")["idle"])
        second = register_owner()
        self.assertFalse(self.status("status")["idle"])  # Every supervisor must acknowledge.
        WorkerOwner.objects.filter(pk=second.pk).update(drain_ack=generation)
        self.assertTrue(self.status("status")["idle"])
        self.status("resume")
        self.assertFalse(self.status("drain")["idle"])  # Old acknowledgment cannot be reused.


class DeployScriptTests(TestCase):
    def execute(self, failure=None, ready=True, legacy_graph=False):
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
            if legacy_graph and args[-1] == "name=^/pr-review-graph-[0-9a-f]{32}$":
                return "legacy-graph-container"
            if args[-2:] == ("status", "--json"):
                # Initial preflight, then worker cleanup still running, then acknowledged.
                if len([x for x in calls if x[-2:] == ("status", "--json")]) == 1:
                    return json.dumps(
                        {
                            "draining": False,
                            "idle": False,
                            "active_jobs": 0,
                            "remote_active": 0,
                            "remote_quarantined": 0,
                        }
                    )
                idle, token = next(polls)
                return json.dumps(
                    {
                        "draining": True,
                        "idle": idle,
                        "active_jobs": 0,
                        "worker_token": token,
                        "remote_active": 0,
                        "remote_quarantined": 0,
                    }
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

        for response in [
            {
                "draining": True,
                "idle": True,
                "active_jobs": 0,
                "remote_active": 0,
                "remote_quarantined": 0,
            },
            {},
        ]:
            with self.subTest(response=response):
                run = Mock(return_value=json.dumps(response))
                with self.assertRaises(RuntimeError):
                    deploy(run=run)
                self.assertEqual(run.call_count, 1)

    def test_backup_and_migrate_only_after_all_writers_stopped(self):
        calls = self.execute()
        stop = next(i for i, c in enumerate(calls) if "stop" in c)
        backup = next(i for i, c in enumerate(calls) if "backup" in c)
        migrate = next(i for i, c in enumerate(calls) if "migrate" in c and "--noinput" in c)
        self.assertLess(stop, backup)
        self.assertLess(backup, migrate)
        self.assertIn("-1", calls[stop])
        self.assertTrue(any("deployment_control" in c and "pause" in c for c in calls[:stop]))

    def test_failed_migration_never_starts_candidate(self):
        calls = self.execute(lambda c: "migrate" in c and "--noinput" in c)
        self.assertTrue(any("stop" in c for c in calls))
        self.assertFalse(any("up" in c or "resume" in c for c in calls))

    def test_unlabelled_legacy_graph_refuses_backup_and_rollout(self):
        calls = self.execute(legacy_graph=True)
        self.assertTrue(any("stop" in c for c in calls))
        self.assertTrue(any("name=^/pr-review-graph-[0-9a-f]{32}$" in c for c in calls))
        self.assertFalse(any("backup" in c or "up" in c or "resume" in c for c in calls))

    def test_failed_backup_never_migrates(self):
        calls = self.execute(lambda c: "backup" in c)
        self.assertFalse(any("migrate" in c and "--noinput" in c for c in calls))
        self.assertFalse(any("resume" in c for c in calls))


class MaintenanceGateTests(TestCase):
    def test_gate_blocks_webhook_and_session_writes_but_allows_health(self):
        from django.http import HttpResponse
        from django.test import RequestFactory

        from core.deployment import MaintenanceMiddleware

        with tempfile.TemporaryDirectory() as temp, override_settings(DATA_DIR=Path(temp)):
            downstream = Mock(return_value=HttpResponse("ok"))
            middleware = MaintenanceMiddleware(downstream)
            call_command("deployment_control", "pause")
            self.assertEqual(middleware(RequestFactory().post("/webhooks/")).status_code, 503)
            downstream.assert_not_called()
            self.assertEqual(middleware(RequestFactory().get("/healthz/")).status_code, 200)
            call_command("deployment_control", "open")
            self.assertEqual(middleware(RequestFactory().get("/")).status_code, 200)
