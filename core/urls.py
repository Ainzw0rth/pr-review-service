from django.urls import path

from . import leaderboard, repository_views, settings_views, views, worker_views, workflow_views
from .worker_api import worker_endpoint

urlpatterns = [
    path("workers/", worker_views.workers, name="workers"),
    path("workers/state/", worker_views.workers_state, name="workers_state"),
    path("workers/actions/", worker_views.worker_action, name="worker_action"),
    path("api/workers/v1/enroll/", worker_endpoint, {"action": "enroll"}, name="worker_enroll"),
    path(
        "api/workers/v1/heartbeat/",
        worker_endpoint,
        {"action": "heartbeat"},
        name="worker_heartbeat",
    ),
    path("settings/repositories/add/", repository_views.repository_edit, name="repository_add"),
    path(
        "settings/repositories/<int:repository_id>/",
        repository_views.repository_edit,
        name="repository_edit",
    ),
    path(
        "settings/repositories/<int:repository_id>/connect/",
        repository_views.repository_connect,
        name="repository_connect",
    ),
    path(
        "settings/repositories/<int:repository_id>/toggle/",
        repository_views.repository_toggle,
        name="repository_toggle",
    ),
    path("leaderboard/", leaderboard.leaderboard, name="leaderboard"),
    path("settings/", settings_views.service_settings, name="service_settings"),
    path("workflow/", workflow_views.workflow_page, name="workflow"),
    path("workflow/state/", workflow_views.workflow_state, name="workflow_state"),
    path("workflow/probe/<int:provider_id>/", workflow_views.workflow_probe, name="workflow_probe"),
    path("healthz/", views.health, name="health"),
    path("", views.dashboard, name="dashboard"),
    path("subscriptions/", views.subscription_quota, name="subscription_quota"),
    path("login/", views.sign_in, name="login"),
    path("logout/", views.sign_out, name="logout"),
    path("access/<str:token>/", views.access, name="access"),
    path("jobs/retry-failed/", views.jobs_retry_failed, name="jobs_retry_failed"),
    path("jobs/<uuid:job_id>/", views.job_detail, name="job_detail"),
    path("jobs/<uuid:job_id>/retry/", views.job_retry, name="job_retry"),
    path("jobs/<uuid:job_id>/activity/", views.job_activity, name="job_activity"),
]

for action in ("claim", "input", "event", "renew", "complete"):
    urlpatterns.append(
        path(
            f"api/workers/v1/{action}/",
            worker_endpoint,
            {"action": action},
            name=f"worker_{action}",
        )
    )
