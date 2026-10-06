"""Inspect Sheet schema/statuses using readonly OAuth; never save the proposal."""

import json

from django.core.management.base import BaseCommand, CommandError

from core.github import IntegrationError
from core.models import Repository
from core.sheets import inspect_mapping


class Command(BaseCommand):
    help = "Print an unconfirmed Sheet mapping proposal without saving or writing anything."

    def add_arguments(self, parser):
        parser.add_argument("repository_id", type=int)
        parser.add_argument("--id-column", help="Exact stable-ID header, if not recognized.")
        parser.add_argument("--status-column", help="Exact status header, if not recognized.")

    def handle(self, *args, **options):
        try:
            repository = Repository.objects.select_related("sheet_credential").get(
                pk=options["repository_id"]
            )
            proposal = inspect_mapping(
                repository, id_column=options["id_column"], status_column=options["status_column"]
            )
        except Repository.DoesNotExist:
            raise CommandError("Repository not found.") from None
        except IntegrationError as error:
            raise CommandError(str(error)) from None
        self.stdout.write(json.dumps(proposal, indent=2, ensure_ascii=False))
