"""V2 delivery decisions and receipt-backed public progress."""

from django.test import TestCase, override_settings

from core.delivery import save_review
from core.models import Receipt
from core.publication import LabelCleanupError, StaleReview
from core.tests import test_publication as publication_tests
from core.tests import test_thread_resolution as thread_tests


@override_settings(LIVE_WRITES=True)
class PublicationV2Tests(TestCase):
    setUp = thread_tests.ThreadResolutionTests.setUp
    snapshot = thread_tests.ThreadResolutionTests.snapshot
    event = thread_tests.ThreadResolutionTests.event
    reconcile = thread_tests.ThreadResolutionTests.reconcile
    prepare = publication_tests.PublicationTests.prepare
    result = publication_tests.PublicationTests.result
    setup_thread = thread_tests.ThreadResolutionTests.setup_thread
    run_publish = thread_tests.ThreadResolutionTests.run_publish

    def v2(self, job):
        job.workflow_revision = "v2"
        job.save(update_fields=["workflow_revision"])

    def assessment(self, result, *, action="not_needed", blocking=False):
        result["replies"][0].update(
            action=action,
            reason="Latest discussion needs no additional response.",
            blocking=blocking,
            resolve=False,
        )

    def events(self, job):
        return list(job.activity.values_list("code", "count"))

    def test_not_needed_checks_thread_without_reply_or_resolution(self):
        job, gh, result, context, _, mutations = self.setup_thread()
        self.v2(job)
        self.assessment(result)
        self.assertEqual(self.run_publish(gh, job, result, context), "ready")
        self.assertFalse(any(path.endswith("/replies") for path, _ in gh.writes))
        self.assertFalse(Receipt.objects.filter(job=job, key="reply:11").exists())
        self.assertEqual(mutations, [])
        self.assertIn(("discussion_unused", 0), self.events(job))

    def test_not_needed_still_rejects_changed_discussion(self):
        job, gh, result, context, thread, _ = self.setup_thread()
        self.v2(job)
        self.assessment(result)
        thread["comments"][0]["updatedAt"] = "changed"
        with self.assertRaises(StaleReview):
            self.run_publish(gh, job, result, context)
        self.assertEqual(gh.writes, [])

    def test_skipped_reply_cannot_resolve(self):
        job, gh, result, context, _, _ = self.setup_thread()
        self.assessment(result)
        result["replies"][0]["resolve"] = True
        with self.assertRaises(ValueError):
            self.run_publish(gh, job, result, context)
        self.assertEqual(gh.writes, [])

    def test_reply_blocking_propagates_to_workflow(self):
        job, gh, result, context, _, _ = self.setup_thread()
        self.v2(job)
        self.assessment(result, action="reply", blocking=True)
        self.assertEqual(self.run_publish(gh, job, result, context), "feedback")
        self.assertIn(("discussion_done", 1), self.events(job))

    def test_supplied_issue_is_allowed_but_arbitrary_target_is_not(self):
        job, gh = self.prepare()
        self.v2(job)
        result = self.result()
        result["related_comments"] = [{"number": 12, "body": "Implementation reviewed."}]
        self.assertEqual(self.run_publish(gh, job, result, {"issues": [{"number": 12}]}), "ready")
        self.assertIn(("related_issue_done", 1), self.events(job))
        self.assertIn("/issues/12/comments", [path for path, _ in gh.writes])

    def test_foreign_repo_target_is_rejected_before_writes(self):
        job, gh = self.prepare()
        self.v2(job)
        result = self.result()
        result["related_comments"] = [{"number": 12, "body": "Review."}]
        with self.assertRaises(ValueError):
            self.run_publish(
                gh,
                job,
                result,
                {
                    "issues": [
                        {"number": 12, "repository_url": "https://api.github.com/repos/other/repo"}
                    ]
                },
            )
        self.assertEqual(gh.writes, [])

    def test_empty_categories_and_summary_labels_have_confirmed_events(self):
        job, gh = self.prepare()
        self.v2(job)
        self.run_publish(gh, job, self.result(), {})
        events = self.events(job)
        for code in (
            "inline_unused",
            "discussion_unused",
            "related_pr_unused",
            "related_issue_unused",
        ):
            self.assertIn((code, 0), events)
        self.assertIn(("summary_done", 1), events)
        self.assertIn(("labels_done", 2), events)

    def test_label_retry_does_not_regress_completed_nodes_or_duplicate_comments(self):
        job, gh = self.prepare()
        self.v2(job)
        result = self.result()
        result["findings"] = [
            {"path": "cart.py", "line": 2, "severity": "suggestion", "body": "Simplify."}
        ]
        context = {"files": [{"filename": "cart.py", "valid_lines": [2]}]}
        save_review(job, result, context, {"passed": True, "summary": "Pass"})
        original = gh.delete
        gh.delete = lambda path: (_ for _ in ()).throw(RuntimeError("network"))
        with self.assertRaises(LabelCleanupError):
            self.run_publish(gh, job, result, context)
        self.assertIn(("inline_done", 1), self.events(job))
        self.assertNotIn("labels_done", [code for code, _ in self.events(job)])
        before = self.events(job)
        gh.delete = original
        self.assertEqual(self.run_publish(gh, job, result, context), "ready")
        self.assertEqual(self.events(job)[: len(before)], before)
        self.assertEqual(sum(path.endswith("/comments") for path, _ in gh.writes), 1)
        self.assertEqual(sum(path.endswith("/reviews") for path, _ in gh.writes), 1)
        self.assertEqual(sum(code == "inline_done" for code, _ in self.events(job)), 1)

    def test_already_resolved_discussion_does_not_claim_reply(self):
        job, gh, result, context, thread, _ = self.setup_thread()
        self.v2(job)
        self.assessment(result, action="reply")
        thread["isResolved"] = True
        self.run_publish(gh, job, result, context)
        self.assertIn(("discussion_unused", 0), self.events(job))
        self.assertFalse(Receipt.objects.filter(job=job, key="reply:11").exists())

    def test_legacy_job_does_not_emit_v2_events(self):
        job, gh = self.prepare()
        job.workflow_revision = "v1"
        job.save(update_fields=["workflow_revision"])
        self.run_publish(gh, job, self.result(), {})
        self.assertEqual(self.events(job), [])
