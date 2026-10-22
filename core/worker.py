"""Single reviewer; event reconciler runs separately so cancellation stays responsive."""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from core.activity import Progress
from core.capacity import CapacityError, cli_limit
from core.claims import admission, attempt_rows, create_attempt, fenced, job_rows, owned
from core.models import Job, ModelAttempt, Receipt, ReviewCheckpoint
from core.publication import (
    LabelCleanupError,
    StaleReview,
    fail_to_feedback,
    guard,
    once,
    publish,
    thread_replies,
)
from core.workflow import finish
from core.workflow_schema import GROUNDED_REVISIONS, TEAM_REVISIONS


def run_job(job):
    """Keep the execution lock through every finalizer, including direct callers."""
    from .claims import execution_job, release

    token = execution_job.set(job)
    try:
        _run_job(job)
    except BaseException:
        raise
    else:
        release(job)
    finally:
        execution_job.reset(token)


def _run_job(job):
    from core.context import build_context, failure_reason
    from core.delivery import DeliveryPaused, load_review, save_review
    from core.github import GitHub, IntegrationError, ci_snapshot
    from core.providers import (
        ProviderError,
        ReviewCancelled,
        failure_description,
        review,
        validation_diagnostics,
    )
    from core.quota import blocked_until, record_exhausted
    from core.runner import run_tests

    github = None
    model_attempt = None
    result = None
    outcome = None
    diagnostic = "Review incomplete. Check connections and required checks; request a new review."
    progress = Progress(job)
    grounded = job.workflow_revision in GROUNDED_REVISIONS
    team = job.workflow_revision in TEAM_REVISIONS
    progress.start()
    try:
        progress("starting")
        github = GitHub(job.pr.repository)
        if ReviewCheckpoint.objects.filter(job=job).exists():
            if not settings.LIVE_WRITES:
                finish(job, "attention", "Publishing disabled; saved review retained.")
                return
            _, saved = load_review(job)
            result = saved["result"]
            progress("delivery_resume")
            outcome = _deliver_and_finish(github, job, progress)
            return
        guard(github, job)
        if not settings.LIVE_WRITES:
            finish(
                job,
                "attention",
                "Publishing disabled. Admin must enable LIVE_WRITES before a new request.",
            )
            return
        progress("context")
        context = build_context(github, job, progress=progress, cancelled=progress.cancelled)
        if not context.get("complete", False):
            diagnostic = failure_reason(context)
            progress("context_failed")
            raise IntegrationError("Context collection incomplete")
        progress("context_ready")
        progress("checks")
        checks = ci_snapshot(github, job.pr.repository, job.head_sha)
        progress("checks_passed" if checks["passed"] else "checks_blocked")
        repo = job.pr.repository
        tests = None
        if repo.test_command:
            guard(github, job)
            progress("tests")
            tests = run_tests(
                repo,
                github.archive(job.head_sha),
                progress.cancelled,
                on_capacity=lambda waiting: progress(
                    "local_tests_waiting" if waiting else "local_tests_resumed"
                ),
            )
            progress("tests_passed" if tests["passed"] else "tests_failed")
            checks = {
                "passed": checks["passed"],
                "local_passed": tests["passed"],
                "summary": checks["summary"] + " " + tests["summary"],
            }
        context["checks"] = checks
        context["workflow"] = {
            "reviewer": repo.reviewer,
            "review_label": repo.review_label,
            "feedback_label": repo.feedback_label,
            "approved_label": repo.approved_label,
        }
        from core.review_config import providers_for_job

        providers = providers_for_job(job)
        prior_provider = next((p for p in providers if p.pk == job.provider_id), None)
        if prior_provider and not prior_provider.kind.endswith("_cli"):
            # A quota retry stays on its provider, then may fall forward on refusal/outage.
            index = next((i for i, p in enumerate(providers) if p.pk == job.provider_id), None)
            providers = providers[index:] if index is not None else []
        if not providers:
            finish(job, "attention", "No enabled model connection. Admin configuration required.")
            return
        result = None
        quota_waits = []
        phase_checkpoints = None
        if team:
            from .phase_checkpoints import PhaseCheckpoints

            phase_checkpoints = PhaseCheckpoints(job, context)
        for provider in providers:
            guard(github, job)
            exhausted_until = blocked_until(provider)
            if exhausted_until:
                quota_waits.append(exhausted_until)
                create_attempt(
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
            job_rows(job).update(provider=provider, provider_diagnostics={})
            model_attempt = create_attempt(
                job=job, provider=provider, provider_name=provider.name, model=provider.model
            )

            cli_state = {}
            phase = "intent" if grounded else "review"
            phase_reports = {}
            phase_history = {}

            @fenced()
            def team_progress_impl(job, code):
                progress(code)
                name, _, event = code.rpartition("_")
                if name in {
                    "intent",
                    "review",
                    "code",
                    "security",
                    "tests",
                    "synthesis",
                } | {row["key"] for row in job.review_agents if row["enabled"]} and event in {
                    "started",
                    "ready",
                    "incomplete",
                    "waiting",
                    "resumed",
                }:
                    now = timezone.now().isoformat()
                    row = phase_history.setdefault(name, {"started_at": None, "finished_at": None})
                    row["state"] = (
                        "running"
                        if event in {"started", "resumed"}
                        else "waiting"
                        if event == "waiting"
                        else "done"
                        if event == "ready"
                        else "incomplete"
                    )
                    if event == "started":
                        row["started_at"] = now
                    elif event in {"ready", "incomplete"}:
                        row["finished_at"] = now
                    data = ModelAttempt.objects.get(pk=model_attempt.pk).diagnostics
                    data["phase_timeline"] = [
                        {"phase": key, **value} for key, value in phase_history.items()
                    ]
                    attempt_rows(job, pk=model_attempt.pk).update(diagnostics=data)
                    job_rows(job).update(provider_diagnostics=data)

            def team_progress(code):
                return team_progress_impl(job, code)

            @fenced()
            def save_diagnostics_impl(job, data):
                from .activity import cli_progress
                from .usage import usage_counters

                cli_progress(progress, data, cli_state)
                if grounded:
                    passes = data.get("usage_passes")
                    if not isinstance(passes, list) or not passes:
                        passes = [data]
                    phase_reports[phase] = [usage_counters(p) for p in passes[:2]]
                    data = {
                        **data,
                        "phase": "specialists"
                        if team
                        and (not provider.kind.endswith("_cli") or cli_limit() > 1)
                        and phase in {row["key"] for row in job.review_agents if row["enabled"]}
                        else phase,
                        "phase_usage": [
                            {"phase": name, "passes": values}
                            for name, values in phase_reports.items()
                        ],
                    }
                if team:
                    data["phase_timeline"] = [
                        {"phase": name, **values} for name, values in phase_history.items()
                    ]
                job_rows(job).update(provider_diagnostics=data)
                attempt_rows(job, pk=model_attempt.pk).update(diagnostics=data)

            def save_diagnostics(data):
                return save_diagnostics_impl(job, data)

            def save_activity(code, text):
                from .activity import record

                source = (
                    f"{provider.name[: min(80, 97 - len(phase))]} / {phase}"
                    if team
                    else provider.name
                )
                record(job, code, detail=text, source=source)

            def team_response(name, data):
                nonlocal phase
                if phase != name:
                    cli_state.clear()
                phase = name
                save_diagnostics(data)

            def team_activity(name, code, text):
                nonlocal phase
                phase = name
                save_activity(code, text)

            try:
                review_context = context
                if grounded:
                    from .review_v2 import intent_context, validate_actions, validate_intent

                    team_progress("intent_started")
                    intent = phase_checkpoints.load("intent") if phase_checkpoints else None
                    if intent is not None:
                        progress("phase_reused")
                    else:
                        save_diagnostics({})
                        intent = review(
                            provider,
                            intent_context(context),
                            progress.cancelled,
                            on_response=save_diagnostics,
                            on_activity=save_activity,
                            task="intent",
                            on_capacity=lambda waiting: team_progress(
                                "intent_waiting" if waiting else "intent_resumed"
                            ),
                        )
                    try:
                        validate_intent(intent, context)
                    except (ProviderError, ValueError, KeyError, TypeError) as exc:
                        raise ProviderError(
                            "invalid",
                            detail="output_invalid",
                            **validation_diagnostics(exc),
                        ) from None
                    if phase_checkpoints and "intent" not in phase_checkpoints.sources:
                        phase_checkpoints.save("intent", intent, provider)
                    team_progress("intent_ready")
                    guard(github, job)
                    if not team:
                        phase = "review"
                        cli_state.clear()
                        save_diagnostics({})
                    review_context = {**context, "intent": intent}
                progress("model")
                if team:
                    from .review_orchestration import run_team

                    result, audit = run_team(
                        provider,
                        review_context,
                        progress.cancelled,
                        call=review,
                        agents=job.review_agents,
                        on_response=team_response,
                        on_activity=team_activity,
                        progress=team_progress,
                        checkpoints=phase_checkpoints,
                    )
                else:
                    result = review(
                        provider,
                        review_context,
                        progress.cancelled,
                        on_response=save_diagnostics,
                        on_activity=save_activity,
                        on_capacity=lambda waiting: team_progress(
                            "review_waiting" if waiting else "review_resumed"
                        ),
                        **({"task": "review_v2"} if grounded else {}),
                    )
                try:
                    if grounded:
                        validate_actions(result, review_context)
                    thread_replies(result, context)
                except (ProviderError, ValueError, KeyError, TypeError) as exc:
                    details = validation_diagnostics(exc)
                    if not details:
                        details = {"validation_error": "review_consistency"}
                    raise ProviderError("invalid", detail="output_invalid", **details) from None
                if grounded:
                    context["intent"] = intent
                if team:
                    context["review_team"] = audit
                attempt_rows(job, pk=model_attempt.pk).update(
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
                if team:
                    metadata["failed_phase"] = phase
                metadata.update(validation_diagnostics(exc))
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
                    "review_timeout",
                    "review_incomplete",
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
                attempt_rows(job, pk=model_attempt.pk).update(
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
            latest = ci_snapshot(github, repo, job.head_sha)
            progress("checks_passed" if latest["passed"] else "checks_blocked")
            checks = {
                "passed": latest["passed"],
                "local_passed": tests is None or tests["passed"],
                "summary": latest["summary"] + (" " + tests["summary"] if tests else ""),
            }
        save_review(job, result, context, checks)
        outcome = _deliver_and_finish(github, job, progress)
    except DeliveryPaused as exc:
        progress("delivery_attention")
        delivered = Receipt.objects.filter(job=job, key="review").exists()
        prefix = (
            "Review published; label cleanup paused. " if delivered else "GitHub delivery paused. "
        )
        finish(job, "attention", prefix + str(exc))
    except LabelCleanupError:
        progress("labels_attention")
        finish(
            job,
            "attention",
            "Review published on GitHub; workflow-label cleanup failed. "
            "Admin should reconcile labels; do not rerun the AI review just to repair labels.",
        )
    except StaleReview as exc:
        delivered = Receipt.objects.filter(job=job, key="review").exists()
        prefix = "Review published; finalization stopped" if delivered else "Review stopped"
        finish(job, "superseded", f"{prefix}: {exc.public_reason}. Request a new review if needed.")
    except ReviewCancelled:
        finish(
            job,
            "superseded",
            "Attempt cancelled, commit or discussion changed; request a new review.",
        )
    except CapacityError:
        finish(job, "attention", "Resource cleanup unconfirmed; administrator recovery required.")
        raise
    except Exception:
        if ReviewCheckpoint.objects.filter(job=job).exists():
            progress("delivery_attention")
            finish(
                job,
                "attention",
                "Saved review retained; delivery could not be confirmed. Retry delivery after checking the connection.",
            )
            return
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
        try:
            if model_attempt is not None:
                attempt_rows(job, pk=model_attempt.pk, status="running").update(
                    status=(
                        "cancelled"
                        if Job.objects.filter(
                            pk=job.pk, status__in=["cancelled", "superseded"]
                        ).exists()
                        else "attention"
                    ),
                    finished_at=timezone.now(),
                )
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

        finally:
            progress.close()


def _deliver_and_finish(github, job, progress):
    from core.delivery import deliver
    from core.sheets import sync_status

    progress("publishing")
    outcome = deliver(github, job, publish, progress)
    progress("published")
    finish(job, "completed")
    try:
        if job.pr.repository.sheet_config:
            progress("sheet")
        sync_status(
            github,
            job.pr.repository,
            job.pr.number,
            outcome,
            before_write=lambda: completed_guard(github, job, outcome),
            write_gate=lambda: admission(job, terminal=True),
        )
    except Exception:
        progress("sheet_attention")
        job_rows(job, terminal=True).filter(status="completed").update(
            reason="Review published; Sheet update needs admin attention."
        )
    from core.merging import merge_approved

    merge_approved(github, job, outcome)
    return outcome


def recover_interrupted(cleanup=None):
    from core.claims import recover_dead_owners

    return recover_dead_owners(cleanup)


def completed_guard(github, job, outcome):
    if not owned(job):
        raise StaleReview("Execution ownership lost")
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
