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
