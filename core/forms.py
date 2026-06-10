import json

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
from .security import DEFAULT_REPORT_URL_WHITELIST, split_policy_lines

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
            "query_timeout_seconds",
            "report_cache_enabled",
            "cache_ttl_seconds",
            "max_rows",
            "max_raw_bytes",
            "max_compressed_bytes",
        ]
        widgets = {
            "query_timeout_seconds": forms.NumberInput(
                attrs={"class": "form-control", "min": 1}
            ),
            "report_cache_enabled": forms.CheckboxInput(
                attrs={"class": "form-check-input", "role": "switch"}
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
            "query_timeout_seconds": "Maximum time a report query may run before it is stopped.",
            "report_cache_enabled": "Reuse report dataset results until the TTL expires. Turn this off to run SQL on every report load.",
            "cache_ttl_seconds": "How long cached report data stays fresh. Use 86400 for 24 hours.",
            "max_rows": "Maximum rows a report dataset can return to the browser.",
            "max_raw_bytes": "Maximum uncompressed JSON payload size returned to the browser.",
            "max_compressed_bytes": "Maximum compressed cache entry size stored in the app database.",
        }


class OrganizationSSOForm(forms.ModelForm):
    client_secret = forms.CharField(
        label="Client secret",
        required=False,
        strip=False,
        widget=forms.PasswordInput(attrs={"class": "form-control"}),
        help_text="Leave blank to keep the existing encrypted secret.",
    )

    class Meta:
        model = Organization
        fields = [
            "sso_oidc_enabled",
            "sso_required",
            "sso_oidc_issuer_url",
            "sso_oidc_client_id",
            "sso_oidc_scopes",
        ]
        widgets = {
            "sso_oidc_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "sso_required": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "sso_oidc_issuer_url": forms.URLInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "https://your-company.okta.com/oauth2/default",
                }
            ),
            "sso_oidc_client_id": forms.TextInput(attrs={"class": "form-control"}),
            "sso_oidc_scopes": forms.TextInput(attrs={"class": "form-control"}),
        }
        labels = {
            "sso_oidc_enabled": "Enable OIDC SSO",
            "sso_required": "Require SSO for this organization",
            "sso_oidc_issuer_url": "Issuer URL",
            "sso_oidc_client_id": "Client ID",
            "sso_oidc_scopes": "Scopes",
        }
        help_texts = {
            "sso_oidc_enabled": "Turn this on after registering the callback URL in your identity provider.",
            "sso_required": "Blocks password login for non-staff users in this organization.",
            "sso_oidc_issuer_url": "The OIDC issuer URL. For Okta this often ends with /oauth2/default.",
            "sso_oidc_client_id": "The client ID from your identity provider.",
            "sso_oidc_scopes": "Most OIDC providers should use: openid email profile.",
        }

    def clean(self):
        cleaned_data = super().clean()
        oidc_enabled = cleaned_data.get("sso_oidc_enabled")
        if oidc_enabled:
            for field_name in ["sso_oidc_issuer_url", "sso_oidc_client_id", "sso_oidc_scopes"]:
                if not cleaned_data.get(field_name):
                    self.add_error(field_name, "This field is required when SSO is enabled.")
            if not self.instance.encrypted_sso_oidc_client_secret and not self.cleaned_data.get("client_secret"):
                self.add_error("client_secret", "Add a client secret before enabling SSO.")
        return cleaned_data

    def save(self, commit=True):
        organization = super().save(commit=False)
        client_secret = self.cleaned_data.get("client_secret")
        if client_secret:
            organization.set_sso_oidc_client_secret(client_secret)
        if commit:
            organization.save()
        return organization


