from django.urls import path

from . import leaderboard, repository_views, settings_views, views, workflow_views

urlpatterns = [
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
