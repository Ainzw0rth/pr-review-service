"""Current GitHub assignments, separate from the human who triggered a review."""

import re

from django.db import transaction
from django.utils import timezone

from .models import PRAssignee, PullRequest


@transaction.atomic
def sync_assignees(pr, snapshot):
    if "assignees" not in snapshot:
        return  # Partial/legacy snapshots must not silently erase known assignments.
    users = snapshot["assignees"]
    if not isinstance(users, list):
        raise ValueError("Invalid GitHub assignee snapshot")
    parsed = {}
    for user in users:
        if (
            not isinstance(user, dict)
            or type(user.get("id")) is not int
            or not 0 < user["id"] < 2**63
            or not isinstance(user.get("login"), str)
            or len(user["login"]) > 100
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?", user["login"])
        ):
            raise ValueError("Invalid GitHub assignee profile")
        parsed[user["id"]] = user["login"]
    PullRequest.objects.select_for_update().get(pk=pr.pk)
    for github_id, login in parsed.items():
        PRAssignee.objects.update_or_create(pr=pr, github_id=github_id, defaults={"login": login})
    pr.assignees.exclude(github_id__in=parsed).delete()
    pr.assignees_synced_at = timezone.now()
    PullRequest.objects.filter(pk=pr.pk).update(assignees_synced_at=pr.assignees_synced_at)
