from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm

from .ai_provider_models import (
    get_curated_model_choices,
    get_default_model_for_provider,
)
from .database_connections import (
    DatabaseConnectionError,
    build_bigquery_connection_string,
    build_postgres_connection_string,
    build_snowflake_connection_string,
    build_sqlite_connection_string,
    sanitize_connection_string,
)
from .models import AIProviderKey, DatabaseConnection, Membership, Organization, Report

User = get_user_model()


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


class DatabaseConnectionFormMixin(forms.Form):
    connection_string = forms.CharField(
        label="Connection string",
        required=False,
        strip=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control font-monospace",
                "rows": 3,
                "placeholder": "postgresql+psycopg://readonly:password@host:5432/dbname",
            }
        ),
        help_text="For custom connections only. The full string is encrypted and never shown again.",
    )
    db_host = forms.CharField(
        label="Host",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    db_port = forms.IntegerField(
        label="Port",
        required=False,
        widget=forms.NumberInput(attrs={"class": "form-control"}),
    )
    db_name = forms.CharField(
        label="Database",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    db_username = forms.CharField(
        label="Username",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    db_password = forms.CharField(
        label="Password",
        required=False,
        strip=False,
        widget=forms.PasswordInput(attrs={"class": "form-control"}),
    )
    postgres_sslmode = forms.ChoiceField(
        label="SSL mode",
        required=False,
        choices=[
            ("require", "Require SSL"),
            ("prefer", "Prefer SSL"),
            ("disable", "Disable SSL"),
            ("", "Unspecified"),
        ],
        initial="require",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    sqlite_path = forms.CharField(
        label="SQLite database path",
        required=False,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "demo.sqlite3 or :memory:"}
        ),
    )
    snowflake_account = forms.CharField(
        label="Account identifier",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    snowflake_username = forms.CharField(
        label="Username",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    snowflake_password = forms.CharField(
        label="Password",
        required=False,
        strip=False,
        widget=forms.PasswordInput(attrs={"class": "form-control"}),
    )
    snowflake_database = forms.CharField(
        label="Database",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    snowflake_schema = forms.CharField(
        label="Schema",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    snowflake_warehouse = forms.CharField(
        label="Warehouse",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    snowflake_role = forms.CharField(
        label="Role",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    bigquery_project = forms.CharField(
        label="Project ID",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    bigquery_dataset = forms.CharField(
        label="Dataset",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    bigquery_credentials_path = forms.CharField(
        label="Service account JSON path",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
        help_text="Path on the server/container where the service account JSON is mounted.",
    )

    provider_field_names = {
        DatabaseConnection.Provider.POSTGRES: [
            "db_host",
            "db_port",
            "db_name",
            "db_username",
            "db_password",
        ],
        DatabaseConnection.Provider.SNOWFLAKE: [
            "snowflake_account",
            "snowflake_username",
            "snowflake_password",
            "snowflake_database",
        ],
        DatabaseConnection.Provider.BIGQUERY: [
            "bigquery_project",
            "bigquery_dataset",
        ],
        DatabaseConnection.Provider.SQLITE: ["sqlite_path"],
        DatabaseConnection.Provider.CUSTOM: ["connection_string"],
    }

    def _selected_provider(self):
        if self.is_bound:
            return self.data.get("provider") or DatabaseConnection.Provider.POSTGRES
        if self.instance and self.instance.pk:
            return self.instance.provider
        return self.initial.get("provider") or DatabaseConnection.Provider.POSTGRES

    def _field_value(self, field_name):
        return self.cleaned_data.get(field_name)

    def _require_provider_fields(self, provider):
        missing_fields = []
        for field_name in self.provider_field_names.get(provider, []):
            if not self._field_value(field_name):
                missing_fields.append(field_name)
                self.add_error(field_name, "This field is required for this provider.")
        return not missing_fields

    def build_connection_string(self):
        provider = self.cleaned_data.get("provider")
        if not self._require_provider_fields(provider):
            return ""

        if provider == DatabaseConnection.Provider.POSTGRES:
            return build_postgres_connection_string(
                self.cleaned_data["db_host"],
                self.cleaned_data.get("db_port"),
                self.cleaned_data["db_name"],
                self.cleaned_data["db_username"],
                self.cleaned_data["db_password"],
                self.cleaned_data.get("postgres_sslmode"),
            )
        if provider == DatabaseConnection.Provider.SNOWFLAKE:
            return build_snowflake_connection_string(
                self.cleaned_data["snowflake_account"],
                self.cleaned_data["snowflake_username"],
                self.cleaned_data["snowflake_password"],
                self.cleaned_data["snowflake_database"],
                self.cleaned_data.get("snowflake_schema"),
                self.cleaned_data.get("snowflake_warehouse"),
                self.cleaned_data.get("snowflake_role"),
            )
        if provider == DatabaseConnection.Provider.BIGQUERY:
            return build_bigquery_connection_string(
                self.cleaned_data["bigquery_project"],
                self.cleaned_data["bigquery_dataset"],
                self.cleaned_data.get("bigquery_credentials_path"),
            )
        if provider == DatabaseConnection.Provider.SQLITE:
            return build_sqlite_connection_string(self.cleaned_data["sqlite_path"])
        if provider == DatabaseConnection.Provider.CUSTOM:
            return self.cleaned_data["connection_string"]
        return ""

    def clean(self):
        cleaned_data = super().clean()
        if self.errors:
            return cleaned_data

        connection_string = self.build_connection_string()
        if not connection_string:
            return cleaned_data
        try:
            self.connection_string_preview = sanitize_connection_string(connection_string)
        except DatabaseConnectionError as exc:
            provider = cleaned_data.get("provider")
            target_field = (
                "connection_string"
                if provider == DatabaseConnection.Provider.CUSTOM
                else None
            )
            if target_field:
                self.add_error(target_field, str(exc))
            else:
                raise forms.ValidationError(str(exc)) from exc
        self.cleaned_connection_string = connection_string
        return cleaned_data


class DatabaseConnectionCreateForm(DatabaseConnectionFormMixin, forms.ModelForm):

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
            "provider": "Choose a common database for guided setup, or custom if you already have a database connection string.",
            "enabled": "Allow reports to use this connection.",
        }

    def save(self, organization):
        database_connection = super().save(commit=False)
        database_connection.organization = organization
        database_connection.set_connection_string(
            self.cleaned_connection_string,
            self.connection_string_preview,
        )
        database_connection.save()
        return database_connection


class DatabaseConnectionUpdateForm(DatabaseConnectionFormMixin, forms.ModelForm):
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
            "provider": "Choose a common database for guided setup, or custom if you already have a database connection string.",
            "enabled": "Allow reports to use this connection.",
        }

    def _has_replacement_details(self):
        provider = self.cleaned_data.get("provider")
        field_names = self.provider_field_names.get(provider, [])
        optional_field_names = [
            "snowflake_schema",
            "snowflake_warehouse",
            "snowflake_role",
            "bigquery_credentials_path",
        ]
        return any(
            self.cleaned_data.get(field_name)
            for field_name in field_names + optional_field_names
        )

    def clean(self):
        if not self.is_bound:
            return super().clean()
        cleaned_data = forms.ModelForm.clean(self)
        if self.errors:
            return cleaned_data
        if not self._has_replacement_details():
            return cleaned_data

        connection_string = self.build_connection_string()
        if not connection_string:
            return cleaned_data
        try:
            self.connection_string_preview = sanitize_connection_string(connection_string)
        except DatabaseConnectionError as exc:
            provider = cleaned_data.get("provider")
            if provider == DatabaseConnection.Provider.CUSTOM:
                self.add_error("connection_string", str(exc))
            else:
                raise forms.ValidationError(str(exc)) from exc
        self.cleaned_connection_string = connection_string
        return cleaned_data

    def save(self, commit=True):
        database_connection = super().save(commit=False)
        if hasattr(self, "cleaned_connection_string"):
            database_connection.set_connection_string(
                self.cleaned_connection_string,
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


class ReportStartForm(forms.Form):
    ai_provider_key = forms.ModelChoiceField(
        label="AI provider",
        queryset=AIProviderKey.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    ai_model_name = forms.ChoiceField(
        label="Model",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    message = forms.CharField(
        label="Message",
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 5,
                "placeholder": "Ask for the report you want to build...",
            }
        ),
    )

    def __init__(self, organization, *args, **kwargs):
        super().__init__(*args, **kwargs)
        provider_keys = organization.ai_provider_keys.all()
        self.fields["ai_provider_key"].queryset = provider_keys
        selected_key = self._selected_provider_key(provider_keys)
        self.fields["ai_model_name"].choices = self._model_choices(selected_key)

    def _selected_provider_key(self, provider_keys):
        selected_id = self.data.get("ai_provider_key") if self.is_bound else None
        if selected_id:
            for provider_key in provider_keys:
                if str(provider_key.id) == str(selected_id):
                    return provider_key
        return provider_keys.first()

    def _model_choices(self, provider_key):
        if not provider_key:
            return []
        choices = list(
            provider_key.available_models.filter(
                allowed=True,
                available=True,
            ).values_list("provider_model_id", "display_name")
        )
        if choices:
            return choices
        return get_curated_model_choices(provider_key.provider)

    def clean_ai_model_name(self):
        model_name = self.cleaned_data["ai_model_name"]
        provider_key = self.cleaned_data.get("ai_provider_key")
        allowed_models = {model_id for model_id, _name in self._model_choices(provider_key)}
        if model_name not in allowed_models:
            raise forms.ValidationError("Choose an allowed model for this provider.")
        return model_name


class ReportAIModelForm(forms.ModelForm):
    ai_model_name = forms.ChoiceField(
        label="Model",
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )

    class Meta:
        model = Report
        fields = ["ai_provider_key", "ai_model_name"]
        widgets = {
            "ai_provider_key": forms.Select(attrs={"class": "form-select form-select-sm"}),
        }

    def __init__(self, organization, *args, **kwargs):
        super().__init__(*args, **kwargs)
        provider_keys = organization.ai_provider_keys.order_by("provider", "name")
        self.fields["ai_provider_key"].queryset = provider_keys
        self.fields["ai_provider_key"].required = True
        selected_key = self._selected_provider_key(provider_keys)
        self.fields["ai_model_name"].choices = self._model_choices(selected_key)

    def _selected_provider_key(self, provider_keys):
        selected_id = self.data.get("ai_provider_key") if self.is_bound else None
        if selected_id:
            for provider_key in provider_keys:
                if str(provider_key.id) == str(selected_id):
                    return provider_key
        if self.instance and self.instance.ai_provider_key_id:
            for provider_key in provider_keys:
                if provider_key.id == self.instance.ai_provider_key_id:
                    return provider_key
        return provider_keys.first()

    def _model_choices(self, provider_key):
        if not provider_key:
            return []
        choices = list(
            provider_key.available_models.filter(
                allowed=True,
                available=True,
            ).values_list("provider_model_id", "display_name")
        )
        if choices:
            return choices
        return get_curated_model_choices(provider_key.provider)

    def clean_ai_model_name(self):
        model_name = self.cleaned_data["ai_model_name"]
        provider_key = self.cleaned_data.get("ai_provider_key")
        allowed_models = {model_id for model_id, _name in self._model_choices(provider_key)}
        if model_name not in allowed_models:
            raise forms.ValidationError("Choose an allowed model for this provider.")
        return model_name


class ReportSharingForm(forms.ModelForm):
    class Meta:
        model = Report
        fields = ["sharing_scope", "shared_with"]
        widgets = {
            "sharing_scope": forms.Select(attrs={"class": "form-select"}),
            "shared_with": forms.CheckboxSelectMultiple(),
        }
        labels = {
            "sharing_scope": "Who can view this report?",
            "shared_with": "Specific people",
        }

    def __init__(self, organization, *args, **kwargs):
        super().__init__(*args, **kwargs)
        selected_ids = []
        if self.is_bound:
            selected_ids = self.data.getlist("shared_with")
        elif self.instance and self.instance.pk:
            selected_ids = list(self.instance.shared_with.values_list("id", flat=True))
        self.fields["shared_with"].queryset = User.objects.filter(
            organization_memberships__organization=organization,
            id__in=selected_ids,
        ).order_by("email").distinct()
        self.fields["shared_with"].required = False


class ReportImportForm(forms.Form):
    primary_sql = forms.CharField(
        label="SQL",
        widget=forms.Textarea(
            attrs={
                "class": "form-control font-monospace",
                "rows": 8,
                "placeholder": "Paste the SQL that powers this report...",
            }
        ),
    )
    html = forms.CharField(
        label="HTML",
        widget=forms.Textarea(
            attrs={
                "class": "form-control font-monospace",
                "rows": 10,
                "placeholder": "Paste the existing HTML/JavaScript report...",
            }
        ),
    )
    instructions = forms.CharField(
        label="Instructions",
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Optional: tell the AI what to preserve or change...",
            }
        ),
    )


class ReportUpdateForm(forms.ModelForm):
    class Meta:
        model = Report
        fields = ["title", "database_connection", "ai_provider_key", "ai_model_name", "primary_sql", "html"]
        widgets = {
            "title": forms.TextInput(attrs={"class": "form-control"}),
            "database_connection": forms.Select(attrs={"class": "form-select"}),
            "ai_provider_key": forms.Select(attrs={"class": "form-select"}),
            "ai_model_name": forms.TextInput(attrs={"class": "form-control"}),
            "primary_sql": forms.Textarea(attrs={"class": "form-control font-monospace", "rows": 8}),
            "html": forms.Textarea(attrs={"class": "form-control font-monospace", "rows": 12}),
        }

    def __init__(self, organization, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["database_connection"].queryset = organization.database_connections.filter(enabled=True)
        self.fields["database_connection"].required = False
        self.fields["ai_provider_key"].queryset = organization.ai_provider_keys.all()
        self.fields["ai_provider_key"].required = False


class ReportChatForm(forms.Form):
    message = forms.CharField(
        label="Message",
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Ask AI to create or revise this report...",
            }
        ),
    )
