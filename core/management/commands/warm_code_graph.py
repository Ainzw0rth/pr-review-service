"""Prebuild an isolated graph at an explicit commit; never invoke a review or publish."""

import base64
import json
import re

from django.core.management.base import BaseCommand, CommandError

from core.github import GitHub, IntegrationError
from core.graph import execute, validate
from core.models import Repository


class Command(BaseCommand):
    help = "Prewarm the configured graph cache: warm_code_graph owner/repo COMMIT_SHA"

    def add_arguments(self, parser):
        parser.add_argument("repository")
        parser.add_argument("sha")

    def handle(self, repository, sha, **options):
        if not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise CommandError("Provide the exact 40-character commit SHA.")
        try:
            repo = Repository.objects.get(full_name=repository)
        except Repository.DoesNotExist:
            raise CommandError("Repository is not configured.") from None
        try:
            with GitHub(repo) as github:
                result = execute(
                    {
                        "repository": repo.full_name,
                        "changed": [],
                        "snapshots": [
                            {"sha": sha, "archive": base64.b64encode(github.archive(sha)).decode()}
                        ],
                    }
                )
            self.stdout.write(json.dumps(validate(result, repo.full_name, sha)))
        except IntegrationError as exc:
            raise CommandError(str(exc)) from None
