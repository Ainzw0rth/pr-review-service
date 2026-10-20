"""Drain all supervisors, including terminal cleanup and connection probes."""

import hashlib
import json

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from core.claims import pool
from core.models import JobClaim, WorkerOwner


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["drain", "resume", "status"])
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options):
        with transaction.atomic():
            config = pool()
            if options["action"] in ("drain", "resume"):
                requested = options["action"] == "drain"
                flag = settings.DATA_DIR / "worker.drain"
                if requested:
                    flag.touch(mode=0o600, exist_ok=True)
                else:
                    flag.unlink(missing_ok=True)
                (settings.DATA_DIR / "worker.drained").unlink(missing_ok=True)
                if requested != config.draining:
                    config.draining = requested
                    config.drain_generation += 1
                    config.save(update_fields=["draining", "drain_generation"])
            owners = list(WorkerOwner.objects.filter(retired=False))
            if not owners:
                owners = list(
                    WorkerOwner.objects.filter(
                        retired=True, drain_ack=config.drain_generation
                    ).order_by("-heartbeat_at")[:1]
                )
            active = JobClaim.objects.count()
            # Unresponsive supervisors are not assumed dead; acknowledgment stays fail-closed.
            idle = (
                config.draining
                and bool(owners)
                and not active
                and all(
                    not o.probe_active and o.drain_ack == config.drain_generation for o in owners
                )
            )
            identity = ":".join(sorted(str(o.pk) for o in owners))
            token = hashlib.sha256(identity.encode()).hexdigest() if owners else ""
            result = dict(
                draining=config.draining,
                active_jobs=active,
                idle=idle,
                worker_token=token,
                concurrent_pr_reviews=config.concurrent_pr_reviews,
                supervisors=[str(o.pk) for o in owners],
            )
        if options["json"]:
            self.stdout.write(json.dumps(result))
        else:
            self.stdout.write(
                f"Intake: {'draining' if config.draining else 'enabled'}. Active jobs: {active}."
            )
            if config.draining:
                self.stdout.write(
                    "Worker acknowledged idle; review and cleanup finished."
                    if idle
                    else "Waiting for worker acknowledgment; do not restart yet."
                )
