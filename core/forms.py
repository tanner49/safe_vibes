from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm

from .ai_provider_models import (
    get_curated_model_choices,
    get_default_model_for_provider,
)
from .database_connections import DatabaseConnectionError, sanitize_connection_string
from .models import AIProviderKey, DatabaseConnection, Membership, Organization


class OrganizationAwareAuthenticationForm(AuthenticationForm):
    def __init__(self, request=None, *args, **kwargs):
        super().__init__(request, *args, **kwargs)
        self.fields["username"].label = "Email"
        self.fields["username"].widget.attrs.update(
            {
                "autocomplete": "email",
                "class": "form-control",
                "inputmode": "email",
            }
        )
        self.fields["password"].widget.attrs.update({"class": "form-control"})

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)

        if user.is_staff or user.is_superuser:
            return

        sso_required = user.organization_memberships.filter(
            organization__sso_required=True
        ).exists()
        if sso_required:
            raise forms.ValidationError(
                "Password login is disabled for your organization. Use SSO instead.",
                code="sso_required",
            )


class OrganizationPolicyForm(forms.ModelForm):
    class Meta:
        model = Organization
        fields = [
            "sso_required",
            "query_timeout_seconds",
            "cache_ttl_seconds",
            "max_rows",
            "max_raw_bytes",
            "max_compressed_bytes",
        ]
        widgets = {
            "sso_required": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "query_timeout_seconds": forms.NumberInput(
                attrs={"class": "form-control", "min": 1}
            ),
            "cache_ttl_seconds": forms.NumberInput(
                attrs={"class": "form-control", "min": 0}
            ),
            "max_rows": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "max_raw_bytes": forms.NumberInput(
                attrs={"class": "form-control", "min": 1}
            ),
            "max_compressed_bytes": forms.NumberInput(
                attrs={"class": "form-control", "min": 1}
            ),
        }
        help_texts = {
            "sso_required": "Require users in this organization to sign in with SSO once it is configured.",
            "query_timeout_seconds": "Maximum time a report query may run before it is stopped.",
            "cache_ttl_seconds": "How long cached report data stays fresh. Use 86400 for 24 hours.",
            "max_rows": "Maximum rows a report dataset can return to the browser.",
            "max_raw_bytes": "Maximum uncompressed JSON payload size returned to the browser.",
            "max_compressed_bytes": "Maximum compressed cache entry size stored in the app database.",
        }


