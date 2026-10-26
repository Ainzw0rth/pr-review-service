from django.db import models


class WorkerKeepalive(models.Model):
    worker = models.OneToOneField(
        "core.ExecutionWorker", on_delete=models.CASCADE, related_name="keepalive"
    )
    encrypted_config = models.TextField(editable=False, blank=True)
    enabled = models.BooleanField(default=False)
    interval_seconds = models.PositiveIntegerField(default=1200)
    next_due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=32, default="never")
    error = models.CharField(max_length=160, blank=True)
    lease_token = models.CharField(max_length=32, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
