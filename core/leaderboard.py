"""Tenant-scoped activity metrics, not a score for code quality."""

import re
from collections import Counter
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.db.models import Min
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .models import PullRequest
from .views import visible_jobs


@login_required
@never_cache
@require_GET
def leaderboard(request):
    period = 30 if request.GET.get("period") == "30" else 90
    group = "requester" if request.GET.get("group") == "requester" else "assignee"
    today = timezone.localdate()
    start = today - timedelta(days=period - 1)
    # A complete delivery receipt prevents CI-only stops and partial publications
    # from counting. Multiple reviews of one PR count once within this window.
    completed = visible_jobs(request.user).filter(
        status="completed",
        review_checkpoint__stage="done",
        finished_at__date__gte=start,
        finished_at__date__lte=today,
    )
    reviewed = dict(
        completed.order_by()
        .values("pr_id")
        .annotate(first_review=Min("finished_at"))
        .values_list("pr_id", "first_review")
    )
    people = {}
    daily = Counter()
    unknown = unassigned = 0
    for pr in PullRequest.objects.filter(pk__in=reviewed).prefetch_related("assignees"):
        daily[timezone.localdate(reviewed[pr.pk])] += 1
        assignees = list(pr.assignees.all())
        if not pr.assignees_synced_at:
            unknown += 1
        elif not assignees:
            unassigned += 1
        for user in assignees:
            row = people.setdefault(
                user.github_id,
                {
                    "id": user.github_id,
                    "login": user.login,
                    "avatar": user.avatar_url,
                    "count": 0,
                    "synced_at": pr.assignees_synced_at,
                },
            )
            if pr.assignees_synced_at and (
                not row["synced_at"] or pr.assignees_synced_at > row["synced_at"]
            ):
                row.update(login=user.login, synced_at=pr.assignees_synced_at)
            row["count"] += 1
    if group == "requester":
        people = {}
        attributed = set()
        for pr_id, actor in (
            completed.exclude(actor="").order_by("actor").values_list("pr_id", "actor").distinct()
        ):
            key = actor.casefold()
            row = people.setdefault(
                key,
                {
                    "login": actor,
                    "count": 0,
                    "prs": set(),
                    "avatar": f"https://github.com/{actor}.png?size=96"
                    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?", actor)
                    else "",
                    "queue_query": urlencode({"actor": actor}),
                },
            )
            row["prs"].add(pr_id)
            row["count"] = len(row["prs"])
            attributed.add(pr_id)
        unknown = len(reviewed.keys() - attributed)
        unassigned = 0
    else:
        for row in people.values():
            row["queue_query"] = urlencode({"assignee": row["id"]})
    leaders = sorted(people.values(), key=lambda row: (-row["count"], row["login"].casefold()))
    maximum = leaders[0]["count"] if leaders else 1
    rank = 0
    previous = None
    for index, row in enumerate(leaders, 1):
        if row["count"] != previous:
            rank = index
        row.update(
            rank=rank, width=round(row["count"] / maximum * 100), delay=min(index - 1, 8) * 45
        )
        previous = row["count"]
    peak = max(daily.values(), default=1)
    days = []
    for offset in range(period):
        day = start + timedelta(days=offset)
        count = daily[day]
        days.append({"date": day, "count": count, "height": round(count / peak * 100)})
    return render(
        request,
        "core/leaderboard.html",
        {
            "group": group,
            "leaders": leaders,
            "total": len(reviewed),
            "period": period,
            "unassigned": unassigned,
            "unknown": unknown,
            "days": days,
            "peak": max(daily.values(), default=0),
            "start": start,
            "today": today,
        },
    )
