from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from django.test import TestCase
from django.utils import timezone

from core.github import GitHub, IntegrationError


class GitHubLimitsTests(TestCase):
    def github_client(self, handler, token="private-token", repository="org/repo"):
        repo = SimpleNamespace(
            full_name=repository, github_credential=SimpleNamespace(encrypted_value="encrypted")
        )
        with patch("core.github.decrypt", return_value=token):
            github = GitHub(repo)
        github.client.close()
        github.client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(github.close)
        return github

    def test_primary_limit_persists_shared_token_cooldown_and_prevents_next_request(self):
        from core.github import GitHubRateLimited
        from core.models import GitHubCooldown

        reset = timezone.now() + timedelta(minutes=30)
        github = self.github_client(
            lambda req: httpx.Response(
                403,
                headers={
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": str(int(reset.timestamp())),
                },
                json={"message": "private-token"},
            )
        )
        with self.assertRaises(GitHubRateLimited) as error:
            github.get("/issues")
        self.assertEqual(error.exception.kind, "primary")
        self.assertGreaterEqual(error.exception.retry_at, reset)
        row = GitHubCooldown.objects.get()
        self.assertEqual(len(row.scope), 64)
        self.assertNotIn("private-token", str(error.exception))
        other = self.github_client(
            lambda req: self.fail("shared cooldown must block HTTP"), repository="other/repo"
        )
        with self.assertRaises(GitHubRateLimited):
            other.get("/issues")

    def test_permission_403_is_not_a_limit_and_does_not_persist_response_body(self):
        from core.github import GitHubRateLimited
        from core.models import GitHubCooldown

        github = self.github_client(
            lambda req: httpx.Response(
                403, json={"message": "Resource not accessible private-token"}
            )
        )
        with self.assertRaises(IntegrationError) as error:
            github.get("/issues")
        self.assertNotIsInstance(error.exception, GitHubRateLimited)
        self.assertEqual(str(error.exception), "GitHub request failed (HTTP 403).")
        self.assertFalse(GitHubCooldown.objects.exists())

    def test_secondary_429_honors_retry_after_without_leaking_body(self):
        from core.github import GitHubRateLimited

        now = timezone.now()
        github = self.github_client(
            lambda req: httpx.Response(
                429, headers={"Retry-After": "7200"}, json={"message": "private-token secret"}
            )
        )
        with self.assertRaises(GitHubRateLimited) as error:
            github.get("/issues")
        self.assertEqual(error.exception.kind, "secondary")
        self.assertGreaterEqual(error.exception.retry_at, now + timedelta(hours=2))
        self.assertNotIn("private-token", str(error.exception))

    def test_secondary_message_and_http_date_retry_after(self):
        from email.utils import format_datetime

        from core.github import GitHubRateLimited

        retry_at = timezone.now() + timedelta(minutes=10)
        github = self.github_client(
            lambda req: httpx.Response(
                403,
                headers={"Retry-After": format_datetime(retry_at)},
                json={"message": "You have exceeded a secondary rate limit private-token"},
            )
        )
        with self.assertRaises(GitHubRateLimited) as error:
            github.get("/issues")
        self.assertEqual(error.exception.kind, "secondary")
        self.assertGreaterEqual(error.exception.retry_at, retry_at)

    def test_secondary_body_without_headers_uses_at_least_one_minute(self):
        from core.github import GitHubRateLimited

        now = timezone.now()
        github = self.github_client(
            lambda req: httpx.Response(
                403, json={"message": "You have triggered an abuse detection mechanism."}
            )
        )
        with self.assertRaises(GitHubRateLimited) as error:
            github.get("/issues")
        self.assertEqual(error.exception.kind, "secondary")
        self.assertGreaterEqual(error.exception.retry_at, now + timedelta(seconds=60))

    def test_invalid_rate_headers_have_safe_minimum_and_different_tokens_do_not_share(self):
        from core.github import GitHubRateLimited

        now = timezone.now()
        github = self.github_client(
            lambda req: httpx.Response(
                403,
                headers={
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": "not-a-date",
                    "Retry-After": "NaN",
                },
            )
        )
        with self.assertRaises(GitHubRateLimited) as error:
            github.get("/issues")
        self.assertGreaterEqual(error.exception.retry_at, now + timedelta(seconds=60))
        other = self.github_client(
            lambda req: httpx.Response(200, json={"ok": True}), token="different-token"
        )
        self.assertEqual(other.get("/issues"), {"ok": True})

    def test_expired_cooldown_ignored_and_longer_existing_deadline_not_shortened(self):
        from core import github_limits
        from core.models import GitHubCooldown

        token_scope = github_limits.scope("private-token")
        GitHubCooldown.objects.create(
            scope=token_scope, retry_at=timezone.now() - timedelta(seconds=1), kind="primary"
        )
        github = self.github_client(lambda req: httpx.Response(200, json={"ok": True}))
        self.assertEqual(github.get("/issues"), {"ok": True})
        longer = timezone.now() + timedelta(hours=5)
        github_limits.remember(token_scope, longer, "primary")
        row = github_limits.remember(
            token_scope, timezone.now() + timedelta(minutes=1), "secondary"
        )
        self.assertEqual(row.retry_at, longer)
        self.assertEqual(row.kind, "primary")

    def test_success_exhausting_budget_is_returned_then_next_request_waits(self):
        from core.github import GitHubRateLimited

        github = self.github_client(
            lambda req: httpx.Response(
                200,
                headers={
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": str(
                        int((timezone.now() + timedelta(minutes=15)).timestamp())
                    ),
                },
                json={"ok": True},
            )
        )
        self.assertEqual(github.get("/issues"), {"ok": True})
        with self.assertRaises(GitHubRateLimited):
            github.get("/issues")

    def test_graphql_http_200_rate_error_becomes_shared_cooldown(self):
        from core.github import GitHubRateLimited

        github = self.github_client(
            lambda req: httpx.Response(
                200, json={"errors": [{"type": "RATE_LIMITED", "message": "private-token"}]}
            )
        )
        with self.assertRaises(GitHubRateLimited):
            github.graphql("query{}", {})
        with self.assertRaises(GitHubRateLimited):
            github.get("/issues")

    def test_archive_api_limit_is_classified(self):
        from core.github import GitHubRateLimited

        github = self.github_client(lambda req: httpx.Response(429, headers={"retry-after": "120"}))
        with self.assertRaises(GitHubRateLimited):
            github.archive("a" * 40)

    def test_ci_and_context_rate_limit_exceptions_are_not_reclassified_as_incomplete(self):
        from unittest.mock import Mock

        from core.context import build_context
        from core.github import GitHubRateLimited, ci_snapshot

        error = GitHubRateLimited(timezone.now() + timedelta(minutes=1))
        github = Mock()
        github.pages.side_effect = error
        repo = SimpleNamespace(required_checks=["tests"], related_item_limit=50, instructions="")
        with self.assertRaises(GitHubRateLimited):
            ci_snapshot(github, repo, "a" * 40)
        github.pull.side_effect = error
        job = SimpleNamespace(pr=SimpleNamespace(repository=repo, number=1), head_sha="a" * 40)
        with self.assertRaises(GitHubRateLimited):
            build_context(github, job)

    def test_error_body_read_is_bounded_even_when_secondary_marker_is_beyond_limit(self):
        from core.models import GitHubCooldown

        class Stream(httpx.SyncByteStream):
            def __iter__(stream):
                yield b'{"message":"' + b"x" * (16 * 1024)
                self.fail("error classification read beyond bounded body")

        github = self.github_client(lambda req: httpx.Response(403, stream=Stream()))
        with self.assertRaises(IntegrationError):
            github.get("/issues")
        self.assertFalse(GitHubCooldown.objects.exists())

    def test_decisive_rate_headers_survive_broken_response_body(self):
        from core.github import GitHubRateLimited

        class BrokenStream(httpx.SyncByteStream):
            def __iter__(self):
                raise httpx.ReadError("private-token response interrupted")

        for status, headers in [
            (429, {"Retry-After": "120"}),
            (403, {"x-ratelimit-remaining": "0"}),
        ]:
            with self.subTest(status=status):
                github = self.github_client(
                    lambda req: httpx.Response(status, headers=headers, stream=BrokenStream()),
                    token=f"token-{status}",
                )
                with self.assertRaises(GitHubRateLimited):
                    github.get("/issues")
