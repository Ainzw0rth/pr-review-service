from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.shortcuts import render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters

from .forms import CredentialForm, RepositoryForm
from .models import (
    AccessLink,
    ApprovalNotification,
    ApprovalRecord,
    Credential,
    Job,
    JournalEntry,
    Membership,
    ModelAttempt,
    Organization,
    Provider,
    Repository,
    Webhook,
)
from .views import admin_login, issue_access_link

# Staff membership alone never grants platform configuration privileges.
admin.site.has_permission = lambda request: request.user.is_active and request.user.is_superuser
admin.site.login = admin_login
admin.site.site_header = "Auto Reviewer · Administration"
admin.site.site_title = "Auto Reviewer"
admin.site.index_title = "Platform configuration"


def links_response(request, links):
    response = render(
        request,
        "core/admin_links.html",
        {**admin.site.each_context(request), "title": "One-time access links", "links": links},
    )
    response["Cache-Control"] = "no-store, private"
    response["Referrer-Policy"] = "same-origin"
    return response


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ["name", "slug"]
    prepopulated_fields = {"slug": ("name",)}
    actions = ["invite"]

    @admin.action(description="Generate one-time invitations (24 hours)")
    def invite(self, request, queryset):
        return links_response(
            request, [(org.name, issue_access_link(organization=org)) for org in queryset]
        )


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "organization"]
    list_filter = ["organization"]
    search_fields = ["user__username", "organization__name"]


@admin.register(AccessLink)
class AccessLinkAdmin(admin.ModelAdmin):
    list_display = ["id", "organization", "user", "created_at", "expires_at", "used_at", "revoked"]
    readonly_fields = ["organization", "user", "created_at", "expires_at", "used_at"]
    fields = readonly_fields + ["revoked"]
    actions = ["revoke"]

    def has_add_permission(self, request):
        return False

    @admin.action(description="Revoke selected links")
    def revoke(self, request, queryset):
        queryset.update(revoked=True)


@admin.register(Credential)
@method_decorator(sensitive_post_parameters("secret"), name="changeform_view")
class CredentialAdmin(admin.ModelAdmin):
    form = CredentialForm
    list_display = ["name"]


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = ["name", "kind", "model", "order", "enabled"]
    list_filter = ["kind", "enabled"]


@admin.register(Repository)
class RepositoryAdmin(admin.ModelAdmin):
    readonly_fields = ["auto_approve"]
    form = RepositoryForm
    list_display = ["full_name", "organization", "reviewer", "enabled"]
    list_filter = ["organization", "enabled"]


class ModelAttemptInline(admin.TabularInline):
    model = ModelAttempt
    extra = 0
    can_delete = False
    readonly_fields = [field.name for field in ModelAttempt._meta.fields]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    inlines = [ModelAttemptInline]
    list_display = ["id", "pr", "status", "actor", "created_at", "finished_at"]
    list_filter = ["status", "pr__repository__organization"]
    readonly_fields = [field.name for field in Job._meta.fields]
    actions = ["interrupt"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Interrupt selected active attempts")
    def interrupt(self, request, queryset):
        count = queryset.filter(status__in=["queued", "running", "quota", "attention"]).update(
            status=Job.Status.CANCELLED,
            finished_at=timezone.now(),
            reason="Interrupted by platform administrator.",
        )
        self.message_user(request, f"{count} active attempt(s) interrupted.")


class PlatformUserAdmin(UserAdmin):
    actions = ["reset_link"]

    @admin.action(description="Generate password-reset links (1 hour)")
    def reset_link(self, request, queryset):
        return links_response(
            request,
            [
                (user.username, issue_access_link(user=user, hours=1))
                for user in queryset.filter(is_active=True)
            ],
        )


admin.site.unregister(get_user_model())
admin.site.register(get_user_model(), PlatformUserAdmin)


@admin.register(Webhook)
class WebhookAdmin(admin.ModelAdmin):
    list_display = ["delivery", "repository", "event", "created_at", "processed_at", "error"]
    fields = list_display
    readonly_fields = list_display
    list_filter = ["repository", "event"]
    search_fields = ["delivery", "repository__full_name"]
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = ["id", "repository", "job", "created_at", "sent_at", "attempts", "last_error"]
    readonly_fields = list_display
    fields = readonly_fields
    list_filter = ["repository", "sent_at"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ApprovalNotification)
class ApprovalNotificationAdmin(admin.ModelAdmin):
    list_display = ["job", "status", "attempts", "created_at", "sent_at", "reason"]
    list_filter = ["status"]
    readonly_fields = [
        "job",
        "status",
        "attempts",
        "message_id",
        "next_attempt_at",
        "created_at",
        "sent_at",
        "reason",
    ]
    fields = readonly_fields
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ApprovalRecord)
class ApprovalRecordAdmin(admin.ModelAdmin):
    list_display = ["repository", "number", "head_sha", "review_id", "dismissed_at", "error"]
    readonly_fields = [field.name for field in ApprovalRecord._meta.fields]
    fields = readonly_fields
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
