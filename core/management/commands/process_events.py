import fcntl
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from core.events import process_event
from core.github import GitHubRateLimited
from core.models import Webhook


class Command(BaseCommand):
    help = "Reconcile durable GitHub events (run independently of the review worker)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        with open(settings.DATA_DIR / "events.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            while True:
                for event in (
                    Webhook.objects.filter(processed_at__isnull=True)
                    .select_related("repository")
                    .order_by("created_at")[:100]
                ):
                    try:
                        process_event(event)
                    except GitHubRateLimited as exc:
                        Webhook.objects.filter(pk=event.pk).update(error=str(exc)[:200])
                        continue
                    except Exception:
                        Webhook.objects.filter(pk=event.pk).update(
                            error="Connection unavailable; event processing will retry."
                        )
                        continue
                if options["once"]:
                    return
                time.sleep(3)
