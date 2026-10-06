import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm, SetPasswordForm
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .forms import InvitationForm
from .models import AccessLink, Job, LoginAttempt, Membership


def issue_access_link(*, organization=None, user=None, hours=24):
    if (organization is None) == (user is None):
        raise ValueError("Specify exactly one organization or user.")
    if not 0 < hours <= 168:
        raise ValueError("Link lifetime must be 1–168 hours.")
    token = secrets.token_urlsafe(32)
    with transaction.atomic():
        if user:
            AccessLink.objects.filter(user=user, used_at__isnull=True).update(revoked=True)
        AccessLink.objects.create(
            digest=hashlib.sha256(token.encode()).hexdigest(),
            organization=organization,
            user=user,
            expires_at=timezone.now() + timedelta(hours=hours),
        )
    return settings.PUBLIC_URL + reverse("access", args=[token])


def login_budget(request):
    """Shared DB limits: account and direct peer; never trust client-supplied IP headers."""
    now = timezone.now()
    identifiers = [
        ("peer", request.META.get("REMOTE_ADDR", "unknown"), 100),
        ("account", request.POST.get("username", "").casefold(), 10),
    ]
    with transaction.atomic():
        rows = []
        for kind, value, limit in identifiers:
            key = hashlib.sha256((kind + ":" + value).encode()).hexdigest()
            row, _ = LoginAttempt.objects.get_or_create(key=key, defaults={"window_at": now})
            if row.window_at < now - timedelta(minutes=15):
                row.count, row.window_at = 0, now
            if row.count >= limit:
                return False
            rows.append(row)
        for row in rows:
            row.count += 1
            row.save(update_fields=["count", "window_at"])
    return True


@sensitive_post_parameters("password")
@never_cache
@require_http_methods(["GET", "POST"])
def sign_in(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == "POST":
        if not login_budget(request):
            return render(request, "core/login.html", {"form": form, "limited": True}, status=429)
        if form.is_valid():
            login(request, form.get_user())
            return redirect("dashboard")
    return render(request, "core/login.html", {"form": form})


@require_POST
def sign_out(request):
    logout(request)
    return redirect("login")


@sensitive_post_parameters("password1", "password2", "new_password1", "new_password2")
@never_cache
@require_http_methods(["GET", "POST"])
def access(request, token):
    digest = hashlib.sha256(token.encode()).hexdigest()
    # Serial transaction makes consumption atomic, including password/membership writes.
    with transaction.atomic():
        link = (
            AccessLink.objects.select_for_update()
            .select_related("user", "organization")
            .filter(
                digest=digest, revoked=False, used_at__isnull=True, expires_at__gt=timezone.now()
            )
            .first()
        )
        if not link or (link.user_id and not link.user.is_active):
            return render(request, "core/access_invalid.html", status=410)
        resetting = bool(link.user_id)
        if resetting:
            form = SetPasswordForm(link.user, request.POST or None)
        elif request.user.is_authenticated:
            form = None
        else:
            form = InvitationForm(request.POST or None)
        if request.method == "POST" and (form is None or form.is_valid()):
            if resetting:
                form.save()  # Password hash invalidates every existing session.
                logout(request)
                messages.success(request, "Password updated. Sign in with your new password.")
            else:
                user = request.user if request.user.is_authenticated else form.save()
                Membership.objects.get_or_create(user=user, organization=link.organization)
                if not request.user.is_authenticated:
                    login(request, user)
            link.used_at = timezone.now()
            link.save(update_fields=["used_at"])
            return redirect("login" if resetting else "dashboard")
        return render(
            request, "core/access.html", {"form": form, "link": link, "resetting": resetting}
        )


def visible_jobs(user):
    jobs = Job.objects.select_related("pr__repository__organization", "provider").filter(
        Q(created_at__gte=timezone.now() - timedelta(days=90))
        | Q(status__in=["queued", "running", "quota", "attention"])
    )
    if not user.is_superuser:
        jobs = jobs.filter(pr__repository__organization__membership__user=user)
    return jobs


@login_required
@never_cache
@require_GET
def dashboard(request):
    jobs = visible_jobs(request.user)
    counts = dict(jobs.values_list("status").annotate(total=Count("id")))
    status = request.GET.get("status", "")
    if status in Job.Status.values:
        jobs = jobs.filter(status=status)
    else:
        status = ""
    query = request.GET.get("q", "").strip()[:100]
    if query:
        jobs = jobs.filter(pr__repository__full_name__icontains=query)
    page = Paginator(jobs.order_by("-created_at"), 30).get_page(request.GET.get("page"))
    return render(
        request,
        "core/dashboard.html",
        {
            "page": page,
            "status": status,
            "query": query,
            "statuses": Job.Status.choices,
            "running": counts.get("running", 0),
            "waiting": counts.get("queued", 0) + counts.get("quota", 0),
            "attention": counts.get("attention", 0) + counts.get("failed", 0),
            "completed": counts.get("completed", 0),
        },
    )


@login_required
@never_cache
@require_GET
def job_detail(request, job_id):
    job = get_object_or_404(visible_jobs(request.user), pk=job_id)
    # Position within the member's visible queue avoids exposing other tenants' work.
    position = None
    if job.status == Job.Status.QUEUED:
        position = (
            visible_jobs(request.user)
            .filter(status=Job.Status.QUEUED, created_at__lt=job.created_at)
            .count()
            + 1
        )
    return render(request, "core/job_detail.html", {"job": job, "position": position})


@login_required
@never_cache
@require_GET
def job_activity(request, job_id):
    from .activity import message

    job = get_object_or_404(visible_jobs(request.user), pk=job_id)
    events = list(job.activity.order_by("-id")[:100])
    return JsonResponse(
        {
            "status": job.status,
            "status_label": job.get_status_display(),
            "provider": job.provider.name if job.provider_id else None,
            "heartbeat_at": job.heartbeat_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "next_retry_at": job.next_retry_at,
            "reason": job.reason,
            "events": [
                {"id": event.pk, "at": event.created_at, "message": message(event)}
                for event in reversed(events)
            ],
        }
    )


def admin_login(request, extra_context=None):
    if request.user.is_authenticated:
        return HttpResponseForbidden("Platform administrator access required.")
    return redirect("login")


@require_GET
def health(request):
    from django.http import JsonResponse

    return JsonResponse({"status": "ok"})
