from unittest.mock import patch

from cryptography.fernet import Fernet
from django.test import override_settings

from core.github import IntegrationError
from core.models import Receipt
from core.publication import StaleReview, publish
from core.tests.test_publication import PublicationTests


@override_settings(CREDENTIAL_KEY=Fernet.generate_key().decode())
class DeliveryTests(PublicationTests):
    def test_typed_rate_limit_keeps_saved_analysis_for_automatic_retry(self):
        from datetime import timedelta

        from django.utils import timezone

        from core.delivery import deliver, save_review
        from core.github import GitHubRateLimited
        from core.models import ReviewCheckpoint

        job, gh = self.prepare()
        save_review(job, self.result(), {}, {"passed": True, "summary": "Pass"})
        before = ReviewCheckpoint.objects.get(job=job).encrypted_payload
        error = GitHubRateLimited(timezone.now() + timedelta(minutes=5))
        with patch("core.delivery.time.sleep") as sleep:
            with self.assertRaises(GitHubRateLimited) as raised:
                deliver(gh, job, lambda *args: (_ for _ in ()).throw(error), lambda *a: None)
            sleep.assert_not_called()
        self.assertIs(raised.exception, error)
        checkpoint = ReviewCheckpoint.objects.get(job=job)
        self.assertEqual(checkpoint.stage, "ready")
        self.assertEqual(checkpoint.encrypted_payload, before)
        self.assertEqual(checkpoint.delivery_attempts, 1)

    def test_rate_limit_after_review_keeps_receipt_and_resumes_labels(self):
        from datetime import timedelta

        from django.utils import timezone

        from core.delivery import deliver, save_review
        from core.github import GitHubRateLimited
        from core.models import ReviewCheckpoint

        job, gh = self.prepare()
        save_review(
            job, {**self.result(), "blocking": True}, {}, {"passed": True, "summary": "Pass"}
        )
        original = gh.delete
        error = GitHubRateLimited(timezone.now() + timedelta(minutes=5))
        with patch.object(gh, "delete", side_effect=error):
            with self.assertRaises(GitHubRateLimited):
                deliver(gh, job, publish, lambda *a, **kw: None)
        self.assertEqual(ReviewCheckpoint.objects.get(job=job).stage, "labels")
        self.assertTrue(Receipt.objects.filter(job=job, key="review").exists())
        gh.delete = original
        self.assertEqual(deliver(gh, job, publish, lambda *a, **kw: None), "feedback")
        self.assertEqual(len(gh.reviews), 1)
        self.assertEqual(len(gh.comments), 1)

    def test_saved_review_retries_label_cleanup_without_duplicate_review(self):
        from core.delivery import deliver, save_review
        from core.models import ReviewCheckpoint

        job, gh = self.prepare()
        result = {**self.result(), "blocking": True}
        save_review(job, result, {}, {"passed": True, "summary": "Pass"})
        original = gh.delete
        calls = []

        def delete(path):
            calls.append(path)
            if len(calls) == 1:
                raise IntegrationError("GitHub request failed (HTTP 502).")
            return original(path)

        gh.delete = delete
        with patch("core.delivery.time.sleep"):
            self.assertEqual(deliver(gh, job, publish, lambda *a, **kw: None), "feedback")
        self.assertEqual(len(gh.reviews), 1)
        self.assertEqual(len(gh.comments), 1)
        self.assertEqual(gh.snapshot["labels"], [{"name": "feedback"}])
        checkpoint = ReviewCheckpoint.objects.get(job=job)
        self.assertEqual(checkpoint.stage, "done")
        self.assertNotIn(result["summary"], checkpoint.encrypted_payload)

    def test_human_override_between_delivery_attempts_stops_retry(self):
        from core.delivery import deliver, save_review

        job, gh = self.prepare()
        save_review(
            job, {**self.result(), "blocking": True}, {}, {"passed": True, "summary": "Pass"}
        )

        def delete(path):
            gh.timeline_event("labeled", "approved", actor="dev")
            gh.snapshot["labels"].append({"name": "approved"})
            raise IntegrationError("GitHub request failed (HTTP 502).")

        gh.delete = delete
        with patch("core.delivery.time.sleep"), self.assertRaises(StaleReview):
            deliver(gh, job, publish, lambda *a, **kw: None)
        self.assertEqual(len(gh.reviews), 1)
        self.assertIn({"name": "approved"}, gh.snapshot["labels"])

    def test_post_response_lost_is_reconciled_by_marker_not_reposted(self):
        from core.delivery import deliver, save_review

        job, gh = self.prepare()
        save_review(
            job, {**self.result(), "blocking": True}, {}, {"passed": True, "summary": "Pass"}
        )
        original = gh.post

        def post(path, data):
            row = original(path, data)
            if path.endswith("/reviews"):
                raise IntegrationError("GitHub request unavailable or malformed.")
            return row

        gh.post = post
        with patch("core.delivery.time.sleep"):
            self.assertEqual(deliver(gh, job, publish, lambda *a, **kw: None), "feedback")
        self.assertEqual(len(gh.reviews), 1)
        self.assertTrue(Receipt.objects.filter(job=job, key="review").exists())

    def test_changed_issue_requirements_prevent_saved_delivery(self):
        from core.delivery import verify_inputs

        job, gh = self.prepare()
        gh.get = lambda path: {
            "number": 2,
            "title": "Task",
            "body": "New agreement",
            "state": "open",
        }
        with self.assertRaisesRegex(StaleReview, "requirements"):
            verify_inputs(
                gh,
                job,
                {
                    "issues": [
                        {"number": 2, "title": "Task", "body": "Old agreement", "state": "open"}
                    ]
                },
                self.result(),
            )
        self.assertEqual(gh.writes, [])

    def test_retained_result_has_no_full_source_after_success(self):
        import json

        from core.delivery import deliver, save_review
        from core.models import ReviewCheckpoint
        from core.secrets import decrypt

        job, gh = self.prepare()
        context = {"sources": [{"path": "secret.py", "content": "private code"}]}
        save_review(job, self.result(), context, {"passed": True, "summary": "Pass"})
        deliver(gh, job, publish, lambda *a, **kw: None)
        saved = json.loads(decrypt(ReviewCheckpoint.objects.get(job=job).encrypted_payload))
        self.assertNotIn("context", saved)
        self.assertEqual(saved["result"], self.result())
        self.assertNotIn("private code", str(saved))

    def test_rate_limit_pauses_without_immediate_retry(self):
        from core.delivery import DeliveryPaused, deliver, save_review

        job, gh = self.prepare()
        save_review(job, self.result(), {}, {"passed": True, "summary": "Pass"})
        with patch("core.delivery.time.sleep") as sleep:
            with patch(
                "core.publication.publish",
                side_effect=IntegrationError("GitHub request failed (HTTP 429)."),
            ) as publisher:
                with self.assertRaisesRegex(DeliveryPaused, "429"):
                    deliver(gh, job, publisher, lambda *a: None)
                self.assertEqual(publisher.call_count, 1)
            sleep.assert_not_called()
