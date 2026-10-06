"""Single reviewer; event reconciler runs separately so cancellation stays responsive."""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from core.activity import Progress
from core.models import Job, Provider
from core.publication import StaleReview, fail_to_feedback, guard, once, publish
from core.workflow import finish


def run_job(job):
    from core.context import build_context, failure_reason
    from core.github import GitHub, IntegrationError, check_required
    from core.providers import ProviderError, ReviewCancelled, review
    from core.runner import run_tests
    from core.sheets import sync_status

    github = None
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
        progress("checks_passed" if checks["passed"] else "checks_blocked")
        repo = job.pr.repository
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
        if job.provider_id:
            providers = [p for p in providers if p.pk == job.provider_id]
        if not providers:
            finish(job, "attention", "No enabled model connection. Admin configuration required.")
            return
        result = None
        for provider in providers:
            guard(github, job)
            Job.objects.filter(pk=job.pk, status="running").update(provider=provider)
            try:
                progress("model")
                result = review(provider, context, progress.cancelled)
                progress("result")
                progress("findings", count=len(result.get("findings", [])))
                break
            except ProviderError as exc:
                if exc.kind == "unavailable":
                    progress("unavailable")
                    continue
                if exc.kind == "quota":
                    progress("quota")
                    finish(
                        job,
                        "quota",
                        "Provider quota exhausted. Next time is an availability check.",
                        retry_at=exc.retry_at or timezone.now() + timedelta(minutes=30),
                    )
                else:
                    progress("model_attention")
                    finish(
                        job,
                        "attention",
                        "Provider refused the review; admin inspection required."
                        if exc.kind == "attention"
                        else "Model connection or response needs admin attention.",
                    )
                return
        if result is None:
            raise IntegrationError("No configured provider is available.")
        if repo.required_checks:
            progress("recheck")
            latest = check_required(github, repo, job.head_sha)
            checks = {"passed": checks["passed"] and latest["passed"], "summary": latest["summary"]}
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
        finish(job, "superseded", "Attempt cancelled or commit changed; request a new review.")
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
        progress.close()
        if github is not None and hasattr(github, "close"):
            github.close()
        progress("ended")


def recover_interrupted():
    # Never replay uncertain external publication automatically after a process crash.
    Job.objects.filter(status="running").update(
        status="attention",
        reason="Worker restarted during review. Inspect GitHub, then retoggle to retry.",
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
