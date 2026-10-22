from django import forms
from django.contrib.auth.forms import UserCreationForm

from .models import Credential
from .secrets import encrypt


class InvitationForm(UserCreationForm):
    """Native username uniqueness and configured password validation."""


class CredentialForm(forms.ModelForm):
    secret = forms.CharField(
        widget=forms.PasswordInput(render_value=False),
        required=False,
        help_text="Stored encrypted. Leave empty to keep the current value.",
    )

    class Meta:
        model = Credential
        fields = ["name", "secret"]

    def clean_secret(self):
        value = self.cleaned_data["secret"]
        if not value and not self.instance.pk:
            raise forms.ValidationError("A secret is required for a new credential.")
        if value:
            try:
                self.encrypted = encrypt(value)
            except (ValueError, RuntimeError):
                raise forms.ValidationError("Credential encryption is not configured correctly.")
        return value

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.cleaned_data.get("secret"):
            instance.encrypted_value = self.encrypted
        if commit:
            instance.save()
        return instance


class RepositoryForm(forms.ModelForm):
    class Meta:
        from .models import Repository

        model = Repository
        fields = "__all__"

    def clean_required_checks(self):
        value = self.cleaned_data.get("required_checks") or []
        if not isinstance(value, list) or any(
            not isinstance(x, str) or not x.strip() for x in value
        ):
            raise forms.ValidationError("Use an array of nonempty check names.")
        return value

    def clean_test_command(self):
        value = self.cleaned_data.get("test_command") or []
        if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
            raise forms.ValidationError("Use an array of command arguments.")
        return value

    def clean_test_image(self):
        import re

        value = self.cleaned_data.get("test_image", "")
        if value and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[0-9a-f]{64}", value):
            raise forms.ValidationError(
                "Pin a prebuilt image with @sha256 followed by its 64-character digest."
            )
        return value

    def clean(self):
        data = super().clean()
        if bool(data.get("test_image")) != bool(data.get("test_command")):
            raise forms.ValidationError(
                "Configure both a pinned test image and a command, or neither."
            )
        labels = [data.get(name) for name in ("review_label", "feedback_label", "approved_label")]
        if len(set(labels)) != 3:
            raise forms.ValidationError("Workflow labels must be distinct.")
        config = data.get("sheet_config") or {}
        if not isinstance(config, dict):
            raise forms.ValidationError("Sheet configuration must be an object.")
        return data


class RepositorySettingsForm(forms.ModelForm):
    """Owner settings surface: never renders stored secrets or policy switches."""

    github_token = forms.CharField(
        required=False,
        strip=False,
        max_length=4096,
        widget=forms.PasswordInput(render_value=False),
        help_text="Optional replacement token. Stored encrypted; never displayed again.",
    )
    required_checks = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="One GitHub check or status name per line.",
    )
    graph_enabled = forms.BooleanField(required=False, label="Dependency graph")
    version = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta:
        from .models import Repository

        model = Repository
        fields = [
            "full_name",
            "organization",
            "reviewer",
            "github_credential",
            "enabled",
            "graph_enabled",
            "review_label",
            "feedback_label",
            "approved_label",
            "required_checks",
            "instructions",
        ]
        widgets = {"instructions": forms.Textarea(attrs={"rows": 5})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["github_credential"].required = False
        self.fields["github_credential"].label = "GitHub connection"
        if self.instance.pk:
            from .graph import enabled

            self.fields["full_name"].disabled = True
            self.initial["required_checks"] = "\n".join(self.instance.required_checks)
            self.initial["graph_enabled"] = enabled(self.instance)

    def clean_full_name(self):
        import re

        value = self.cleaned_data["full_name"].strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) or any(
            part in (".", "..") for part in value.split("/")
        ):
            raise forms.ValidationError("Use owner/repository, not a URL or path.")
        from .models import Repository

        if Repository.objects.filter(full_name__iexact=value).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("This repository is already configured.")
        return value

    def clean_reviewer(self):
        import re

        value = self.cleaned_data["reviewer"]
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}", value):
            raise forms.ValidationError("Use a GitHub username without @ or whitespace.")
        return value

    def clean_github_token(self):
        value = self.cleaned_data["github_token"]
        if any(character.isspace() for character in value):
            raise forms.ValidationError("GitHub tokens cannot contain whitespace or newlines.")
        return value

    def clean_required_checks(self):
        return list(
            dict.fromkeys(
                line.strip()
                for line in self.cleaned_data["required_checks"].splitlines()
                if line.strip()
            )
        )

    def clean(self):
        data = super().clean()
        if not data.get("github_credential") and not data.get("github_token"):
            self.add_error("github_credential", "Choose a connection or enter a GitHub token.")
        labels = [data.get(field) for field in ("review_label", "feedback_label", "approved_label")]
        if len(set(labels)) != 3:
            raise forms.ValidationError("Workflow labels must be distinct.")
        if data.get("github_token"):
            try:
                self.encrypted_token = encrypt(data["github_token"])
            except (ValueError, RuntimeError):
                self.add_error("github_token", "Credential encryption is not configured correctly.")
        return data