class CompanyUserCreateForm(forms.Form):
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"class": "form-control"}),
    )
    first_name = forms.CharField(
        required=False,
        max_length=150,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    last_name = forms.CharField(
        required=False,
        max_length=150,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    role = forms.ChoiceField(
        choices=Membership.Role.choices,
        initial=Membership.Role.VIEWER,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    password1 = forms.CharField(
        label="Temporary password",
        strip=False,
        widget=forms.PasswordInput(attrs={"class": "form-control"}),
    )
    password2 = forms.CharField(
        label="Confirm temporary password",
        strip=False,
        widget=forms.PasswordInput(attrs={"class": "form-control"}),
    )

    def clean_email(self):
        email = self.cleaned_data["email"]
        if get_user_model().objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("A user with that email already exists.")
        return email

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            raise forms.ValidationError("The temporary passwords do not match.")
        return cleaned_data

    def save(self, organization):
        user = get_user_model().objects.create_user(
            email=self.cleaned_data["email"],
            password=self.cleaned_data["password1"],
            first_name=self.cleaned_data["first_name"],
            last_name=self.cleaned_data["last_name"],
        )
        Membership.objects.create(
            organization=organization,
            user=user,
            role=self.cleaned_data["role"],
        )
        return user


class AIProviderKeyCreateForm(forms.ModelForm):
    model_name = forms.ChoiceField(
        label="Default model",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    allowed_model_ids = forms.MultipleChoiceField(
        label="Allowed models",
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Select the models report creators can use with this key.",
    )
    api_key = forms.CharField(
        label="API key",
        strip=False,
        widget=forms.PasswordInput(attrs={"class": "form-control"}),
        help_text="The key is encrypted before it is stored and will not be shown again.",
    )

    class Meta:
        model = AIProviderKey
        fields = ["name", "provider", "model_name"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "provider": forms.Select(attrs={"class": "form-select"}),
        }
        help_texts = {
            "name": "A friendly label for this provider key.",
            "provider": "OpenAI is the first supported provider for the MVP.",
            "model_name": "Default curated model to use when generating or revising reports.",
        }

    def save(self, organization):
        provider_key = super().save(commit=False)
        provider_key.organization = organization
        provider_key.set_api_key(self.cleaned_data["api_key"])
        provider_key.save()
        return provider_key

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        provider = self._selected_provider()
        choices = get_curated_model_choices(provider)
        if provider and not self.initial.get("model_name"):
            self.initial["model_name"] = get_default_model_for_provider(provider)
        self.fields["model_name"].choices = choices
        self.fields["allowed_model_ids"].choices = choices
        self.fields["allowed_model_ids"].initial = [choice[0] for choice in choices]

    def _selected_provider(self):
        if self.is_bound:
            return self.data.get("provider") or AIProviderKey.Provider.OPENAI
        return self.initial.get("provider") or AIProviderKey.Provider.OPENAI

    def clean(self):
        cleaned_data = super().clean()
        provider = cleaned_data.get("provider")
        valid_model_ids = {
            model_id for model_id, _display_name in get_curated_model_choices(provider)
        }
        model_name = cleaned_data.get("model_name")
        allowed_model_ids = set(cleaned_data.get("allowed_model_ids") or [])
        if model_name and model_name not in valid_model_ids:
            self.add_error("model_name", "Choose a curated model for this provider.")
        if model_name and model_name not in allowed_model_ids:
            self.add_error(
                "allowed_model_ids",
                "The default model must be allowed.",
            )
        return cleaned_data


class DatabaseConnectionCreateForm(forms.ModelForm):
    connection_string = forms.CharField(
        label="SQLAlchemy connection string",
        strip=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control font-monospace",
                "rows": 3,
                "placeholder": "postgresql+psycopg://readonly:password@host:5432/dbname",
            }
        ),
        help_text="Use read-only credentials. The full string is encrypted and never shown again.",
    )

    class Meta:
        model = DatabaseConnection
        fields = ["name", "provider", "enabled"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "provider": forms.Select(attrs={"class": "form-select"}),
            "enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }
        help_texts = {
            "name": "A friendly label report creators will recognize.",
            "provider": "This helps the product explain and organize connections. The SQLAlchemy string is still the source of truth.",
            "enabled": "Allow reports to use this connection.",
        }

    def clean_connection_string(self):
        connection_string = self.cleaned_data["connection_string"]
        try:
            self.connection_string_preview = sanitize_connection_string(connection_string)
        except DatabaseConnectionError as exc:
            raise forms.ValidationError(str(exc)) from exc
        return connection_string

    def save(self, organization):
        database_connection = super().save(commit=False)
        database_connection.organization = organization
        database_connection.set_connection_string(
            self.cleaned_data["connection_string"],
            self.connection_string_preview,
        )
        database_connection.save()
        return database_connection


class DatabaseConnectionUpdateForm(forms.ModelForm):
    connection_string = forms.CharField(
        label="Replace connection string",
        required=False,
        strip=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control font-monospace",
                "rows": 3,
            }
        ),
        help_text="Leave blank to keep the existing encrypted connection string.",
    )

    class Meta:
        model = DatabaseConnection
        fields = ["name", "provider", "enabled"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "provider": forms.Select(attrs={"class": "form-select"}),
            "enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }
        help_texts = {
            "name": "A friendly label report creators will recognize.",
            "provider": "This helps the product explain and organize connections. The SQLAlchemy string is still the source of truth.",
            "enabled": "Allow reports to use this connection.",
        }

    def clean_connection_string(self):
        connection_string = self.cleaned_data["connection_string"]
        if not connection_string:
            return connection_string
        try:
            self.connection_string_preview = sanitize_connection_string(connection_string)
        except DatabaseConnectionError as exc:
            raise forms.ValidationError(str(exc)) from exc
        return connection_string

    def save(self, commit=True):
        database_connection = super().save(commit=False)
        connection_string = self.cleaned_data.get("connection_string")
        if connection_string:
            database_connection.set_connection_string(
                connection_string,
                self.connection_string_preview,
            )
        if commit:
            database_connection.save()
        return database_connection


class AIProviderKeyUpdateForm(forms.ModelForm):
    model_name = forms.ChoiceField(
        label="Default model",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    api_key = forms.CharField(
        label="Replace API key",
        required=False,
        strip=False,
        widget=forms.PasswordInput(attrs={"class": "form-control"}),
        help_text="Leave blank to keep the existing encrypted key.",
    )
    allowed_model_ids = forms.MultipleChoiceField(
        label="Allowed models",
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Select the models report creators can use with this key.",
    )

    class Meta:
        model = AIProviderKey
        fields = ["name", "model_name"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
        }
        help_texts = {
            "name": "A friendly label for this provider key.",
            "model_name": "Default curated model to use when generating or revising reports.",
        }

    def save(self, commit=True):
        provider_key = super().save(commit=False)
        api_key = self.cleaned_data.get("api_key")
        if api_key:
            provider_key.set_api_key(api_key)
        if commit:
            provider_key.save()
        return provider_key

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        provider = (
            self.instance.provider
            if self.instance and self.instance.pk
            else AIProviderKey.Provider.OPENAI
        )
        choices = get_curated_model_choices(provider)
        if self.instance and self.instance.model_name:
            choice_values = {choice[0] for choice in choices}
            if self.instance.model_name not in choice_values:
                choices = [(self.instance.model_name, self.instance.model_name)] + choices
        self.fields["model_name"].choices = choices
        self.fields["allowed_model_ids"].choices = choices
        if self.instance and self.instance.pk:
            allowed_ids = list(
                self.instance.available_models.filter(allowed=True).values_list(
                    "provider_model_id",
                    flat=True,
                )
            )
            self.fields["allowed_model_ids"].initial = allowed_ids or [
                choice[0] for choice in choices
            ]
        else:
            self.fields["allowed_model_ids"].initial = [choice[0] for choice in choices]

    def clean(self):
        cleaned_data = super().clean()
        provider = (
            self.instance.provider
            if self.instance and self.instance.pk
            else AIProviderKey.Provider.OPENAI
        )
        valid_model_ids = {
            model_id for model_id, _display_name in get_curated_model_choices(provider)
        }
        model_name = cleaned_data.get("model_name")
        allowed_model_ids = set(cleaned_data.get("allowed_model_ids") or [])
        if model_name and model_name not in valid_model_ids:
            self.add_error("model_name", "Choose a curated model for this provider.")
        if model_name and model_name not in allowed_model_ids:
            self.add_error(
                "allowed_model_ids",
                "The default model must be allowed.",
            )
        return cleaned_data
