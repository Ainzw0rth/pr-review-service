"""Small, explicit CLI check, executed serially by the trusted worker."""

from django.conf import settings
from django.utils import timezone

from .models import ModelProbe, Provider


def claim_probe():
    probe = ModelProbe.objects.filter(status="queued").order_by("id").first()
    if probe and ModelProbe.objects.filter(pk=probe.pk, status="queued").update(status="running"):
        return probe


def run_probe(probe):
    from .capacity import ActiveBudget, CapacityError
    from .cli import review_cli
    from .providers import ProviderError, ReviewCancelled

    budget = ActiveBudget(90)

    def cancelled():
        return (settings.DATA_DIR / "worker.drain").exists()

    try:
        review_cli(
            Provider(**probe.configuration),
            {
                "task": "Connectivity probe only. No PR exists. Return complete false, blocking true, and empty findings, replies and related_comments."
            },
            cancelled,
            budget=budget,
            on_capacity=lambda waiting: ModelProbe.objects.filter(
                pk=probe.pk, status="running"
            ).update(reason="Waiting for capacity." if waiting else "Connection test running."),
        )
        status, reason = (
            "passed",
            "CLI returned a valid structured response. No GitHub actions performed.",
        )
    except ReviewCancelled:
        status, reason = (
            "failed",
            "Connection test stopped: deployment drain or 90-second probe deadline.",
        )
    except ProviderError as exc:
        status, reason = (
            "failed",
            f"Connection test failed: {exc.kind}. Check login, quota, model ID and installed CLI compatibility.",
        )
    except CapacityError:
        raise
    except Exception:
        status, reason = (
            "failed",
            "Runner unavailable. Check worker and CLI container configuration.",
        )
    ModelProbe.objects.filter(pk=probe.pk).update(
        status=status, reason=reason, finished_at=timezone.now()
    )
