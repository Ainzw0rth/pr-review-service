"""Gate ingress before migrations and until replacement supervisors are ready."""

from django.conf import settings
from django.http import HttpResponse


class MaintenanceMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path != "/healthz/" and (settings.DATA_DIR / "deployment.maintenance").exists():
            response = HttpResponse(
                "Maintenance in progress. Retry later.", status=503, content_type="text/plain"
            )
            response["Retry-After"] = "120"
            return response
        return self.get_response(request)
