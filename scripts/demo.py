#!/usr/bin/env python3
"""Run the real dashboard with disposable fictional data, never production data."""

import argparse
import os
import socket
import sys
import tempfile
from pathlib import Path
from wsgiref.simple_server import make_server

ROOT = Path(__file__).resolve().parent.parent


def seed():
    import uuid
    from datetime import timedelta

    from django.contrib.auth import get_user_model
    from django.utils import timezone

    from core.models import (
        Credential,
        Job,
        JobActivity,
        Membership,
        ModelAttempt,
        Organization,
        PRAssignee,
        Provider,
        PullRequest,
        Repository,
        ServiceSettings,
    )
    from core.secrets import encrypt

    owner = get_user_model().objects.create_superuser("demo", password="demo-review-only")
    organization = Organization.objects.create(name="Northstar · Demo workspace", slug="demo")
    Membership.objects.create(user=owner, organization=organization)
    credential = Credential.objects.create(
        name="Demo connection · not a real token", encrypted_value=encrypt("fictional-demo-only")
    )
    provider = Provider.objects.create(name="Codex · primary", kind="codex_cli", model="demo-model")
    Provider.objects.create(
        name="Claude Code · fallback", kind="claude_cli", model="demo-fallback", order=1
    )
    repositories = [
        Repository.objects.create(
            organization=organization,
            full_name=f"northstar-demo/{name}",
            reviewer="review-bot-demo",
            github_credential=credential,
            webhook_credential=credential,
            graph_enabled=False,
            enabled=name != "design-system",
            required_checks=["unit-tests", "typecheck"],
        )
        for name in ("payments-api", "web-app", "design-system")
    ]
    ServiceSettings.objects.update_or_create(
        pk=1, defaults={"concurrent_pr_reviews": 2, "cli_account_concurrency": 4}
    )
    now = timezone.now()
    from core.quota import store_snapshot

    for kind, used in (("codex_cli", 24), ("claude_cli", 39)):
        store_snapshot(
            kind,
            {
                "windows": [
                    {
                        "name": "primary",
                        "used_percent": used,
                        "resets_at": int((now + timedelta(hours=3)).timestamp()),
                    }
                ]
            },
        )
    examples = [
        (0, 248, "running", "maya"),
        (1, 126, "running", "alex"),
        (0, 247, "queued", "sam"),
        (1, 125, "queued", "maya"),
        (0, 246, "completed", "alex"),
        (1, 124, "completed", "sam"),
        (2, 83, "completed", "maya"),
        (0, 245, "completed", "sam"),
    ]
    for index, (repo_index, number, status, actor) in enumerate(examples):
        start = now - timedelta(minutes=3 + index * 8)
        finish = start + timedelta(seconds=116) if status == "completed" else None
        sha = uuid.uuid5(uuid.NAMESPACE_URL, f"demo/{number}").hex + "d00df00d"
        pr = PullRequest.objects.create(
            repository=repositories[repo_index],
            number=number,
            head_sha=sha,
            requested=True,
            labels=["on review"],
            generation=1,
            assignees_synced_at=now,
        )
        PRAssignee.objects.create(pr=pr, github_id=9000000000 + index, login=actor)
        job = Job.objects.create(
            id=uuid.uuid5(uuid.NAMESPACE_URL, f"demo-job/{number}"),
            pr=pr,
            generation=1,
            head_sha=sha,
            actor=actor,
            status=status,
            provider=provider if status != "queued" else None,
            started_at=start if status != "queued" else None,
            finished_at=finish,
            heartbeat_at=now,
        )
        Job.objects.filter(pk=job.pk).update(created_at=start - timedelta(seconds=20))
        if status == "queued":
            continue
        attempt = ModelAttempt.objects.create(
            job=job,
            provider=provider,
            provider_name=provider.name,
            model="demo-model",
            status="success" if finish else "running",
            finished_at=finish,
        )
        ModelAttempt.objects.filter(pk=attempt.pk).update(started_at=start)
        events = [
            ("starting", None),
            ("context", None),
            ("files", 14),
            ("threads", None),
            ("related", None),
            ("docs", 4),
            ("context_ready", None),
            ("checks", None),
            ("checks_passed", None),
            ("model", None),
            ("intent_started", None),
            ("intent_ready", None),
            ("code_started", None),
            ("code_ready", None),
            ("security_started", None),
        ]
        if finish:
            events += [
                ("security_ready", None),
                ("tests_started", None),
                ("tests_ready", None),
                ("synthesis_started", None),
                ("synthesis_ready", None),
                ("result", None),
                ("findings", 2),
                ("recheck", None),
                ("publishing", None),
                ("summary_done", 1),
                ("inline_done", 2),
                ("discussion_unused", 0),
                ("related_pr_unused", 0),
                ("related_issue_unused", 0),
                ("labels_done", 1),
                ("published", None),
                ("ended", None),
            ]
        for order, (code, count) in enumerate(events):
            event = JobActivity.objects.create(job=job, code=code, count=count)
            JobActivity.objects.filter(pk=event.pk).update(
                created_at=start + timedelta(seconds=order * 3)
            )
        if not finish:
            for source, detail in [
                ("Code reviewer", "Tracing the changed request handler and its callers."),
                (
                    "Security reviewer",
                    "Checking authorization and tenant boundaries in the affected paths.",
                ),
                ("Review orchestrator", "Test review follows the security assessment."),
            ]:
                event = JobActivity.objects.create(
                    job=job, code="cli_output_text", source=source, detail=detail
                )
                JobActivity.objects.filter(pk=event.pk).update(
                    created_at=start + timedelta(seconds=70)
                )
    return owner.pk


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18041)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Use an unprivileged port between 1024 and 65535.")
    sys.path.insert(0, str(ROOT))
    (ROOT / "var").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fictional-demo-", dir=ROOT / "var") as directory:
        # Replace inherited application settings; never read the deployment .env.
        for line in (ROOT / ".env.example").read_text().splitlines():
            key, sep, _ = line.partition("=")
            if sep and not key.startswith("#"):
                os.environ.pop(key, None)
        from cryptography.fernet import Fernet

        os.environ.update(
            DJANGO_SETTINGS_MODULE="reviewdesk.settings",
            DJANGO_SECRET_KEY="fictional-demo-not-for-deployment",
            CREDENTIAL_KEY=Fernet.generate_key().decode(),
            DATA_DIR=directory,
            DB_ENGINE="sqlite",
            LOCAL_HTTP="1",
            PUBLIC_URL=f"http://127.0.0.1:{args.port}",
            ALLOWED_HOSTS="127.0.0.1,localhost",
            LIVE_WRITES="0",
            TIME_ZONE="Asia/Jakarta",
        )
        import django

        django.setup()
        from django.conf import settings
        from django.contrib.staticfiles.handlers import StaticFilesHandler
        from django.core.management import call_command
        from django.core.wsgi import get_wsgi_application

        call_command("migrate", verbosity=0, interactive=False)
        settings.DASHBOARD_RETRY_USER_ID = seed()
        settings.STORAGES = {
            "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
        }

        def offline(*args, **kwargs):
            raise OSError("Fictional demo: outbound connections are disabled.")

        socket.socket.connect = offline
        socket.socket.connect_ex = offline
        app = StaticFilesHandler(get_wsgi_application())
        print(
            f"Fictional demo: http://127.0.0.1:{args.port}\nSign in: demo / demo-review-only\nDisposable database; no workers, Docker runners or outbound connections. Ctrl+C to stop.",
            flush=True,
        )

        def local_app(environ, start_response):
            def headers(status, response_headers, exc_info=None):
                response_headers.append(
                    (
                        "Content-Security-Policy",
                        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; connect-src 'self'",
                    )
                )
                return start_response(status, response_headers, exc_info)

            return app(environ, headers)

        with make_server("127.0.0.1", args.port, local_app) as server:
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
