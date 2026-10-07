from django.test import override_settings

from core import workflow
from core.models import Job, PullRequest
from core.publication import StaleReview, publish
from core.tests.test_workflow import WorkflowTests


class FakeGitHub:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.writes = []
        self.comments = []
        self.reviews = []
        key = int(PullRequest.objects.get(number=snapshot["number"]).canonical_trigger)
        self.timeline = [
            {
                "id": key - 1,
                "event": "review_requested",
                "requested_reviewer": {"login": "louis"},
                "actor": {"login": "dev", "type": "User"},
            },
            {
                "id": key,
                "event": "labeled",
                "label": {"name": "on review"},
                "actor": {"login": "dev", "type": "User"},
            },
        ]

    def timeline_event(self, action, label="", actor="louis"):
        self.timeline.append(
            {
                "id": max(event["id"] for event in self.timeline) + 1,
                "event": action,
                "label": {"name": label},
                "requested_reviewer": {"login": "louis"},
                "actor": {"login": actor, "type": "User"},
            }
        )

    def pull(self, n):
        return self.snapshot

    def identity(self):
        return "louis"

    def pages(self, path):
        if path.endswith("/timeline"):
            return list(self.timeline)
        return self.reviews if path.endswith("/reviews") else self.comments

    def post(self, path, data):
        self.writes.append((path, data))
        row = {"id": len(self.writes), "body": data.get("body", ""), "user": {"login": "louis"}}
        if path.endswith("/reviews"):
            row.update(
                commit_id=data["commit_id"],
                state="CHANGES_REQUESTED" if data["event"] == "REQUEST_CHANGES" else "COMMENTED",
            )
            self.reviews.append(row)
            if data["event"] == "REQUEST_CHANGES":
                self.snapshot["requested_reviewers"] = []
        elif path.endswith("/labels"):
            self.snapshot["labels"] += [{"name": x} for x in data["labels"]]
            for label in data["labels"]:
                self.timeline_event("labeled", label)
        else:
            self.comments.append(row)
        return row

    def delete(self, path):
        from urllib.parse import unquote

        label = unquote(path.rsplit("/", 1)[-1])
        self.snapshot["labels"] = [x for x in self.snapshot["labels"] if x["name"] != label]
        self.timeline_event("unlabeled", label)


