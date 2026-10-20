"""Bounded supervisor; durable claims include every worker's finally cleanup."""

import time
from concurrent.futures import ThreadPoolExecutor

from django.core.management.base import BaseCommand
from django.db import close_old_connections, connection, connections, transaction
from django.utils import timezone

from core.claims import execution_job, pool, register_owner, release, supervisor_owner
from core.models import JobClaim, WorkerOwner
from core.worker import run_job
from core.workflow import claim, finish


def execute(job):
    close_old_connections()
    token = execution_job.set(job)
    try:
        run_job(job)
    except Exception:
        # Unknown cleanup outcome is quarantined: do not silently free its slot.
        finish(
            job,
            "attention",
            "Worker cleanup could not be confirmed; administrator recovery required.",
        )
        raise
    else:
        release(job)
    finally:
        execution_job.reset(token)
        try:
            connections.close_all()
        finally:
            # The thread is quiescent even if its exception traceback retains Job.
            # Keep the DB claim quarantined; scoped recovery must clean child resources.
            handle = getattr(job, "execution_lock", None)
            if handle:
                handle.close()


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        from core.model_probe import claim_probe, run_probe

        owner = register_owner()
        owner_context = supervisor_owner.set(str(owner.pk))
        futures = set()
        try:
            with ThreadPoolExecutor(max_workers=8, thread_name_prefix="review") as executor:
                while True:
                    if not connection.in_atomic_block:
                        close_old_connections()
                    WorkerOwner.objects.filter(pk=owner.pk).update(heartbeat_at=timezone.now())
                    for future in list(futures):
                        if future.done():
                            futures.remove(future)
                            try:
                                future.result()
                            except Exception:
                                self.stderr.write(
                                    "Worker execution failed; retained claim requires recovery."
                                )
                    with transaction.atomic():
                        config = pool()
                        draining = config.draining
                        if (
                            draining
                            and not futures
                            and not JobClaim.objects.filter(owner=owner).exists()
                        ):
                            WorkerOwner.objects.filter(pk=owner.pk, probe_active=False).update(
                                drain_ack=config.drain_generation
                            )
                    if not draining:
                        while len(futures) < 8:
                            job = claim(owner)
                            if job is None:
                                break
                            try:
                                futures.add(executor.submit(execute, job))
                            except Exception:
                                finish(
                                    job,
                                    "attention",
                                    "Executor submission failed before review started.",
                                )
                                release(job)
                                raise
                        # One global auxiliary slot covers quota refresh and probes.
                        if not futures:
                            with transaction.atomic():
                                config = pool()
                                auxiliary = (
                                    not config.draining
                                    and not JobClaim.objects.exists()
                                    and not WorkerOwner.objects.filter(probe_active=True).exists()
                                )
                                if auxiliary:
                                    WorkerOwner.objects.filter(pk=owner.pk).update(
                                        probe_active=True
                                    )
                            if auxiliary:
                                try:
                                    from core.quota import refresh_due

                                    refresh_due()
                                    with transaction.atomic():
                                        config = pool()
                                        probe = None if config.draining else claim_probe()
                                        if probe:
                                            probe.owner = owner
                                            probe.save(update_fields=["owner"])
                                    if probe:
                                        run_probe(probe)
                                finally:
                                    WorkerOwner.objects.filter(pk=owner.pk).update(
                                        probe_active=False
                                    )
                    if options["once"]:
                        break
                    time.sleep(2)
        finally:
            if not JobClaim.objects.filter(owner=owner).exists():
                with transaction.atomic():
                    config = pool()
                    WorkerOwner.objects.filter(pk=owner.pk).update(
                        retired=True, drain_ack=config.drain_generation if config.draining else 0
                    )
            supervisor_owner.reset(owner_context)
            owner.execution_lock.close()
            if not connection.in_atomic_block:
                connections.close_all()
