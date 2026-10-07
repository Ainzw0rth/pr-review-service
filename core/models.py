import uuid

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models


class Organization(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)

    def __str__(self):
        return self.name


class Membership(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "user"], name="unique_member")
        ]


class AccessLink(models.Model):
    digest = models.CharField(max_length=64, unique=True)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, null=True, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True
    )
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    revoked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)


class Credential(models.Model):
    name = models.CharField(max_length=100, unique=True)
    encrypted_value = models.TextField(editable=False)

    def __str__(self):
        return self.name


class Provider(models.Model):
    name = models.CharField(max_length=100)
    kind = models.CharField(
        max_length=20,
        choices=[
            ("anthropic", "Anthropic Messages API"),
            ("openai", "OpenAI Responses API"),
            ("claude_cli", "Claude Code subscription CLI"),
            ("codex_cli", "Codex subscription CLI"),
        ],
    )
    model = models.CharField(max_length=120)
    credential = models.ForeignKey(Credential, on_delete=models.PROTECT, null=True, blank=True)
    order = models.PositiveIntegerField(default=0)
    enabled = models.BooleanField(default=True)

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.kind in {"claude_cli", "codex_cli"}:
            if self.credential_id:
                raise ValidationError(
                    {
                        "credential": "CLI backends use private subscription login volumes, not API keys."
                    }
                )
        elif not self.credential_id:
            raise ValidationError({"credential": "API backends require an API credential."})

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.name


class SubscriptionQuota(models.Model):
    # There is one private CLI login volume per backend in this deployment.
    kind = models.CharField(max_length=20, primary_key=True)
    windows = models.JSONField(default=list)
    checked_at = models.DateTimeField(null=True)
    observed_at = models.DateTimeField(null=True)
    blocked_until = models.DateTimeField(null=True)
    error = models.CharField(max_length=30, blank=True)


class Repository(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    full_name = models.CharField(
        max_length=200,
        unique=True,
        validators=[RegexValidator(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")],
    )
    reviewer = models.CharField(max_length=100)
    enabled = models.BooleanField(default=True)
    github_credential = models.ForeignKey(Credential, on_delete=models.PROTECT, related_name="+")
    webhook_credential = models.ForeignKey(Credential, on_delete=models.PROTECT, related_name="+")
    review_label = models.CharField(max_length=60, default="on review")
    feedback_label = models.CharField(max_length=60, default="feedback")
    approved_label = models.CharField(max_length=60, default="approved")
    required_checks = models.JSONField(
        default=list,
        blank=True,
        help_text="GitHub check/status names that must succeed for this commit.",
    )
    test_image = models.CharField(
        max_length=240,
        blank=True,
        help_text="Prebuilt image pinned by @sha256 digest; empty disables local tests.",
    )
    test_command = models.JSONField(
        default=list,
        blank=True,
        help_text='Argument array, e.g. ["npm", "test"]. No network or credentials provided.',
    )
    instructions = models.TextField(blank=True)
    sheet_config = models.JSONField(
        default=dict, blank=True, help_text="Optional confirmed Sheet mapping; see README."
    )
    sheet_credential = models.ForeignKey(
        Credential, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    def __str__(self):
        return self.full_name


class PullRequest(models.Model):
    repository = models.ForeignKey(Repository, on_delete=models.CASCADE)
    number = models.PositiveIntegerField()
    head_sha = models.CharField(max_length=64)
    is_open = models.BooleanField(default=True)
    requested = models.BooleanField(default=False)
    labels = models.JSONField(default=list)
    generation = models.PositiveIntegerField(default=0)
    canonical_trigger = models.CharField(max_length=100, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["repository", "number"], name="unique_pr")]

    @property
    def url(self):
        return f"https://github.com/{self.repository.full_name}/pull/{self.number}"


class Job(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Reviewing"
        QUOTA = "quota", "Out of quota"
        ATTENTION = "attention", "Needs attention"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        SUPERSEDED = "superseded", "Superseded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pr = models.ForeignKey(PullRequest, on_delete=models.CASCADE, related_name="jobs")
    generation = models.PositiveIntegerField()
    head_sha = models.CharField(max_length=64)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    actor = models.CharField(max_length=100)
    reason = models.CharField(max_length=250, blank=True)
    provider_diagnostics = models.JSONField(default=dict, blank=True)
    provider_plan = models.JSONField(default=list, blank=True)
    retry_of = models.OneToOneField(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="dashboard_retry"
    )
    provider = models.ForeignKey(Provider, on_delete=models.SET_NULL, null=True, blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [models.UniqueConstraint(fields=["pr", "generation"], name="unique_attempt")]


class ModelAttempt(models.Model):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="model_attempts")
    provider = models.ForeignKey(Provider, on_delete=models.SET_NULL, null=True)
    provider_name = models.CharField(max_length=100)
    model = models.CharField(max_length=120)
    status = models.CharField(max_length=20, default="running")
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    diagnostics = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["id"]


class JobActivity(models.Model):
    detail = models.TextField(blank=True, max_length=1000)
    source = models.CharField(blank=True, max_length=100)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="activity")
    code = models.CharField(max_length=30)
    count = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


class Webhook(models.Model):
    delivery = models.CharField(max_length=100, unique=True)
    repository = models.ForeignKey(Repository, on_delete=models.CASCADE)
    event = models.CharField(max_length=60)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=200, blank=True)


class Receipt(models.Model):
    job = models.ForeignKey(Job, on_delete=models.CASCADE)
    key = models.CharField(max_length=180)
    remote_id = models.CharField(max_length=100, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "key"], name="unique_receipt")]


class ExpectedLabel(models.Model):
    repository = models.ForeignKey(Repository, on_delete=models.CASCADE)
    number = models.PositiveIntegerField()
    label = models.CharField(max_length=60)
    action = models.CharField(max_length=20)
    actor = models.CharField(max_length=100)
    expires_at = models.DateTimeField()


class LoginAttempt(models.Model):
    key = models.CharField(max_length=64, unique=True)
    count = models.PositiveIntegerField(default=0)
    window_at = models.DateTimeField()


class JournalEntry(models.Model):
    """Private delivery outbox; never included in member dashboard feeds."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(
        Job, null=True, on_delete=models.SET_NULL, related_name="journal_entries"
    )
    repository = models.CharField(max_length=200)
    encrypted_payload = models.TextField(editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=180, blank=True)