class OrganizationSecurityForm(forms.ModelForm):
    class Meta:
        model = Organization
        fields = [
            "report_ip_allowlist_enabled",
            "report_ip_allowlist",
            "report_url_whitelist_enabled",
            "report_url_whitelist",
            "report_url_blacklist_enabled",
            "report_url_blacklist",
        ]
        widgets = {
            "report_ip_allowlist_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "report_ip_allowlist": forms.Textarea(
                attrs={
                    "class": "form-control font-monospace",
                    "rows": 5,
                    "placeholder": "203.0.113.10\n198.51.100.0/24",
                }
            ),
            "report_url_whitelist_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "report_url_whitelist": forms.Textarea(
                attrs={
                    "class": "form-control font-monospace",
                    "rows": 8,
                }
            ),
            "report_url_blacklist_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "report_url_blacklist": forms.Textarea(
                attrs={
                    "class": "form-control font-monospace",
                    "rows": 5,
                    "placeholder": "example.com\ntracking.vendor.com",
                }
            ),
        }
        labels = {
            "report_ip_allowlist_enabled": "Restrict report access by IP",
            "report_ip_allowlist": "Allowed IPs and CIDR ranges",
            "report_url_whitelist_enabled": "Allow report requests only to approved domains",
            "report_url_whitelist": "Allowed report URL domains",
            "report_url_blacklist_enabled": "Block report requests to specific domains",
            "report_url_blacklist": "Blocked report URL domains",
        }
        help_texts = {
            "report_ip_allowlist_enabled": "When enabled, only requests from these IPs or CIDR ranges can open reports, previews, builders, or report data endpoints.",
            "report_ip_allowlist": "One IPv4/IPv6 address or CIDR range per line. Use your company VPN egress IPs.",
            "report_url_whitelist_enabled": "When enabled, report HTML can only load or fetch external URLs from these domains.",
            "report_url_whitelist": "One domain per line. Subdomains are allowed automatically.",
            "report_url_blacklist_enabled": "When enabled, these domains are blocked from report HTML even if outbound access is otherwise open.",
            "report_url_blacklist": "One domain per line. Subdomains are blocked automatically.",
        }

    def clean(self):
        cleaned_data = super().clean()
        whitelist_enabled = cleaned_data.get("report_url_whitelist_enabled")
        whitelist = cleaned_data.get("report_url_whitelist")
        if whitelist_enabled and not split_policy_lines(whitelist):
            cleaned_data["report_url_whitelist"] = "\n".join(DEFAULT_REPORT_URL_WHITELIST)
        if cleaned_data.get("report_ip_allowlist_enabled") and not split_policy_lines(
            cleaned_data.get("report_ip_allowlist")
        ):
            self.add_error(
                "report_ip_allowlist",
                "Add at least one allowed IP address or CIDR range before enabling this.",
            )
        if cleaned_data.get("report_url_blacklist_enabled") and not split_policy_lines(
            cleaned_data.get("report_url_blacklist")
        ):
            self.add_error(
                "report_url_blacklist",
                "Add at least one blocked domain before enabling this.",
            )
        return cleaned_data


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
    snowflake_auth_type = forms.ChoiceField(
        label="Authentication",
        required=False,
        choices=[
            ("programmatic_access_token", "Programmatic access token"),
            ("oauth", "OAuth token"),
            ("key_pair", "Key pair JWT"),
        ],
        initial="programmatic_access_token",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    snowflake_password = forms.CharField(
        label="Token",
        required=False,
        strip=False,
        widget=forms.PasswordInput(attrs={"class": "form-control"}),
        help_text="Use a programmatic access token or OAuth token. Password auth is not used by the Snowflake SQL API.",
    )
    snowflake_private_key = forms.CharField(
        label="Private key",
        required=False,
        strip=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control font-monospace",
                "rows": 5,
                "placeholder": "-----BEGIN PRIVATE KEY-----",
            }
        ),
        help_text="Only needed for key pair JWT authentication.",
    )
    snowflake_private_key_passphrase = forms.CharField(
        label="Private key passphrase",
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
    bigquery_location = forms.CharField(
        label="Location",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
        help_text="Optional BigQuery job location, for example US or us-central1.",
    )
    bigquery_service_account_json = forms.CharField(
        label="Service account JSON",
        required=False,
        strip=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control font-monospace",
                "rows": 6,
                "placeholder": '{"type":"service_account", ...}',
            }
        ),
        help_text="Paste a read-only service account JSON key. It is encrypted and never shown again.",
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
            "snowflake_database",
        ],
        DatabaseConnection.Provider.BIGQUERY: [
            "bigquery_project",
            "bigquery_dataset",
            "bigquery_service_account_json",
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
            auth_type = self.cleaned_data.get("snowflake_auth_type") or "programmatic_access_token"
            if auth_type in {"programmatic_access_token", "oauth"} and not self.cleaned_data.get("snowflake_password"):
                self.add_error("snowflake_password", "This field is required for token authentication.")
                return ""
            if auth_type == "key_pair" and not self.cleaned_data.get("snowflake_private_key"):
                self.add_error("snowflake_private_key", "This field is required for key pair authentication.")
                return ""
            return build_snowflake_connection_string(
                self.cleaned_data["snowflake_account"],
                self.cleaned_data["snowflake_username"],
                self.cleaned_data["snowflake_password"],
                self.cleaned_data["snowflake_database"],
                self.cleaned_data.get("snowflake_schema"),
                self.cleaned_data.get("snowflake_warehouse"),
                self.cleaned_data.get("snowflake_role"),
                auth_type=auth_type,
                private_key=self.cleaned_data.get("snowflake_private_key"),
                private_key_passphrase=self.cleaned_data.get("snowflake_private_key_passphrase"),
            )
        if provider == DatabaseConnection.Provider.BIGQUERY:
            service_account_json = self.cleaned_data.get("bigquery_service_account_json")
            try:
                parsed_service_account = json.loads(service_account_json)
            except (TypeError, json.JSONDecodeError):
                self.add_error("bigquery_service_account_json", "Paste a valid service account JSON key.")
                return ""
            required_keys = {"client_email", "private_key", "token_uri"}
            missing_keys = sorted(required_keys - set(parsed_service_account))
            if missing_keys:
                self.add_error(
                    "bigquery_service_account_json",
                    f"Service account JSON is missing: {', '.join(missing_keys)}.",
                )
                return ""
            return build_bigquery_connection_string(
                self.cleaned_data["bigquery_project"],
                self.cleaned_data["bigquery_dataset"],
                json.dumps(parsed_service_account, separators=(",", ":")),
                self.cleaned_data.get("bigquery_location"),
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
            "snowflake_auth_type",
            "snowflake_password",
            "snowflake_private_key",
            "snowflake_private_key_passphrase",
            "snowflake_schema",
            "snowflake_warehouse",
            "snowflake_role",
            "bigquery_location",
            "bigquery_service_account_json",
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
        if selected_key and not self.is_bound:
            self.initial["ai_provider_key"] = selected_key
            self.initial["ai_model_name"] = self._selected_model_name(selected_key)

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

    def _selected_model_name(self, provider_key):
        allowed_models = {model_id for model_id, _name in self._model_choices(provider_key)}
        if (
            self.instance
            and self.instance.ai_provider_key_id == provider_key.id
            and self.instance.ai_model_name in allowed_models
        ):
            return self.instance.ai_model_name
        if provider_key.model_name in allowed_models:
            return provider_key.model_name
        return next(iter(allowed_models), "")

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
