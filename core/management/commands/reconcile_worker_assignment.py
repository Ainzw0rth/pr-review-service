"""Record owner-confirmed termination on a revoked worker; never stop or publish work."""

import uuid

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.claims import pool
from core.execution_dispatch import digest, encoded
from core.models import ExecutionAssignment, ExecutionWorker
from core.retries import is_retry_owner

MAX_EVIDENCE_LENGTH = 4096


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("assignment", help="Unresolved execution assignment UUID")
        parser.add_argument(
            "--operator", required=True, help="Configured dashboard owner's username"
        )
        parser.add_argument(
            "--evidence", required=True, help="Actual-host termination evidence; no credentials"
        )
        parser.add_argument(
            "--confirm-terminated",
            action="store_true",
            help="Attest that actual worker execution has already been terminated",
        )

    def handle(self, *args, **options):
        if options.get("confirm_terminated") is not True:
            raise CommandError(
                "Actual-host termination must be verified before --confirm-terminated."
            )
        evidence = options.get("evidence")
        if (
            not isinstance(evidence, str)
            or not evidence.strip()
            or len(evidence) > MAX_EVIDENCE_LENGTH
        ):
            raise CommandError("Evidence must be nonblank and at most 4096 characters.")
        try:
            assignment_id = uuid.UUID(options["assignment"])
        except (ValueError, TypeError, AttributeError):
            raise CommandError("Assignment must be a UUID.") from None
        with transaction.atomic():
            pool()  # Same admission/cleanup lock order as worker protocol mutations.
            owner = get_user_model().objects.filter(username=options.get("operator")).first()
            if owner is None or not is_retry_owner(owner):
                raise CommandError("Operator must be the configured active dashboard owner.")
            worker_id = (
                ExecutionAssignment.objects.filter(pk=assignment_id)
                .values_list("worker_id", flat=True)
                .first()
            )
            if worker_id is None:
                raise CommandError("Execution assignment not found.")
            worker = ExecutionWorker.objects.select_for_update().get(pk=worker_id)
            row = ExecutionAssignment.objects.select_for_update().get(
                pk=assignment_id, worker=worker
            )
            if worker.revoked_at is None:
                raise CommandError("Revoke the worker before operator reconciliation.")
            if row.cleanup_confirmed:
                self.stdout.write(f"Assignment {row.pk} already reconciled; no changes.")
                return
            confirmed_at = timezone.now()
            result = {
                "exit_code": 1,
                "oom": False,
                "error": "operator_confirmed_termination",
                "audit": {
                    "operator_id": owner.pk,
                    "operator": owner.get_username(),
                    "confirmed_at": confirmed_at.isoformat(),
                    "evidence": evidence.strip(),
                },
            }
            row.state = "failed"
            row.cleanup_confirmed = True
            row.encrypted_input = ""
            row.result = result
            row.result_digest = digest(encoded(result))
            row.output.all().delete()
            row.output_bytes = 0
            row.next_sequence = 0
            row.save(
                update_fields=[
                    "state",
                    "cleanup_confirmed",
                    "encrypted_input",
                    "result",
                    "result_digest",
                    "output_bytes",
                    "next_sequence",
                    "updated_at",
                ]
            )
        self.stdout.write(
            f"Assignment {assignment_id} termination recorded; failed, never accepted."
        )
