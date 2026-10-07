"""Single reviewer; event reconciler runs separately so cancellation stays responsive."""

import time
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from core.activity import Progress
from core.models import Job, ModelAttempt, Provider
from core.publication import StaleReview, fail_to_feedback, guard, once, publish, thread_replies
from core.workflow import finish


def run_job(job):
    from core.context import build_context, failure_reason
    from core.github import GitHub, IntegrationError, check_required
    from core.providers import ProviderError, ReviewCancelled, failure_description, review
    from core.quota import blocked_until, record_exhausted
    from core.runner import run_tests
    from core.sheets import sync_status

    github = None
    model_attempt = None
    result = None
    outcome = None
    diagnostic = "Review incomplete. Check connections and required checks; request a new review."
    progress = Progress(job)
    progress.start()
    try:
        progress("starting")
        github = GitHub(job.pr.repository)
        guard(github, job)
        if not settings.LIVE_WRITES:
            finish(
                job,
                "attention",
                "Publishing disabled. Admin must enable LIVE_WRITES before a new request.",
            )
            return
        progress("context")
        context = build_context(github, job, progress=progress)
        if not context.get("complete", False):
            diagnostic = failure_reason(context)
            progress("context_failed")
            raise IntegrationError("Context collection incomplete")
        progress("context_ready")
        progress("checks")
        checks = check_required(github, job.pr.repository, job.head_sha)
        if checks.get("pending"):
            progress("checks_waiting")
        while checks.get("pending"):
            # No whole-review timeout; keep admin/commit cancellation responsive.
            for _ in range(15):
                if progress.cancelled():
                    raise ReviewCancelled()
                time.sleep(1)
            guard(github, job)
            checks = check_required(github, job.pr.repository, job.head_sha)
        progress("checks_passed" if checks["passed"] else "checks_blocked")
        repo = job.pr.repository
        tests = None
        if repo.test_command:
            guard(github, job)
            progress("tests")
            tests = run_tests(repo, github.archive(job.head_sha), progress.cancelled)
            progress("tests_passed" if tests["passed"] else "tests_failed")
            checks = {
                "passed": tests["passed"] and (checks["passed"] if repo.required_checks else True),
                "summary": checks["summary"] + " " + tests["summary"],
            }
        context["checks"] = checks
        context["workflow"] = {
            "reviewer": repo.reviewer,
            "review_label": repo.review_label,
            "feedback_label": repo.feedback_label,
            "approved_label": repo.approved_label,
        }
        providers = list(Provider.objects.filter(enabled=True))
        if job.provider_id and not job.provider.kind.endswith("_cli"):
            # A quota retry stays on its provider, then may fall forward on refusal/outage.
            index = next((i for i, p in enumerate(providers) if p.pk == job.provider_id), None)
            providers = providers[index:] if index is not None else []
        if not providers:
            finish(job, "attention", "No enabled model connection. Admin configuration required.")
            return
        if not job.provider_plan:
            job.provider_plan = [
                {"id": p.pk, "name": p.name, "model": p.model, "kind": p.kind} for p in providers
            ]
            Job.objects.filter(pk=job.pk, status="running").update(provider_plan=job.provider_plan)
        result = None
        quota_waits = []
        for provider in providers:
            guard(github, job)
            exhausted_until = blocked_until(provider)
            if exhausted_until:
                quota_waits.append(exhausted_until)
                ModelAttempt.objects.create(
                    job=job,
                    provider=provider,
                    provider_name=provider.name,
                    model=provider.model,
                    status="skipped",
                    finished_at=timezone.now(),
                    diagnostics={
                        "failure_kind": "quota",
                        "failure_detail": "cli_quota",
                        "retry_at": exhausted_until.isoformat(),
                    },
                )
                progress("model_skipped")
                continue
            Job.objects.filter(pk=job.pk, status="running").update(
                provider=provider, provider_diagnostics={}
            )
            model_attempt = ModelAttempt.objects.create(
                job=job, provider=provider, provider_name=provider.name, model=provider.model
            )

            cli_state = {}

            def save_diagnostics(data):
                from .activity import cli_progress

                cli_progress(progress, data, cli_state)
                Job.objects.filter(pk=job.pk, status="running").update(provider_diagnostics=data)
                ModelAttempt.objects.filter(pk=model_attempt.pk).update(diagnostics=data)

            def save_activity(code, text):
                from .activity import record

                record(job, code, detail=text, source=provider.name)

            try:
                progress("model")
                result = review(
                    provider,
                    context,
                    progress.cancelled,
                    on_response=save_diagnostics,
                    on_activity=save_activity,
                )
                try:
                    thread_replies(result, context)
                except (ValueError, KeyError, TypeError):
                    raise ProviderError("invalid", detail="output_invalid") from None
                ModelAttempt.objects.filter(pk=model_attempt.pk).update(
                    status="success", finished_at=timezone.now()
                )
                progress("result")
                progress("findings", count=len(result.get("findings", [])))
                break
            except ProviderError as exc:
                # A parsed result can still fail thread validation. Never retain it
                # across fallback or publish it when every provider has failed.
                result = None
                metadata = ModelAttempt.objects.get(pk=model_attempt.pk).diagnostics
                metadata.update(failure_kind=exc.kind, failure_detail=exc.detail)
                save_diagnostics(metadata)
                incomplete = exc.detail == "cli_coverage"
                review_failed = exc.kind == "invalid" and exc.detail in {
                    "cli_protocol",
                    "output_invalid",
                    "output_incomplete",
                    "response_too_large",
                    "cli_memory",
                    "context_too_large",
                    "model_not_found",
                }
                state = (
                    "incomplete"
                    if incomplete
                    else "failed"
                    if review_failed
                    else {
                        "attention": "refused",
                        "unavailable": "unavailable",
                        "quota": "quota",
                    }.get(exc.kind, "attention")
                )
                ModelAttempt.objects.filter(pk=model_attempt.pk).update(
                    status=state, finished_at=timezone.now()
                )
                if incomplete or review_failed or exc.kind in ("unavailable", "attention"):
                    progress(
                        "model_incomplete"
                        if incomplete
                        else "model_failed"
                        if review_failed
                        else "unavailable"
                        if exc.kind == "unavailable"
                        else "model_refused"
                    )
                    continue
                if exc.kind == "quota":
                    progress("quota")
                    if provider.kind.endswith("_cli"):
                        quota_waits.append(record_exhausted(provider, exc.retry_at))
                        progress("quota_fallback")
                        continue
                    finish(
                        job,
                        "quota",
                        failure_description(metadata, state),
                        retry_at=exc.retry_at or timezone.now() + timedelta(minutes=30),
                    )
                else:
                    progress("model_attention")
                    finish(
                        job,
                        "attention",
                        failure_description(metadata, state),
                    )
                return
        if result is None:
            if quota_waits:
                finish(
                    job,
                    "quota",
                    "No usable model remains. Exhausted subscription accounts were skipped; waiting for the next availability check.",
                    retry_at=min(quota_waits),
                )
                return
            progress("models_exhausted")
            finish(
                job,
                "attention",
                "All remaining models declined, were unavailable, failed to return a valid review, or returned incomplete evidence. Inspect model attempts before requesting another review.",
            )
            return
        if repo.required_checks:
            progress("recheck")
            latest = check_required(github, repo, job.head_sha)
            progress("checks_passed" if latest["passed"] else "checks_blocked")
            checks = {
                "passed": latest["passed"] and (tests is None or tests["passed"]),
                "summary": latest["summary"] + (" " + tests["summary"] if tests else ""),
            }
        progress("publishing")
        outcome = publish(github, job, result, context, checks)
        progress("published")
        finish(job, "completed")
        try:
            if repo.sheet_config:
                progress("sheet")
            sync_status(
                github,
                repo,
                job.pr.number,
                outcome,
                before_write=lambda: completed_guard(github, job, outcome),
            )
        except Exception:
            progress("sheet_attention")
            Job.objects.filter(pk=job.pk, status="completed").update(
                reason="Review published; Sheet update needs admin attention."
            )
    except (StaleReview, ReviewCancelled):
        finish(
            job,
            "superseded",
            "Attempt cancelled, commit or discussion changed; request a new review.",
        )
    except Exception:
        # Exception messages can contain HTTP response content or credentials: never persist them.
        try:
            guard(github, job)
            once(
                github,
                job,
                "failure",
                f"Review could not complete. {diagnostic} No approval was granted. After resolving the problem, request @{job.pr.repository.reviewer} and toggle `{job.pr.repository.feedback_label}` → `{job.pr.repository.review_label}`.",
                f"/issues/{job.pr.number}/comments",
                f"/issues/{job.pr.number}/comments",
            )
            fail_to_feedback(github, job)
        except Exception:
            pass
        finish(
            job,
            "failed",
            diagnostic,
        )

    finally:
        if model_attempt is not None:
            ModelAttempt.objects.filter(pk=model_attempt.pk, status="running").update(
                status=(
                    "cancelled"
                    if Job.objects.filter(
                        pk=job.pk, status__in=["cancelled", "superseded"]
                    ).exists()
                    else "attention"
                ),
                finished_at=timezone.now(),
            )
        progress.close()
        if github is not None and hasattr(github, "close"):
            github.close()
        progress("ended")
        try:
            from core.journal import queue_job

            if queue_job(job, result, outcome):
                progress("journal_queued")
        except Exception:
            # Journaling never changes the GitHub outcome or exposes payload/credential errors.
            try:
                progress("journal_attention")
            except Exception:
                pass


def recover_interrupted():
    # Never replay uncertain external publication automatically after a process crash.
    Job.objects.filter(status="running").update(
        status="attention",
        reason="Worker restarted during review. Inspect GitHub, then retoggle to retry.",
    )
    ModelAttempt.objects.filter(status="running").update(
        status="interrupted", finished_at=timezone.now()
    )


def completed_guard(github, job, outcome):
    row = Job.objects.select_related("pr__repository").get(pk=job.pk)
    fresh = github.pull(job.pr.number)
    expected = (
        row.pr.repository.feedback_label
        if outcome == "feedback"
        else row.pr.repository.approved_label
    )
    if (
        row.status != "completed"
        or row.generation != row.pr.generation
        or not row.pr.repository.enabled
        or fresh["head"]["sha"] != job.head_sha
        or expected not in {x["name"] for x in fresh.get("labels", [])}
    ):
        raise StaleReview("Review no longer authoritative for task status")
