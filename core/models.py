import uuid

from django.conf import settings
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone


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
    effort = models.CharField(max_length=10, blank=True, default="")
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
    auto_approve = models.BooleanField(default=False)
    auto_merge = models.BooleanField(default=False)
    merge_method = models.CharField(
        max_length=10,
        default="squash",
        choices=[("squash", "Squash merge"), ("merge", "Merge commit"), ("rebase", "Rebase merge")],
    )
    related_item_limit = models.PositiveIntegerField(
        default=50,
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
        help_text="Maximum related issues and PRs per review; blank means no item limit.",
    )
    enabled = models.BooleanField(default=True)
    graph_enabled = models.BooleanField(null=True, blank=True, default=None)
    webhook_id = models.CharField(max_length=32, blank=True)
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
    assignees_synced_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["repository", "number"], name="unique_pr")]

    @property
    def url(self):
        return f"https://github.com/{self.repository.full_name}/pull/{self.number}"


class PRAssignee(models.Model):
    pr = models.ForeignKey(PullRequest, on_delete=models.CASCADE, related_name="assignees")
    github_id = models.PositiveBigIntegerField()
    login = models.CharField(max_length=100)

    class Meta:
        ordering = ["login"]
        constraints = [
            models.UniqueConstraint(fields=["pr", "github_id"], name="unique_pr_assignee")
        ]

    @property
    def avatar_url(self):
        return f"https://avatars.githubusercontent.com/u/{self.github_id}?s=96"


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
    auto_approve = models.BooleanField(default=False)
    auto_merge = models.BooleanField(default=False)
    merge_method = models.CharField(
        max_length=10,
        default="squash",
        choices=[("squash", "Squash merge"), ("merge", "Merge commit"), ("rebase", "Rebase merge")],
    )
    workflow_version = models.CharField(max_length=16, blank=True, default="")
    workflow_revision = models.CharField(max_length=16, blank=True, default="", editable=False)
    workflow_steps = models.JSONField(default=list, blank=True, editable=False)
    retry_of = models.OneToOneField(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="dashboard_retry"
    )
    provider = models.ForeignKey(Provider, on_delete=models.SET_NULL, null=True, blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    github_retry_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    @property
    def duration_display(self):
        """Wall time since the worker first started; excludes initial queue wait."""
        if not self.started_at:
            return "Not started"
        end = self.finished_at
        if end is None:
            if self.status != self.Status.RUNNING:
                return "Unavailable"
            end = timezone.now()
        seconds = max(0, int((end - self.started_at).total_seconds()))
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        if hours:
            return f"{hours}h {minutes}m {seconds}s"
        return f"{minutes}m {seconds}s" if minutes else f"{seconds}s"

    def save(self, *args, **kwargs):
        if self._state.adding:
            from .workflow_schema import snapshot as workflow_snapshot

            self.workflow_revision, self.workflow_steps = workflow_snapshot(self.pr.repository)
            self.auto_approve, self.auto_merge, self.merge_method = Repository.objects.values_list(
                "auto_approve", "auto_merge", "merge_method"
            ).get(pk=self.pr.repository_id)
        if self._state.adding and not self.workflow_version:
            from .review_config import snapshot, version

            revision, plan = snapshot()
            self.workflow_version = version(self.provider_plan) if self.provider_plan else revision
            if not self.provider_plan:
                self.provider_plan = plan
        super().save(*args, **kwargs)

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
    retry_at = models.DateTimeField(null=True, blank=True)
    retry_count = models.PositiveIntegerField(default=0)
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
    action = models.CharField(max_length=32)
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


class PhaseCheckpoint(models.Model):
    """Private validated analysis, independent of publication receipts."""

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="phase_checkpoints")
    phase = models.CharField(max_length=32)
    fingerprint = models.CharField(max_length=64)
    encrypted_payload = models.TextField(editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["job", "phase"], name="unique_phase_checkpoint")
        ]


class ReviewCheckpoint(models.Model):
    """Private review artifact and delivery cursor; never returned by dashboard endpoints."""

    job = models.OneToOneField(Job, on_delete=models.CASCADE, related_name="review_checkpoint")
    encrypted_payload = models.TextField(editable=False)
    stage = models.CharField(max_length=20, default="ready")
    state = models.JSONField(default=dict, editable=False)
    delivery_attempts = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)