@override_settings(LIVE_WRITES=True)
class PublicationTests(WorkflowTests):
    def prepare(self):
        self.reconcile(self.event("labeled"), self.snapshot())
        job = workflow.claim()
        return job, FakeGitHub(self.snapshot())

    def result(self):
        return {
            "summary": "No blocking defects.",
            "complete": True,
            "blocking": False,
            "findings": [],
            "replies": [],
            "related_comments": [],
        }

    def test_pass_changes_label_without_formal_approve(self):
        job, gh = self.prepare()
        outcome = publish(
            gh, job, self.result(), {}, {"passed": True, "summary": "Required checks passed."}
        )
        self.assertEqual(outcome, "ready")
        self.assertEqual([x["name"] for x in gh.snapshot["labels"]], ["approved"])
        self.assertFalse(any(data.get("event") == "APPROVE" for _, data in gh.writes))

    def test_request_changes_can_finish_after_review_request_removed(self):
        job, gh = self.prepare()
        result = self.result()
        result["blocking"] = True
        self.assertEqual(
            publish(gh, job, result, {}, {"passed": True, "summary": "Pass"}), "feedback"
        )
        self.assertEqual([x["name"] for x in gh.snapshot["labels"]], ["feedback"])

    def test_stale_commit_prevents_all_writes(self):
        job, gh = self.prepare()
        gh.snapshot["head"]["sha"] = "b" * 40
        with self.assertRaises(StaleReview):
            publish(gh, job, self.result(), {}, {"passed": True, "summary": "Pass"})
        self.assertEqual(gh.writes, [])

    def test_unknown_inline_target_rejected_before_any_writes(self):
        job, gh = self.prepare()
        result = self.result()
        result["findings"] = [{"path": "bad", "line": 99, "severity": "blocking", "body": "bad"}]
        with self.assertRaises(ValueError):
            publish(gh, job, result, {}, {"passed": True, "summary": "Pass"})
        self.assertEqual(gh.writes, [])

    def test_admin_cancel_prevents_all_writes(self):
        job, gh = self.prepare()
        Job.objects.filter(pk=job.pk).update(status="cancelled")
        with self.assertRaises(StaleReview):
            publish(gh, job, self.result(), {}, {"passed": True, "summary": "Pass"})
        self.assertEqual(gh.writes, [])

    def test_reviewer_removed_during_finalization_cannot_apply_approved(self):
        job, gh = self.prepare()
        original = gh.pages

        def pages(path):
            if path.endswith("/reviews"):
                gh.snapshot["requested_reviewers"] = []
            return original(path)

        gh.pages = pages
        with self.assertRaises(StaleReview):
            publish(gh, job, self.result(), {}, {"passed": True, "summary": "Pass"})
        self.assertNotIn("approved", [x["name"] for x in gh.snapshot["labels"]])

    def test_invalid_finding_prevents_valid_related_comment_side_effect(self):
        job, gh = self.prepare()
        result = self.result()
        result["related_comments"] = [{"number": 2, "body": "dependency"}]
        result["findings"] = [
            {"path": "absent.py", "line": 1, "severity": "blocking", "body": "invalid"}
        ]
        with self.assertRaises(ValueError):
            publish(
                gh,
                job,
                result,
                {"related_prs": [{"number": 2}]},
                {"passed": True, "summary": "Pass"},
            )
        self.assertEqual(gh.writes, [])

    def test_spoofed_marker_does_not_suppress_bot_summary(self):
        from core.publication import marker

        job, gh = self.prepare()
        gh.comments.append(
            {"id": 123, "body": marker(job, "summary"), "user": {"login": "contributor"}}
        )
        publish(gh, job, self.result(), {}, {"passed": True, "summary": "Pass"})
        self.assertTrue(any(path.endswith("/comments") for path, _ in gh.writes))

    def test_thread_reply_targets_root_not_developer_reply(self):
        job, gh = self.prepare()
        result = self.result()
        result["replies"] = [{"comment_id": 22, "body": "Needs another guard.", "resolve": False}]
        context = {
            "threads": [{"id": "thread-1", "comments": [{"databaseId": 11}, {"databaseId": 22}]}]
        }
        from unittest.mock import patch

        fresh = {**context["threads"][0], "isResolved": False}
        with patch("core.publication._fresh_thread", return_value=fresh):
            publish(gh, job, result, context, {"passed": True, "summary": "Pass"})
        self.assertIn("/pulls/1/comments/11/replies", [path for path, _ in gh.writes])

    def test_admin_cancel_during_remote_guard_read_prevents_write(self):
        job, gh = self.prepare()
        original = gh.pull

        def pull(number):
            Job.objects.filter(pk=job.pk).update(status="cancelled")
            return original(number)

        gh.pull = pull
        with self.assertRaises(StaleReview):
            publish(gh, job, self.result(), {}, {"passed": True, "summary": "Pass"})
        self.assertEqual(gh.writes, [])

    def test_human_mixed_feedback_during_finalization_prevents_approval(self):
        job, gh = self.prepare()
        original = gh.pages

        def pages(path):
            if path.endswith("/reviews"):
                gh.snapshot["labels"].append({"name": "feedback"})
                gh.timeline_event("labeled", "feedback", actor="developer")
            return original(path)

        gh.pages = pages
        with self.assertRaises(StaleReview):
            publish(gh, job, self.result(), {}, {"passed": True, "summary": "Pass"})
        self.assertNotIn("approved", [label["name"] for label in gh.snapshot["labels"]])

    def comment_review(self, after_review=None):
        job, gh = self.prepare()
        result = self.result()
        result["findings"] = [
            {"path": "a.py", "line": 1, "severity": "suggestion", "body": "Optional cleanup"}
        ]
        original = gh.post

        def post(path, data):
            response = original(path, data)
            if path.endswith("/reviews"):
                # A submitted COMMENT also consumes the pending request on GitHub.
                gh.snapshot["requested_reviewers"] = []
                if after_review:
                    after_review(job, gh)
            return response

        gh.post = post
        return job, gh, result, {"files": [{"filename": "a.py", "valid_lines": [1]}]}

    def test_comment_review_can_finish_after_implicit_request_consumption(self):
        from core.models import Receipt

        job, gh, result, context = self.comment_review()
        self.assertEqual(
            publish(gh, job, result, context, {"passed": True, "summary": "Pass"}), "ready"
        )
        self.assertEqual(gh.snapshot["labels"], [{"name": "approved"}])
        self.assertEqual([r["state"] for r in gh.reviews], ["COMMENTED"])
        self.assertTrue(Receipt.objects.filter(job=job, key="review").exists())
        self.assertFalse(any(data.get("event") == "APPROVE" for _, data in gh.writes))

    def test_comment_review_still_stops_for_explicit_reviewer_removal(self):
        for actor in ["developer", "louis"]:
            with self.subTest(actor=actor):
                # Each cycle must be independent of the previous subtest.
                Job.objects.all().delete()
                PullRequest.objects.all().delete()
                job, gh, result, context = self.comment_review(
                    lambda job, gh: gh.timeline_event("review_request_removed", actor=actor)
                )
                with self.assertRaises(StaleReview):
                    publish(gh, job, result, context, {"passed": True, "summary": "Pass"})
                self.assertNotIn({"name": "approved"}, gh.snapshot["labels"])

    def test_comment_review_still_stops_for_new_commit(self):
        def new_commit(job, gh):
            gh.snapshot["head"]["sha"] = "b" * 40

        job, gh, result, context = self.comment_review(new_commit)
        with self.assertRaises(StaleReview):
            publish(gh, job, result, context, {"passed": True, "summary": "Pass"})
        self.assertNotIn({"name": "approved"}, gh.snapshot["labels"])

    def test_comment_review_still_stops_for_human_label_override(self):
        def human_override(job, gh):
            gh.snapshot["labels"].append({"name": "feedback"})
            gh.timeline_event("labeled", "feedback", actor="louis")

        job, gh, result, context = self.comment_review(human_override)
        with self.assertRaises(StaleReview):
            publish(gh, job, result, context, {"passed": True, "summary": "Pass"})
        self.assertNotIn({"name": "approved"}, gh.snapshot["labels"])

    def test_same_account_human_override_between_own_label_writes_stops(self):
        job, gh = self.prepare()
        original = gh.post

        def post(path, data):
            response = original(path, data)
            if path.endswith("/labels") and "approved" in data["labels"]:
                gh.snapshot["labels"].append({"name": "feedback"})
                gh.timeline_event("labeled", "feedback", actor="louis")
            return response

        gh.post = post
        with self.assertRaises(StaleReview):
            publish(gh, job, self.result(), {}, {"passed": True, "summary": "Pass"})
        self.assertIn("feedback", [label["name"] for label in gh.snapshot["labels"]])

    def test_single_guard_rechecks_admin_cancellation_after_network(self):
        from core.publication import guard

        job, gh = self.prepare()
        original = gh.pull

        def pull(number):
            Job.objects.filter(pk=job.pk).update(status="cancelled")
            return original(number)

        gh.pull = pull
        with self.assertRaises(StaleReview):
            guard(gh, job)

    def test_unrelated_human_label_does_not_cancel_publication(self):
        job, gh = self.prepare()
        original = gh.pages

        def pages(path):
            if path.endswith("/reviews"):
                gh.snapshot["labels"].append({"name": "bug"})
                gh.timeline_event("labeled", "bug", actor="developer")
            return original(path)

        gh.pages = pages
        self.assertEqual(
            publish(gh, job, self.result(), {}, {"passed": True, "summary": "Pass"}), "ready"
        )
        self.assertIn("bug", [label["name"] for label in gh.snapshot["labels"]])
