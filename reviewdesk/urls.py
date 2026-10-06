from django.contrib import admin
from django.urls import include, path

from core.webhooks import github_webhook

urlpatterns = [
    path("webhooks/github/<int:repository_id>/", github_webhook),
    path("admin/", admin.site.urls),
    path("", include("core.urls")),
]