class ModelProbe(models.Model):
    owner = models.ForeignKey("WorkerOwner", null=True, on_delete=models.PROTECT)
    provider = models.ForeignKey(Provider, on_delete=models.CASCADE)
    configuration = models.JSONField(default=dict)
    status = models.CharField(max_length=20, default="queued")
    reason = models.CharField(max_length=250, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)


class ApprovalNotification(models.Model):
    """Durable Telegram receipt; never contains review content or bot credentials."""

    job = models.OneToOneField(Job, on_delete=models.CASCADE, related_name="approval_notification")
    status = models.CharField(max_length=20, default="pending")
    attempts = models.PositiveIntegerField(default=0)
    reason = models.CharField(max_length=200, blank=True)
    message_id = models.CharField(max_length=30, blank=True)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True)


class ApprovalRecord(models.Model):
    """Own approval intent survives job-history cleanup, allowing later dismissal."""

    job = models.OneToOneField(Job, null=True, on_delete=models.SET_NULL)
    repository = models.ForeignKey(Repository, on_delete=models.PROTECT)
    number = models.PositiveIntegerField()
    head_sha = models.CharField(max_length=64)
    marker = models.CharField(max_length=100, unique=True)
    actor = models.CharField(max_length=100)
    revoke_requested = models.BooleanField(default=False)
    review_id = models.CharField(max_length=30, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    checked_at = models.DateTimeField(null=True)
    dismissed_at = models.DateTimeField(null=True)
    error = models.CharField(max_length=200, blank=True)


class ServiceSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    concurrent_pr_reviews = models.PositiveSmallIntegerField(default=2)
    cli_account_concurrency = models.PositiveSmallIntegerField(null=True, blank=True, default=None)
    draining = models.BooleanField(default=False)
    drain_generation = models.PositiveIntegerField(default=0)

    def clean(self):
        from django.core.exceptions import ValidationError

        if type(self.concurrent_pr_reviews) is not int or not 1 <= self.concurrent_pr_reviews <= 8:
            raise ValidationError({"concurrent_pr_reviews": "Choose a whole number from 1 to 8."})
        if self.cli_account_concurrency is not None and (
            type(self.cli_account_concurrency) is not int
            or not 1 <= self.cli_account_concurrency <= 32
        ):
            raise ValidationError(
                {"cli_account_concurrency": "Choose a whole number from 1 to 32."}
            )
        if self.pk != 1:
            raise ValidationError("Service settings is a singleton.")

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)

    def full_clean(self, *args, **kwargs):
        self.clean()  # Validate before IntegerField coerces bool/string/fractional input.
        return super().full_clean(*args, **kwargs)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="service_settings_singleton"),
            models.CheckConstraint(
                condition=models.Q(concurrent_pr_reviews__gte=1, concurrent_pr_reviews__lte=8),
                name="concurrent_reviews_bounds",
            ),
            models.CheckConstraint(
                condition=models.Q(cli_account_concurrency__isnull=True)
                | models.Q(cli_account_concurrency__gte=1, cli_account_concurrency__lte=32),
                name="cli_account_concurrency_bounds",
            ),
        ]


class WorkerOwner(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    heartbeat_at = models.DateTimeField(default=timezone.now)
    retired = models.BooleanField(default=False)
    drain_ack = models.PositiveIntegerField(default=0)
    probe_active = models.BooleanField(default=False)


class JobClaim(models.Model):
    job = models.OneToOneField(Job, on_delete=models.PROTECT, related_name="execution_claim")
    pr = models.OneToOneField(PullRequest, on_delete=models.PROTECT, related_name="execution_claim")
    owner = models.ForeignKey(WorkerOwner, on_delete=models.PROTECT)
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    heartbeat_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()


class GitHubCooldown(models.Model):
    """Token-hash-scoped GitHub backoff shared by every repository and process."""

    scope = models.CharField(max_length=64, primary_key=True)
    retry_at = models.DateTimeField()
    kind = models.CharField(max_length=16)
