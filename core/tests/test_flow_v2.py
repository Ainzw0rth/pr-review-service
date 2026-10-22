from django.test import TestCase, override_settings
from django.utils import timezone

from core.activity import message, record
from core.flow import snapshot
from core.models import (
    Credential,
    Job,
    ModelAttempt,
    Organization,
    Provider,
    PullRequest,
    Repository,
)


@override_settings(GRAPH_REPOSITORIES=[])
class WorkflowV2Tests(TestCase):
    def setUp(self):
        org = Organization.objects.create(name="Org", slug="org")
        credential = Credential.objects.create(name="github", encrypted_value="unused")
        repo = Repository.objects.create(
            organization=org,
            full_name="org/repo",
            reviewer="owner",
            github_credential=credential,
            webhook_credential=credential,
        )
        pr = PullRequest.objects.create(repository=repo, number=1, head_sha="a")
        self.job = Job.objects.create(
            pr=pr, generation=1, head_sha="a", actor="dev", status="running"
        )

    def stages(self):
        return {
            stage["id"]: stage
            for stage in snapshot(self.job, list(self.job.activity.all()))["stages"]
        }

    def test_model_phase_tracks_intent_to_review_without_claiming_overlap(self):
        provider = Provider.objects.create(name="Sol", kind="codex_cli", model="sol")
        attempt = ModelAttempt.objects.create(
            job=self.job,
            provider=provider,
            provider_name=provider.name,
            model=provider.model,
            diagnostics={"phase": "intent", "evidence_read": 46, "evidence_total": 46},
        )
        record(self.job, "intent_started")
        flow = snapshot(self.job, list(self.job.activity.all()))
        self.assertEqual(flow["models"][0]["attempts"][0]["phase"], "intent")
        self.assertEqual(self.stages()["intent"]["state"], "running")
        record(self.job, "intent_ready")
        record(self.job, "model")
        attempt.diagnostics = {"phase": "review"}
        attempt.save()
        flow = snapshot(self.job, list(self.job.activity.all()))
        self.assertEqual(flow["models"][0]["attempts"][0]["phase"], "review")
        self.assertEqual(self.stages()["intent"]["state"], "done")

    def test_reviewer_sessions_are_scoped_to_their_provider_attempt(self):
        provider = Provider.objects.create(name="Sol", kind="codex_cli", model="sol")
        first = ModelAttempt.objects.create(
            job=self.job,
            provider=provider,
            provider_name="First",
            model="sol",
            diagnostics={"phase": "code", "failure_detail": "review_incomplete"},
        )
        record(self.job, "intent_started")
        record(self.job, "intent_ready")
        record(self.job, "code_started")
        first_code = self.job.activity.last()
        first.status = "failed"
        first.finished_at = timezone.now()
        first.save()
        second = ModelAttempt.objects.create(
            job=self.job,
            provider=provider,
            provider_name="Second",
            model="sol",
            diagnostics={"phase": "security"},
        )
        record(self.job, "intent_started")
        record(self.job, "intent_ready")
        record(self.job, "code_started")
        record(self.job, "code_ready")
        second_code = self.job.activity.last()
        record(self.job, "security_started")
        attempts = snapshot(self.job, list(self.job.activity.all()))["models"][0]["attempts"]
        phases = {row["id"]: {p["phase"]: p for p in row["phases"]} for row in attempts}
        self.assertEqual(phases[first.pk]["code"]["state"], "skipped")
        self.assertIsNone(phases[first.pk]["code"]["coverage_complete"])
        self.assertEqual(phases[first.pk]["code"]["started_at"], first_code.created_at)
        self.assertEqual(phases[first.pk]["code"]["finished_at"], first.finished_at)
        self.assertEqual(phases[first.pk]["security"]["state"], "skipped")
        self.assertEqual(phases[second.pk]["code"]["state"], "done")
        self.assertEqual(phases[second.pk]["code"]["finished_at"], second_code.created_at)
        self.assertEqual(phases[second.pk]["security"]["state"], "running")
        self.assertEqual(phases[second.pk]["tests"]["state"], "pending")

    def test_legacy_attempts_do_not_invent_reviewer_sessions(self):
        self.job.workflow_revision = "v2.1"
        provider = Provider.objects.create(name="Old", kind="codex_cli", model="old")
        ModelAttempt.objects.create(
            job=self.job, provider=provider, provider_name="Old", model="old"
        )
        record(self.job, "model")
        value = snapshot(self.job, list(self.job.activity.all()))["models"][0]["attempts"][0]
        self.assertEqual(value["phases"], [])

    def test_saved_phase_timing_survives_transcript_retention(self):
        provider = Provider.objects.create(name="Sol", kind="codex_cli", model="sol")
        attempt = ModelAttempt.objects.create(
            job=self.job, provider=provider, provider_name="Sol", model="sol"
        )
        started = timezone.now()
        finished = timezone.now()
        attempt.diagnostics = {
            "phase_timeline": [
                {
                    "phase": "code",
                    "state": "incomplete",
                    "started_at": started.isoformat(),
                    "finished_at": finished.isoformat(),
                }
            ]
        }
        attempt.save()
        phases = snapshot(self.job, [])["models"][0]["attempts"][0]["phases"]
        code = next(p for p in phases if p["phase"] == "code")
        self.assertEqual(code["state"], "done")
        self.assertIs(code["coverage_complete"], False)
        self.assertEqual(code["started_at"], started)
        self.assertEqual(code["finished_at"], finished)

    def test_delivered_assessment_is_complete_despite_coverage_caveats(self):
        provider = Provider.objects.create(name="Sol", kind="codex_cli", model="sol")
        ModelAttempt.objects.create(
            job=self.job, provider=provider, provider_name="Sol", model="sol"
        )
        record(self.job, "code_started")
        record(self.job, "code_incomplete")
        flow = snapshot(self.job, list(self.job.activity.all()))
        code = next(p for p in flow["models"][0]["attempts"][0]["phases"] if p["phase"] == "code")
        self.assertEqual(code["state"], "done")
        self.assertIs(code["coverage_complete"], False)
        stage = next(p for p in flow["stages"] if p["id"] == "code_review")
        self.assertEqual(stage["state"], "done")
        self.assertIs(stage["coverage_complete"], False)

    def test_stopped_team_skips_only_sessions_without_a_delivered_assessment(self):
        provider = Provider.objects.create(name="Sol", kind="codex_cli", model="sol")
        attempt = ModelAttempt.objects.create(
            job=self.job, provider=provider, provider_name="Sol", model="sol"
        )
        record(self.job, "intent_started")
        record(self.job, "intent_ready")
        record(self.job, "code_started")
        record(self.job, "code_ready")
        record(self.job, "security_started")
        attempt.status = "failed"
        attempt.finished_at = timezone.now()
        attempt.save()
        phases = {
            p["phase"]: p
            for p in snapshot(self.job, list(self.job.activity.all()))["models"][0]["attempts"][0][
                "phases"
            ]
        }
        self.assertEqual(phases["code"]["state"], "done")
        self.assertIs(phases["code"]["coverage_complete"], True)
        for phase in ("security", "tests", "synthesis"):
            self.assertEqual(phases[phase]["state"], "skipped")
            self.assertIsNone(phases[phase]["coverage_complete"])
        self.assertIsNotNone(phases["security"]["started_at"])
        self.assertIsNone(phases["tests"]["started_at"])

    def test_missing_historical_phase_records_cannot_prove_a_session_was_skipped(self):
        provider = Provider.objects.create(name="Old", kind="codex_cli", model="old")
        attempt = ModelAttempt.objects.create(
            job=self.job, provider=provider, provider_name="Old", model="old"
        )
        for status in ("failed", "cancelled", "success"):
            with self.subTest(status=status):
                attempt.status = status
                attempt.finished_at = timezone.now()
                attempt.save()
                phases = snapshot(self.job, [])["models"][0]["attempts"][0]["phases"]
                for phase in phases:
                    if phase["phase"] != "intent":
                        self.assertEqual(phase["state"], "unknown")
                        self.assertIsNone(phase["coverage_complete"])

    def test_delivered_caveats_survive_a_stale_running_timeline(self):
        provider = Provider.objects.create(name="Sol", kind="codex_cli", model="sol")
        attempt = ModelAttempt.objects.create(
            job=self.job, provider=provider, provider_name="Sol", model="sol"
        )
        started = timezone.now()
        attempt.diagnostics = {
            "phase_timeline": [
                {"phase": "code", "state": "running", "started_at": started.isoformat()}
            ]
        }
        attempt.save()
        record(self.job, "code_incomplete")
        phases = snapshot(self.job, list(self.job.activity.all()))["models"][0]["attempts"][0][
            "phases"
        ]
        code = next(p for p in phases if p["phase"] == "code")
        self.assertEqual(code["state"], "done")
        self.assertIs(code["coverage_complete"], False)

    def test_usage_without_recorded_phase_times_is_unknown_not_waiting(self):
        provider = Provider.objects.create(name="Sol", kind="codex_cli", model="sol")
        attempt = ModelAttempt.objects.create(
            job=self.job,
            provider=provider,
            provider_name="Sol",
            model="sol",
            diagnostics={"phase_usage": [{"phase": "code", "passes": [{}]}]},
        )
        phases = snapshot(self.job, [])["models"][0]["attempts"][0]["phases"]
        self.assertEqual(next(p for p in phases if p["phase"] == "code")["state"], "unknown")
        attempt.status = "failed"
        attempt.finished_at = timezone.now()
        attempt.save()
        phases = snapshot(self.job, [])["models"][0]["attempts"][0]["phases"]
        self.assertTrue(all(p["state"] == "unknown" for p in phases if p["phase"] != "intent"))

    def test_missing_or_untrusted_model_phase_is_not_inferred_or_exposed(self):
        provider = Provider.objects.create(name="Sol", kind="codex_cli", model="sol")
        attempt = ModelAttempt.objects.create(
            job=self.job, provider=provider, provider_name=provider.name, model=provider.model
        )
        for phase in (None, "private-model-text", ["intent"], {"phase": "review"}):
            with self.subTest(phase=phase):
                attempt.diagnostics = {"phase": phase}
                attempt.save()
                value = snapshot(self.job, [])["models"][0]["attempts"][0]
                self.assertIsNone(value["phase"])

    def test_new_jobs_snapshot_explicit_v2_steps(self):
        self.assertEqual(self.job.workflow_revision, "v3.1")
        self.assertEqual(
            list(self.stages()),
            [
                "files",
                "discussion",
                "docs",
                "intent",
                "checks",
                "code_review",
                "security_review",
                "test_review",
                "synthesis",
                "pr_summary",
                "inline_findings",
                "discussion_replies",
                "related_pr_comments",
                "related_issue_comments",
                "labels",
            ],
        )
        self.assertTrue(all(stage["state"] == "pending" for stage in self.stages().values()))

    def test_specialist_stages_show_overlap_and_latest_provider_progress(self):
        record(self.job, "intent_started")
        record(self.job, "intent_ready")
        record(self.job, "code_started")
        record(self.job, "security_started")
        record(self.job, "tests_started")
        self.assertEqual(self.stages()["code_review"]["state"], "running")
        self.assertEqual(self.stages()["security_review"]["state"], "running")
        record(self.job, "code_ready")
        self.assertEqual(self.stages()["code_review"]["state"], "done")
        self.assertEqual(self.stages()["synthesis"]["state"], "pending")
        record(self.job, "intent_started")
        self.assertEqual(self.stages()["code_review"]["state"], "pending")
        record(self.job, "code_started")
        self.job.status = "attention"
        self.assertEqual(self.stages()["code_review"]["state"], "attention")

    def test_delivery_not_needed_is_explicit_and_safe(self):
        for prefix, key in [
            ("inline", "inline_findings"),
            ("discussion", "discussion_replies"),
            ("related_pr", "related_pr_comments"),
            ("related_issue", "related_issue_comments"),
        ]:
            record(self.job, f"{prefix}_started")
            record(self.job, f"{prefix}_unused", count=0)
            stage = self.stages()[key]
            self.assertEqual(stage["state"], "not_needed")
            self.assertEqual(stage["count"], 0)
            self.assertIsNotNone(stage["finished_at"])
        with self.assertRaises(ValueError):
            record(self.job, "inline_unused", detail="Private model rationale")
        self.assertNotIn("quota", message(self.job.activity.last()))

    def test_completed_receipts_survive_retry_and_label_failure(self):
        record(self.job, "starting")
        record(self.job, "intent_started")
        record(self.job, "intent_ready")
        record(self.job, "summary_started")
        record(self.job, "summary_done", count=1)
        record(self.job, "inline_done", count=2)
        record(self.job, "labels_started")
        self.job.status = "attention"
        self.assertEqual(self.stages()["labels"]["state"], "attention")
        self.assertEqual(self.stages()["pr_summary"]["state"], "done")
        record(self.job, "starting")
        record(self.job, "delivery_resume")
        record(self.job, "summary_started")
        self.job.status = "running"
        self.assertEqual(self.stages()["pr_summary"]["state"], "done")
        self.assertEqual(self.stages()["inline_findings"]["count"], 2)
        self.assertEqual(self.stages()["intent"]["state"], "done")
        record(self.job, "labels_done", count=1)
        self.job.status = "completed"
        self.assertEqual(self.stages()["labels"]["state"], "done")

    def test_later_failed_intent_does_not_inherit_first_provider_success(self):
        record(self.job, "intent_started")
        record(self.job, "intent_ready")
        record(self.job, "intent_started")
        self.job.status = "failed"
        self.assertEqual(self.stages()["intent"]["state"], "attention")
        self.assertIsNone(self.stages()["intent"]["finished_at"])

    def test_ended_unfinished_category_does_not_claim_success_or_count(self):
        record(self.job, "inline_started")
        self.job.status = "cancelled"
        stage = self.stages()["inline_findings"]
        self.assertEqual(stage["state"], "cancelled")
        self.assertIsNone(stage["count"])
        self.assertEqual(self.stages()["pr_summary"]["state"], "pending")

    def test_later_completed_intent_reports_its_own_timing(self):
        record(self.job, "intent_started")
        record(self.job, "intent_ready")
        record(self.job, "intent_started")
        started = self.job.activity.last()
        record(self.job, "intent_ready")
        finished = self.job.activity.last()
        stage = self.stages()["intent"]
        self.assertEqual(stage["state"], "done")
        self.assertEqual(stage["started_at"], started.created_at)
        self.assertEqual(stage["finished_at"], finished.created_at)

    def test_saved_historical_versions_ignore_new_events(self):
        legacy_steps = ["files", "discussion", "docs", "checks", "publication"]
        record(self.job, "intent_ready")
        record(self.job, "summary_done", count=1)
        for revision in ["v1", "v1.1"]:
            with self.subTest(revision=revision):
                self.job.workflow_revision = revision
                self.job.workflow_steps = legacy_steps
                self.assertEqual(list(self.stages()), legacy_steps)

    def test_capacity_wait_is_per_phase_and_clears_on_resume_or_terminal(self):
        from core.workflow_schema import REVISION

        self.job.workflow_revision = REVISION
        self.job.save(update_fields=["workflow_revision"])
        provider = Provider.objects.create(name="Sol", kind="codex_cli", model="sol")
        started = timezone.now().isoformat()
        attempt = ModelAttempt.objects.create(
            job=self.job,
            provider=provider,
            provider_name=provider.name,
            model=provider.model,
            diagnostics={
                "phase": "specialists",
                "phase_timeline": [
                    {
                        "phase": "code",
                        "state": "waiting",
                        "started_at": started,
                        "finished_at": None,
                    },
                    {
                        "phase": "security",
                        "state": "running",
                        "started_at": started,
                        "finished_at": None,
                    },
                ],
            },
        )
        # Record valid phase timestamps within the actual attempt.
        started = attempt.started_at.isoformat()
        for row in attempt.diagnostics["phase_timeline"]:
            row["started_at"] = started
        attempt.save()

        def phases():
            flow = snapshot(self.job, list(self.job.activity.all()))
            return {row["phase"]: row for row in flow["models"][0]["attempts"][0]["phases"]}

        self.assertTrue(phases()["code"]["waiting"])
        self.assertFalse(phases()["security"]["waiting"])
        self.assertEqual(phases()["code"]["state"], "running")
        attempt.diagnostics["phase_timeline"][0]["state"] = "running"
        attempt.save()
        self.assertFalse(phases()["code"]["waiting"])
        attempt.diagnostics["phase_timeline"][0]["state"] = "waiting"
        attempt.status = "cancelled"
        attempt.finished_at = timezone.now()
        attempt.save()
        self.job.status = "cancelled"
        self.assertFalse(phases()["code"]["waiting"])
