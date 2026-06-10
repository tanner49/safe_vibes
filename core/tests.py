import json
import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .ai_provider_models import (
    CURATED_ANTHROPIC_MODELS,
    CURATED_GEMINI_MODELS,
    CURATED_OPENAI_MODELS,
    sync_provider_models,
)
from .database_connections import (
    build_bigquery_connection_string,
    build_snowflake_connection_string,
    parse_connection_config,
    redact_connection_error,
)
from .demo_database import demo_database_connection_string
from .ai_clients import AIMessage, generate_openai_text, provider_error_message, stream_openai_text
from .query_execution import (
    QueryExecutionError,
    QueryExecutionResult,
    QueryPolicyError,
    async_execute_query,
    async_sqlalchemy_connection_string,
    execute_query,
)
from . import report_cache
from .report_generation import ReportGenerationError
from .security import DEFAULT_REPORT_URL_WHITELIST
from .forms import AIProviderKeyCreateForm, DatabaseConnectionCreateForm
from .models import (
    AIModelCatalog,
    AIProviderKey,
    AIProviderModel,
    DatabaseConnection,
    Membership,
    Organization,
    QueryExecutionLog,
    Report,
    ReportChatMessage,
    ReportDatasetCache,
    ReportDatasetCacheLock,
)


def create_user(email, password="password-12345", **extra_fields):
    return get_user_model().objects.create_user(
        email=email,
        password=password,
        **extra_fields,
    )


def curated_model_ids(provider=AIProviderKey.Provider.OPENAI):
    models_by_provider = {
        AIProviderKey.Provider.OPENAI: CURATED_OPENAI_MODELS,
        AIProviderKey.Provider.ANTHROPIC: CURATED_ANTHROPIC_MODELS,
        AIProviderKey.Provider.GEMINI: CURATED_GEMINI_MODELS,
    }
    return [model["id"] for model in models_by_provider[provider]]


class OrganizationModelTests(TestCase):
    def test_organization_slug_is_generated(self):
        organization = Organization.objects.create(name="Acme Revenue Team")

        self.assertEqual(organization.slug, "acme-revenue-team")

    def test_organization_gets_demo_database_connection(self):
        organization = Organization.objects.create(name="Acme Revenue Team")

        database_connection = organization.database_connections.get(
            name="Demo SaaS Sales"
        )
        self.assertEqual(database_connection.provider, DatabaseConnection.Provider.SQLITE)
        self.assertTrue(database_connection.enabled)
        self.assertEqual(
            database_connection.get_connection_string(),
            demo_database_connection_string(),
        )

    def test_demo_database_backfill_command_refreshes_existing_organization(self):
        organization = Organization.objects.create(name="Acme Revenue Team")
        organization.database_connections.filter(name="Demo SaaS Sales").delete()

        call_command("ensure_demo_database", verbosity=0)

        self.assertTrue(
            organization.database_connections.filter(name="Demo SaaS Sales").exists()
        )


class NavigationTests(TestCase):
    def test_logged_out_home_does_not_show_django_admin_link(self):
        response = self.client.get(reverse("core:home"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Django Admin")


class AIClientTests(TestCase):
    def test_provider_error_message_summarizes_auth_failure(self):
        class ProviderException(Exception):
            status_code = 401
            code = "invalid_api_key"

        message = provider_error_message("OpenAI", ProviderException("raw provider error"))

        self.assertEqual(
            message,
            "OpenAI authentication failed. Check the saved API key.",
        )

    def test_openai_generate_uses_modern_completion_token_parameter(self):
        created_kwargs = {}

        class FakeOpenAIClient:
            def __init__(self, api_key):
                self.chat = types.SimpleNamespace(
                    completions=types.SimpleNamespace(create=self.create)
                )

            def create(self, **kwargs):
                created_kwargs.update(kwargs)
                message = types.SimpleNamespace(content="ok")
                choice = types.SimpleNamespace(message=message)
                return types.SimpleNamespace(choices=[choice])

        fake_openai = types.SimpleNamespace(
            OpenAI=FakeOpenAIClient,
            OpenAIError=Exception,
        )

        with patch.dict(sys.modules, {"openai": fake_openai}):
            response = generate_openai_text(
                "gpt-5.4-mini",
                "sk-test",
                [AIMessage(role="user", content="Build a report.")],
                "System prompt",
                1234,
            )

        self.assertEqual(response.content, "ok")
        self.assertEqual(created_kwargs["max_completion_tokens"], 1234)
        self.assertNotIn("max_tokens", created_kwargs)
        self.assertNotIn("temperature", created_kwargs)

    def test_openai_generate_omits_token_limit_by_default(self):
        created_kwargs = {}

        class FakeOpenAIClient:
            def __init__(self, api_key):
                self.chat = types.SimpleNamespace(
                    completions=types.SimpleNamespace(create=self.create)
                )

            def create(self, **kwargs):
                created_kwargs.update(kwargs)
                message = types.SimpleNamespace(content="ok")
                choice = types.SimpleNamespace(message=message)
                return types.SimpleNamespace(choices=[choice])

        fake_openai = types.SimpleNamespace(
            OpenAI=FakeOpenAIClient,
            OpenAIError=Exception,
        )

        with patch.dict(sys.modules, {"openai": fake_openai}):
            response = generate_openai_text(
                "gpt-5.4-mini",
                "sk-test",
                [AIMessage(role="user", content="Build a report.")],
                "System prompt",
                None,
            )

        self.assertEqual(response.content, "ok")
        self.assertNotIn("max_completion_tokens", created_kwargs)
        self.assertNotIn("max_tokens", created_kwargs)
        self.assertNotIn("temperature", created_kwargs)

    def test_openai_stream_uses_modern_completion_token_parameter(self):
        created_kwargs = {}

        class FakeOpenAIClient:
            def __init__(self, api_key):
                self.chat = types.SimpleNamespace(
                    completions=types.SimpleNamespace(create=self.create)
                )

            def create(self, **kwargs):
                created_kwargs.update(kwargs)
                delta = types.SimpleNamespace(content="streamed")
                choice = types.SimpleNamespace(delta=delta)
                return [types.SimpleNamespace(choices=[choice])]

        fake_openai = types.SimpleNamespace(
            OpenAI=FakeOpenAIClient,
            OpenAIError=Exception,
        )

        with patch.dict(sys.modules, {"openai": fake_openai}):
            chunks = list(
                stream_openai_text(
                    "gpt-5.4-mini",
                    "sk-test",
                    [AIMessage(role="user", content="Build a report.")],
                    "System prompt",
                    1234,
                )
            )

        self.assertEqual(chunks, ["streamed"])
        self.assertEqual(created_kwargs["max_completion_tokens"], 1234)
        self.assertTrue(created_kwargs["stream"])
        self.assertNotIn("max_tokens", created_kwargs)
        self.assertNotIn("temperature", created_kwargs)

    def test_openai_stream_omits_token_limit_by_default(self):
        created_kwargs = {}

        class FakeOpenAIClient:
            def __init__(self, api_key):
                self.chat = types.SimpleNamespace(
                    completions=types.SimpleNamespace(create=self.create)
                )

            def create(self, **kwargs):
                created_kwargs.update(kwargs)
                delta = types.SimpleNamespace(content="streamed")
                choice = types.SimpleNamespace(delta=delta)
                return [types.SimpleNamespace(choices=[choice])]

        fake_openai = types.SimpleNamespace(
            OpenAI=FakeOpenAIClient,
            OpenAIError=Exception,
        )

        with patch.dict(sys.modules, {"openai": fake_openai}):
            chunks = list(
                stream_openai_text(
                    "gpt-5.4-mini",
                    "sk-test",
                    [AIMessage(role="user", content="Build a report.")],
                    "System prompt",
                    None,
                )
            )

        self.assertEqual(chunks, ["streamed"])
        self.assertTrue(created_kwargs["stream"])
        self.assertNotIn("max_completion_tokens", created_kwargs)
        self.assertNotIn("max_tokens", created_kwargs)
        self.assertNotIn("temperature", created_kwargs)


class UserModelTests(TestCase):
    def test_user_uses_email_as_identity_and_internal_username(self):
        user = create_user("operator@example.com")

        self.assertEqual(user.email, "operator@example.com")
        self.assertEqual(user.username, "operator@example.com")


class LoginPolicyTests(TestCase):
    def test_password_login_is_blocked_when_organization_requires_sso(self):
        user = create_user("operator@example.com")
        organization = Organization.objects.create(
            name="Enterprise Account",
            sso_required=True,
        )
        Membership.objects.create(
            organization=organization,
            user=user,
            role=Membership.Role.ADMIN,
        )

        response = self.client.post(
            reverse("login"),
            {"username": "operator@example.com", "password": "password-12345"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Password login is disabled")

    def test_staff_password_login_is_allowed_when_organization_requires_sso(self):
        user = create_user("staff@example.com", is_staff=True)
        organization = Organization.objects.create(
            name="Enterprise Account",
            sso_required=True,
        )
        Membership.objects.create(
            organization=organization,
            user=user,
            role=Membership.Role.ADMIN,
        )

        response = self.client.post(
            reverse("login"),
            {"username": "staff@example.com", "password": "password-12345"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("core:reports_placeholder"))


class DashboardTests(TestCase):
    def test_dashboard_redirects_to_reports(self):
        user = create_user("creator@example.com")
        organization = Organization.objects.create(name="Demo Sales Org")
        Membership.objects.create(
            organization=organization,
            user=user,
            role=Membership.Role.CREATOR,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("core:dashboard"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("core:reports_placeholder"))

    def test_authenticated_home_redirects_to_reports(self):
        user = create_user("creator@example.com")
        organization = Organization.objects.create(name="Demo Sales Org")
        Membership.objects.create(
            organization=organization,
            user=user,
            role=Membership.Role.CREATOR,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("core:home"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("core:reports_placeholder"))

    def test_authenticated_nav_does_not_render_dashboard_link(self):
        user = create_user("admin@example.com")
        organization = Organization.objects.create(name="Demo Sales Org")
        Membership.objects.create(
            organization=organization,
            user=user,
            role=Membership.Role.ADMIN,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("core:reports_placeholder"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Dashboard")
        self.assertContains(response, reverse("core:builder_home"))
        self.assertContains(response, reverse("core:reports_placeholder"))


class CompanySettingsTests(TestCase):
    def test_company_admin_can_view_settings(self):
        user = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=user,
            role=Membership.Role.ADMIN,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("core:settings_home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Company Settings")
        self.assertContains(response, "Internal Test Org")

    def test_creator_cannot_view_company_settings(self):
        user = create_user("creator@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=user,
            role=Membership.Role.CREATOR,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("core:settings_home"))

        self.assertEqual(response.status_code, 403)

    def test_company_admin_can_update_org_report_policy(self):
        user = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=user,
            role=Membership.Role.ADMIN,
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("core:settings_report_limits"),
            {
                "sso_required": "on",
                "query_timeout_seconds": "90",
                "cache_ttl_seconds": "3600",
                "max_rows": "10000",
                "max_raw_bytes": "2000000",
                "max_compressed_bytes": "500000",
            },
        )

        organization.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("core:settings_home"))
        self.assertTrue(organization.sso_required)
        self.assertEqual(organization.query_timeout_seconds, 90)
        self.assertEqual(organization.cache_ttl_seconds, 3600)
        self.assertEqual(organization.max_rows, 10000)
        self.assertEqual(organization.max_raw_bytes, 2000000)
        self.assertEqual(organization.max_compressed_bytes, 500000)

    def test_company_admin_can_update_security_settings(self):
        user = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=user,
            role=Membership.Role.ADMIN,
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("core:settings_security"),
            {
                "report_ip_allowlist_enabled": "on",
                "report_ip_allowlist": "203.0.113.10\n198.51.100.0/24",
                "report_url_whitelist_enabled": "on",
                "report_url_whitelist": "cdn.example.com",
                "report_url_blacklist_enabled": "on",
                "report_url_blacklist": "tracker.example.com",
            },
        )

        organization.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("core:settings_security"))
        self.assertTrue(organization.report_ip_allowlist_enabled)
        self.assertIn("198.51.100.0/24", organization.report_ip_allowlist)
        self.assertTrue(organization.report_url_whitelist_enabled)
        self.assertEqual(organization.report_url_whitelist, "cdn.example.com")
        self.assertTrue(organization.report_url_blacklist_enabled)
        self.assertEqual(organization.report_url_blacklist, "tracker.example.com")

    def test_security_settings_page_includes_whitelist_autofill_helper(self):
        user = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=user,
            role=Membership.Role.ADMIN,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("core:settings_security"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "default-report-url-whitelist")
        self.assertContains(response, "id_report_url_whitelist_enabled")
        self.assertContains(response, "textarea.value = defaults.join")
        self.assertContains(response, "cdn.jsdelivr.net")

    def test_url_whitelist_autopopulates_common_library_domains(self):
        user = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=user,
            role=Membership.Role.ADMIN,
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("core:settings_security"),
            {
                "report_url_whitelist_enabled": "on",
                "report_url_whitelist": "",
            },
        )

        organization.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(organization.report_url_whitelist_enabled)
        for domain in DEFAULT_REPORT_URL_WHITELIST:
            self.assertIn(domain, organization.report_url_whitelist)

    def test_add_user_link_is_visible_when_sso_is_off(self):
        user = create_user("admin@example.com")
        organization = Organization.objects.create(
            name="Internal Test Org",
            sso_required=False,
        )
        Membership.objects.create(
            organization=organization,
            user=user,
            role=Membership.Role.ADMIN,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("core:settings_home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("core:settings_user_add"))
        self.assertContains(response, "Add user")

    def test_add_user_link_is_hidden_when_sso_is_on(self):
        user = create_user("admin@example.com")
        organization = Organization.objects.create(
            name="Internal Test Org",
            sso_required=True,
        )
        Membership.objects.create(
            organization=organization,
            user=user,
            role=Membership.Role.ADMIN,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("core:settings_home"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, reverse("core:settings_user_add"))
        self.assertContains(response, "identity provider")

    def test_company_admin_can_add_user_when_sso_is_off(self):
        admin = create_user("admin@example.com")
        organization = Organization.objects.create(
            name="Internal Test Org",
            sso_required=False,
        )
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        self.client.force_login(admin)

        response = self.client.post(
            reverse("core:settings_user_add"),
            {
                "email": "new-viewer@example.com",
                "first_name": "New",
                "last_name": "Viewer",
                "role": Membership.Role.VIEWER,
                "password1": "temporary-12345",
                "password2": "temporary-12345",
            },
        )

        self.assertEqual(response.status_code, 302)
        user = get_user_model().objects.get(email="new-viewer@example.com")
        self.assertEqual(user.username, "new-viewer@example.com")
        self.assertTrue(user.check_password("temporary-12345"))
        self.assertTrue(
            Membership.objects.filter(
                organization=organization,
                user=user,
                role=Membership.Role.VIEWER,
            ).exists()
        )

    def test_company_admin_cannot_add_user_when_sso_is_on(self):
        admin = create_user("admin@example.com")
        organization = Organization.objects.create(
            name="Internal Test Org",
            sso_required=True,
        )
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        self.client.force_login(admin)

        response = self.client.get(reverse("core:settings_user_add"))

        self.assertEqual(response.status_code, 403)

    def test_company_admin_can_remove_user_when_sso_is_off(self):
        admin = create_user("admin@example.com")
        user = create_user("viewer@example.com")
        organization = Organization.objects.create(
            name="Internal Test Org",
            sso_required=False,
        )
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        target_membership = Membership.objects.create(
            organization=organization,
            user=user,
            role=Membership.Role.VIEWER,
        )
        self.client.force_login(admin)

        response = self.client.post(
            reverse("core:settings_user_remove", args=[target_membership.id])
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Membership.objects.filter(id=target_membership.id).exists())
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_company_admin_cannot_remove_self(self):
        admin = create_user("admin@example.com")
        organization = Organization.objects.create(
            name="Internal Test Org",
            sso_required=False,
        )
        membership = Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        self.client.force_login(admin)

        response = self.client.post(
            reverse("core:settings_user_remove", args=[membership.id])
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Membership.objects.filter(id=membership.id).exists())

    def test_company_admin_cannot_remove_user_when_sso_is_on(self):
        admin = create_user("admin@example.com")
        user = create_user("viewer@example.com")
        organization = Organization.objects.create(
            name="Internal Test Org",
            sso_required=True,
        )
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        target_membership = Membership.objects.create(
            organization=organization,
            user=user,
            role=Membership.Role.VIEWER,
        )
        self.client.force_login(admin)

        response = self.client.post(
            reverse("core:settings_user_remove", args=[target_membership.id])
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Membership.objects.filter(id=target_membership.id).exists())


class DatabaseConnectionSettingsTests(TestCase):
    def test_company_admin_can_add_database_connection(self):
        admin = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        self.client.force_login(admin)

        raw_connection_string = (
            "postgresql+psycopg://readonly:secret-password@db.example.com:5432/sales"
        )
        response = self.client.post(
            reverse("core:settings_database_connection_add"),
            {
                "name": "Sales Warehouse",
                "provider": DatabaseConnection.Provider.POSTGRES,
                "db_host": "db.example.com",
                "db_port": "5432",
                "db_name": "sales",
                "db_username": "readonly",
                "db_password": "secret-password",
                "postgres_sslmode": "",
                "enabled": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        database_connection = DatabaseConnection.objects.get(name="Sales Warehouse")
        self.assertEqual(database_connection.organization, organization)
        self.assertNotEqual(
            database_connection.encrypted_connection_string,
            raw_connection_string,
        )
        self.assertEqual(
            database_connection.get_connection_string(),
            raw_connection_string,
        )
        self.assertIn("***", database_connection.connection_string_preview)
        self.assertNotIn("secret-password", database_connection.connection_string_preview)

    def test_company_admin_can_add_custom_database_connection(self):
        admin = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        self.client.force_login(admin)

        raw_connection_string = "sqlite:///:memory:"
        response = self.client.post(
            reverse("core:settings_database_connection_add"),
            {
                "name": "Custom SQLite",
                "provider": DatabaseConnection.Provider.CUSTOM,
                "connection_string": raw_connection_string,
                "enabled": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        database_connection = DatabaseConnection.objects.get(name="Custom SQLite")
        self.assertEqual(database_connection.provider, DatabaseConnection.Provider.CUSTOM)
        self.assertEqual(
            database_connection.get_connection_string(),
            raw_connection_string,
        )

    def test_company_admin_can_add_bigquery_connection(self):
        admin = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        self.client.force_login(admin)
        service_account = {
            "type": "service_account",
            "client_email": "reports@example.iam.gserviceaccount.com",
            "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
            "token_uri": "https://oauth2.googleapis.com/token",
        }

        response = self.client.post(
            reverse("core:settings_database_connection_add"),
            {
                "name": "BigQuery Warehouse",
                "provider": DatabaseConnection.Provider.BIGQUERY,
                "bigquery_project": "analytics-prod",
                "bigquery_dataset": "sales",
                "bigquery_location": "US",
                "bigquery_service_account_json": json.dumps(service_account),
                "enabled": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        database_connection = DatabaseConnection.objects.get(name="BigQuery Warehouse")
        connection_config = parse_connection_config(database_connection.get_connection_string())
        self.assertEqual(connection_config["provider"], "bigquery")
        self.assertEqual(connection_config["config"]["project_id"], "analytics-prod")
        self.assertIn("bigquery://analytics-prod/sales", database_connection.connection_string_preview)
        self.assertNotIn("PRIVATE KEY", database_connection.connection_string_preview)

    def test_company_admin_can_add_snowflake_token_connection(self):
        admin = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        self.client.force_login(admin)

        response = self.client.post(
            reverse("core:settings_database_connection_add"),
            {
                "name": "Snowflake Warehouse",
                "provider": DatabaseConnection.Provider.SNOWFLAKE,
                "snowflake_account": "acme-prod",
                "snowflake_username": "REPORT_USER",
                "snowflake_auth_type": "programmatic_access_token",
                "snowflake_password": "pat-secret",
                "snowflake_database": "ANALYTICS",
                "snowflake_schema": "SALES",
                "snowflake_warehouse": "REPORTING_WH",
                "snowflake_role": "REPORT_READER",
                "enabled": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        database_connection = DatabaseConnection.objects.get(name="Snowflake Warehouse")
        connection_config = parse_connection_config(database_connection.get_connection_string())
        self.assertEqual(connection_config["provider"], "snowflake")
        self.assertEqual(connection_config["config"]["auth_type"], "programmatic_access_token")
        self.assertIn("snowflake://acme-prod/ANALYTICS.SALES", database_connection.connection_string_preview)
        self.assertNotIn("pat-secret", database_connection.connection_string_preview)

    def test_database_connection_add_page_renders_guided_provider_fields(self):
        admin = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        self.client.force_login(admin)

        response = self.client.get(reverse("core:settings_database_connection_add"))

        self.assertContains(response, 'data-provider-fields="postgres"')
        self.assertContains(response, 'data-provider-fields="snowflake"')
        self.assertContains(response, 'data-provider-fields="custom"')
        self.assertContains(response, 'id="id_db_username"')
        self.assertContains(response, 'id="id_snowflake_username"')
        self.assertContains(response, 'id="id_bigquery_service_account_json"')
        self.assertNotContains(response, "SQLAlchemy connection string")

    def test_database_connection_pages_never_show_raw_connection_string(self):
        admin = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        raw_connection_string = (
            "postgresql+psycopg://readonly:secret-password@db.example.com:5432/sales"
        )
        database_connection = DatabaseConnection(
            organization=organization,
            name="Sales Warehouse",
            provider=DatabaseConnection.Provider.POSTGRES,
        )
        database_connection.set_connection_string(
            raw_connection_string,
            "postgresql+psycopg://readonly:***@db.example.com:5432/sales",
        )
        database_connection.save()
        self.client.force_login(admin)

        list_response = self.client.get(reverse("core:settings_database_connections"))
        detail_response = self.client.get(
            reverse(
                "core:settings_database_connection_detail",
                args=[database_connection.id],
            )
        )

        self.assertContains(list_response, "readonly:***")
        self.assertNotContains(list_response, "secret-password")
        self.assertContains(detail_response, "readonly:***")
        self.assertNotContains(detail_response, "secret-password")

    def test_creator_cannot_manage_database_connections(self):
        user = create_user("creator@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=user,
            role=Membership.Role.CREATOR,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("core:settings_database_connections"))

        self.assertEqual(response.status_code, 403)

    def test_company_admin_can_update_database_connection(self):
        admin = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        database_connection = DatabaseConnection(
            organization=organization,
            name="Sales Warehouse",
            provider=DatabaseConnection.Provider.POSTGRES,
        )
        database_connection.set_connection_string(
            "sqlite:///:memory:",
            "sqlite:///:memory:",
        )
        database_connection.save()
        self.client.force_login(admin)

        response = self.client.post(
            reverse(
                "core:settings_database_connection_detail",
                args=[database_connection.id],
            ),
            {
                "name": "Sales Warehouse Updated",
                "provider": DatabaseConnection.Provider.SQLITE,
                "sqlite_path": "updated.sqlite3",
            },
        )

        database_connection.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(database_connection.name, "Sales Warehouse Updated")
        self.assertEqual(database_connection.provider, DatabaseConnection.Provider.SQLITE)
        self.assertFalse(database_connection.enabled)
        self.assertEqual(database_connection.get_connection_string(), "sqlite:///updated.sqlite3")

    def test_company_admin_can_test_sqlite_database_connection(self):
        admin = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        database_connection = DatabaseConnection(
            organization=organization,
            name="Demo SQLite",
            provider=DatabaseConnection.Provider.SQLITE,
        )
        database_connection.set_connection_string(
            "sqlite:///:memory:",
            "sqlite:///:memory:",
        )
        database_connection.save()
        self.client.force_login(admin)

        response = self.client.post(
            reverse("core:settings_database_connection_test", args=[database_connection.id])
        )

        database_connection.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(database_connection.last_test_succeeded)
        self.assertEqual(database_connection.last_test_error, "")
        self.assertIsNotNone(database_connection.last_tested_at)

    def test_company_admin_can_delete_database_connection(self):
        admin = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        database_connection = DatabaseConnection(
            organization=organization,
            name="Sales Warehouse",
            provider=DatabaseConnection.Provider.POSTGRES,
        )
        database_connection.set_connection_string(
            "sqlite:///:memory:",
            "sqlite:///:memory:",
        )
        database_connection.save()
        self.client.force_login(admin)

        response = self.client.post(
            reverse(
                "core:settings_database_connection_delete",
                args=[database_connection.id],
            )
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            DatabaseConnection.objects.filter(id=database_connection.id).exists()
        )

    def test_database_connection_form_rejects_invalid_sqlalchemy_url(self):
        form = DatabaseConnectionCreateForm(
            data={
                "name": "Bad URL",
                "provider": DatabaseConnection.Provider.CUSTOM,
                "connection_string": "not a real sqlalchemy url",
                "enabled": "on",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("connection_string", form.errors)

    def test_connection_test_error_redaction_removes_password(self):
        raw_connection_string = (
            "postgresql+psycopg://readonly:secret-password@db.example.com:5432/sales"
        )
        error = (
            "Could not connect using "
            "postgresql+psycopg://readonly:secret-password@db.example.com:5432/sales"
        )

        redacted = redact_connection_error(error, raw_connection_string)

        self.assertNotIn("secret-password", redacted)
        self.assertIn("readonly:***", redacted)


class QueryExecutionServiceTests(TestCase):
    def setUp(self):
        self.user = create_user("creator@example.com")
        self.organization = Organization.objects.create(
            name="Internal Test Org",
            max_rows=10,
            max_raw_bytes=10000,
        )
        Membership.objects.create(
            organization=self.organization,
            user=self.user,
            role=Membership.Role.CREATOR,
        )
        self.temp_dir = tempfile.TemporaryDirectory()
        self.sqlite_path = Path(self.temp_dir.name) / "query_service.sqlite3"
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute(
                "create table deals (id integer primary key, name text, amount numeric, booked_at text)"
            )
            conn.executemany(
                "insert into deals (name, amount, booked_at) values (?, ?, ?)",
                [
                    ("Expansion", 12500.50, "2026-01-15"),
                    ("Platform", 87500, "2026-02-20"),
                    ("Analytics", 42000, "2026-03-05"),
                ],
            )
        self.database_connection = DatabaseConnection(
            organization=self.organization,
            name="SQLite Query Test",
            provider=DatabaseConnection.Provider.SQLITE,
        )
        sqlite_url = f"sqlite:///{self.sqlite_path.as_posix()}"
        self.database_connection.set_connection_string(sqlite_url, sqlite_url)
        self.database_connection.save()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_execute_query_returns_json_safe_rows_and_logs_success(self):
        result = execute_query(
            self.database_connection,
            "select id, name, amount, booked_at from deals order by id",
            user=self.user,
        )

        self.assertEqual(result.columns, ["id", "name", "amount", "booked_at"])
        self.assertEqual(result.row_count, 3)
        self.assertEqual(result.rows[0]["name"], "Expansion")
        self.assertGreater(result.raw_bytes, 0)
        log = QueryExecutionLog.objects.get()
        self.assertTrue(log.succeeded)
        self.assertEqual(log.row_count, 3)
        self.assertEqual(log.database_connection, self.database_connection)
        self.assertEqual(log.user, self.user)

    def test_execute_query_blocks_disabled_connection_and_logs_failure(self):
        self.database_connection.enabled = False
        self.database_connection.save(update_fields=["enabled"])

        with self.assertRaises(QueryPolicyError):
            execute_query(self.database_connection, "select * from deals", user=self.user)

        log = QueryExecutionLog.objects.get()
        self.assertFalse(log.succeeded)
        self.assertIn("disabled", log.error_message)

    def test_execute_query_blocks_write_sql(self):
        with self.assertRaises(QueryPolicyError):
            execute_query(
                self.database_connection,
                "delete from deals where id = 1",
                user=self.user,
            )

        self.assertFalse(QueryExecutionLog.objects.get().succeeded)

    def test_execute_query_blocks_write_keyword_in_cte(self):
        with self.assertRaises(QueryPolicyError):
            execute_query(
                self.database_connection,
                "with deleted as (delete from deals returning id) select * from deleted",
                user=self.user,
            )

        self.assertIn(
            "Write and schema-changing",
            QueryExecutionLog.objects.get().error_message,
        )

    def test_execute_query_enforces_max_rows(self):
        self.organization.max_rows = 2
        self.organization.save(update_fields=["max_rows"])

        with self.assertRaises(QueryPolicyError):
            execute_query(
                self.database_connection,
                "select id, name from deals order by id",
                user=self.user,
            )

        log = QueryExecutionLog.objects.get()
        self.assertFalse(log.succeeded)
        self.assertIn("more than the allowed 2 rows", log.error_message)

    def test_execute_query_enforces_raw_byte_limit(self):
        self.organization.max_raw_bytes = 20
        self.organization.save(update_fields=["max_raw_bytes"])

        with self.assertRaises(QueryPolicyError):
            execute_query(
                self.database_connection,
                "select id, name from deals order by id",
                user=self.user,
            )

        log = QueryExecutionLog.objects.get()
        self.assertFalse(log.succeeded)
        self.assertIn("above the allowed 20 bytes", log.error_message)

    def test_execute_query_allows_leading_sql_comment(self):
        result = execute_query(
            self.database_connection,
            "-- report query\nselect count(*) as deal_count from deals",
            user=self.user,
        )

        self.assertEqual(result.rows, [{"deal_count": 3}])

    def test_async_execute_query_returns_rows(self):
        result = async_to_sync(async_execute_query)(
            self.database_connection,
            "select count(*) as deal_count from deals",
            user=self.user,
        )

        self.assertEqual(result.rows, [{"deal_count": 3}])

    def test_sqlite_connection_string_is_converted_to_async_driver(self):
        self.assertEqual(
            async_sqlalchemy_connection_string("sqlite:///demo.sqlite3"),
            "sqlite+aiosqlite:///demo.sqlite3",
        )

    def test_postgres_connection_string_is_converted_to_async_driver(self):
        converted = async_sqlalchemy_connection_string(
            "postgresql+psycopg://readonly:secret@db.example.com:5432/sales?sslmode=require"
        )

        self.assertTrue(converted.startswith("postgresql+asyncpg://readonly:secret@db.example.com:5432/sales"))
        self.assertIn("ssl=require", converted)

    def test_custom_sync_driver_is_rejected_for_async_execution(self):
        with self.assertRaises(QueryExecutionError):
            async_sqlalchemy_connection_string("mysql+pymysql://user:pass@db/sales")

    def test_bigquery_execution_dispatches_to_async_adapter(self):
        database_connection = DatabaseConnection(
            organization=self.organization,
            name="BigQuery Query Test",
            provider=DatabaseConnection.Provider.BIGQUERY,
        )
        database_connection.set_connection_string(
            build_bigquery_connection_string(
                "analytics-prod",
                "sales",
                json.dumps(
                    {
                        "client_email": "reports@example.iam.gserviceaccount.com",
                        "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                ),
                "US",
            ),
            "bigquery://analytics-prod/sales",
        )
        database_connection.save()
        expected_result = QueryExecutionResult(
            columns=["answer"],
            rows=[{"answer": "1"}],
            row_count=1,
            raw_bytes=40,
            duration_ms=12,
        )

        with patch(
            "core.warehouse_adapters.async_execute_bigquery_query",
            new_callable=AsyncMock,
        ) as execute_bigquery:
            execute_bigquery.return_value = expected_result
            result = async_to_sync(async_execute_query)(
                database_connection,
                "select 1 as answer",
                user=self.user,
            )

        self.assertEqual(result, expected_result)
        execute_bigquery.assert_awaited_once()
        self.assertTrue(QueryExecutionLog.objects.latest("id").succeeded)

    def test_snowflake_execution_dispatches_to_async_adapter(self):
        database_connection = DatabaseConnection(
            organization=self.organization,
            name="Snowflake Query Test",
            provider=DatabaseConnection.Provider.SNOWFLAKE,
        )
        database_connection.set_connection_string(
            build_snowflake_connection_string(
                "acme-prod",
                "REPORT_USER",
                "pat-secret",
                "ANALYTICS",
                "SALES",
                "REPORTING_WH",
                "REPORT_READER",
            ),
            "snowflake://acme-prod/ANALYTICS.SALES",
        )
        database_connection.save()
        expected_result = QueryExecutionResult(
            columns=["ANSWER"],
            rows=[{"ANSWER": "1"}],
            row_count=1,
            raw_bytes=40,
            duration_ms=12,
        )

        with patch(
            "core.warehouse_adapters.async_execute_snowflake_query",
            new_callable=AsyncMock,
        ) as execute_snowflake:
            execute_snowflake.return_value = expected_result
            result = async_to_sync(async_execute_query)(
                database_connection,
                "select 1 as answer",
                user=self.user,
            )

        self.assertEqual(result, expected_result)
        execute_snowflake.assert_awaited_once()
        self.assertTrue(QueryExecutionLog.objects.latest("id").succeeded)


class ReportBuilderTests(TestCase):
    def setUp(self):
        self.creator = create_user("creator@example.com")
        self.admin = create_user("admin@example.com")
        self.organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=self.organization,
            user=self.creator,
            role=Membership.Role.CREATOR,
        )
        Membership.objects.create(
            organization=self.organization,
            user=self.admin,
            role=Membership.Role.ADMIN,
        )
        self.temp_dir = tempfile.TemporaryDirectory()
        self.sqlite_path = Path(self.temp_dir.name) / "reports.sqlite3"
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute("create table pipeline (stage text, amount numeric)")
            conn.executemany(
                "insert into pipeline (stage, amount) values (?, ?)",
                [("Qualified", 1000), ("Proposal", 2500)],
            )
        self.database_connection = DatabaseConnection(
            organization=self.organization,
            name="Demo SQLite",
            provider=DatabaseConnection.Provider.SQLITE,
        )
        sqlite_url = f"sqlite:///{self.sqlite_path.as_posix()}"
        self.database_connection.set_connection_string(sqlite_url, sqlite_url)
        self.database_connection.save()
        self.provider_key = AIProviderKey(
            organization=self.organization,
            name="OpenAI Test",
            provider=AIProviderKey.Provider.OPENAI,
            model_name="gpt-5.4-mini",
        )
        self.provider_key.set_api_key("sk-test-secret-value")
        self.provider_key.save()

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_report(self, owner=None):
        return Report.objects.create(
            organization=self.organization,
            owner=owner or self.creator,
            database_connection=self.database_connection,
            ai_provider_key=self.provider_key,
            title="Pipeline Report",
            primary_sql="select stage, amount from pipeline order by amount",
            html="<div id='report'>Pipeline</div>",
        )

    def test_builder_home_lists_owned_drafts(self):
        report = self.create_report()
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:builder_home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Builder")
        self.assertContains(response, report.title)
        self.assertContains(response, reverse("core:report_builder", args=[report.id]))
        self.assertContains(response, reverse("core:report_draft_delete", args=[report.id]))

    def test_builder_home_new_draft_popup_offers_ai_and_import_paths(self):
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:builder_home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="new-draft-dialog"')
        self.assertContains(response, "Build with AI")
        self.assertContains(response, "Upload existing HTML + SQL")
        self.assertContains(response, reverse("core:builder_new"))
        self.assertContains(response, reverse("core:builder_import"))
        self.assertContains(response, 'id="id_primary_sql"')
        self.assertContains(response, 'id="id_html"')

    def test_builder_home_does_not_show_published_reports(self):
        draft = self.create_report()
        published = self.create_report()
        published.title = "Published Pipeline"
        published.status = Report.Status.PUBLISHED
        published.save()
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:builder_home"))

        self.assertContains(response, draft.title)
        self.assertNotContains(response, published.title)

    def test_builder_new_creates_fresh_draft(self):
        existing = self.create_report()
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:builder_new"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Report.objects.filter(owner=self.creator).count(), 2)
        new_report = Report.objects.exclude(id=existing.id).get()
        self.assertEqual(
            response.headers["Location"],
            reverse("core:report_builder", args=[new_report.id]),
        )
        self.assertEqual(new_report.title, "Draft report")

    def test_builder_import_creates_draft_and_hot_starts_ai_adaptation(self):
        self.client.force_login(self.creator)
        uploaded_sql = "select stage, amount from pipeline"
        uploaded_html = "<h1>Old Pipeline</h1><script>console.log('old')</script>"

        with patch(
            "core.views.async_generate_report_chat_response",
            new_callable=AsyncMock,
        ) as chat_response:
            chat_response.return_value = (
                "I adapted the imported report.",
                {
                    "title": "Imported Pipeline",
                    "database_connection_id": self.database_connection.id,
                    "primary_sql": uploaded_sql,
                    "html": "<h1>Imported Pipeline</h1>",
                },
            )
            response = self.client.post(
                reverse("core:builder_import"),
                {
                    "primary_sql": uploaded_sql,
                    "html": uploaded_html,
                    "instructions": "Keep the headline.",
                },
            )

        report = Report.objects.get(title="Imported Pipeline")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("core:report_builder", args=[report.id]))
        self.assertEqual(report.owner, self.creator)
        self.assertEqual(report.primary_sql, uploaded_sql)
        self.assertIn("Imported Pipeline", report.html)
        user_message = report.chat_messages.get(role=ReportChatMessage.Role.USER)
        self.assertIn("Existing SQL:", user_message.content)
        self.assertIn(uploaded_sql, user_message.content)
        self.assertIn(uploaded_html, user_message.content)
        self.assertIn("Keep the headline.", user_message.content)
        self.assertTrue(
            report.chat_messages.filter(
                role=ReportChatMessage.Role.ASSISTANT,
                content="I adapted the imported report.",
            ).exists()
        )

    def test_builder_import_requires_sql_and_html(self):
        self.client.force_login(self.creator)

        response = self.client.post(
            reverse("core:builder_import"),
            {
                "primary_sql": "",
                "html": "<h1>Missing SQL</h1>",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("core:builder_home"))
        self.assertFalse(Report.objects.filter(title="Imported report draft").exists())

    def test_reports_list_only_shows_published_reports(self):
        draft = self.create_report()
        published = self.create_report()
        published.title = "Published Pipeline"
        published.status = Report.Status.PUBLISHED
        published.save()
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:reports_placeholder"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, published.title)
        self.assertContains(response, reverse("core:report_preview", args=[published.id]))
        self.assertNotContains(response, draft.title)
        self.assertNotContains(response, reverse("core:report_builder", args=[draft.id]))

    def test_reports_list_uses_share_popup_instead_of_share_page_link(self):
        published = self.create_report()
        published.status = Report.Status.PUBLISHED
        published.save()
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:reports_placeholder"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="share-dialog"')
        self.assertContains(response, "data-share-open")
        self.assertContains(response, reverse("core:report_share_options", args=[published.id]))
        self.assertContains(response, "Search by email")
        self.assertContains(response, "selectedUsers")
        self.assertContains(response, 'id="share-dropdown"')
        self.assertContains(response, "share-option")

    def test_reports_list_shows_org_wide_published_reports_to_org_members(self):
        viewer = create_user("viewer@example.com")
        Membership.objects.create(
            organization=self.organization,
            user=viewer,
            role=Membership.Role.VIEWER,
        )
        published = self.create_report()
        published.status = Report.Status.PUBLISHED
        published.sharing_scope = Report.SharingScope.ORGANIZATION
        published.save()
        self.client.force_login(viewer)

        response = self.client.get(reverse("core:reports_placeholder"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, published.title)
        self.assertContains(response, reverse("core:report_preview", args=[published.id]))
        self.assertNotContains(response, reverse("core:report_builder", args=[published.id]))

    def test_reports_list_shows_explicitly_shared_report_to_user(self):
        viewer = create_user("viewer@example.com")
        other_viewer = create_user("other-viewer@example.com")
        Membership.objects.create(
            organization=self.organization,
            user=viewer,
            role=Membership.Role.VIEWER,
        )
        Membership.objects.create(
            organization=self.organization,
            user=other_viewer,
            role=Membership.Role.VIEWER,
        )
        published = self.create_report()
        published.status = Report.Status.PUBLISHED
        published.save()
        published.shared_with.add(viewer)

        self.client.force_login(viewer)
        response = self.client.get(reverse("core:reports_placeholder"))

        self.assertContains(response, published.title)

        self.client.force_login(other_viewer)
        response = self.client.get(reverse("core:reports_placeholder"))

        self.assertNotContains(response, published.title)

    def test_shared_viewer_can_preview_but_not_edit_published_report(self):
        viewer = create_user("viewer@example.com")
        Membership.objects.create(
            organization=self.organization,
            user=viewer,
            role=Membership.Role.VIEWER,
        )
        published = self.create_report()
        published.status = Report.Status.PUBLISHED
        published.save()
        published.shared_with.add(viewer)
        self.client.force_login(viewer)

        preview_response = self.client.get(reverse("core:report_preview", args=[published.id]))
        builder_response = self.client.get(reverse("core:report_builder", args=[published.id]))

        self.assertEqual(preview_response.status_code, 200)
        self.assertEqual(builder_response.status_code, 403)

    def test_builder_shows_publish_button_for_draft(self):
        report = self.create_report()
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:report_builder", args=[report.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("core:report_publish", args=[report.id]))
        self.assertContains(response, "Publish")

    def test_creator_can_publish_owned_draft(self):
        report = self.create_report()
        self.client.force_login(self.creator)

        response = self.client.post(reverse("core:report_publish", args=[report.id]))

        report.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("core:reports_placeholder"))
        self.assertEqual(report.status, Report.Status.PUBLISHED)

        reports_response = self.client.get(reverse("core:reports_placeholder"))
        self.assertContains(reports_response, report.title)
        self.assertContains(reports_response, reverse("core:report_preview", args=[report.id]))

    def test_owner_can_open_published_report_in_builder_for_edits(self):
        report = self.create_report()
        report.status = Report.Status.PUBLISHED
        report.save()
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:report_builder", args=[report.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, report.title)

    def test_creator_cannot_publish_someone_elses_draft(self):
        other_user = create_user("other@example.com")
        Membership.objects.create(
            organization=self.organization,
            user=other_user,
            role=Membership.Role.CREATOR,
        )
        report = self.create_report(owner=other_user)
        self.client.force_login(self.creator)

        response = self.client.post(reverse("core:report_publish", args=[report.id]))

        report.refresh_from_db()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(report.status, Report.Status.DRAFT)

    def test_owner_can_update_published_report_sharing(self):
        viewer = create_user("viewer@example.com")
        Membership.objects.create(
            organization=self.organization,
            user=viewer,
            role=Membership.Role.VIEWER,
        )
        report = self.create_report()
        report.status = Report.Status.PUBLISHED
        report.save()
        self.client.force_login(self.creator)

        response = self.client.post(
            reverse("core:report_share", args=[report.id]),
            {
                "sharing_scope": Report.SharingScope.PRIVATE,
                "shared_with": [viewer.id],
            },
        )

        report.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("core:reports_placeholder"))
        self.assertEqual(report.sharing_scope, Report.SharingScope.PRIVATE)
        self.assertIn(viewer, report.shared_with.all())

    def test_owner_can_search_org_users_for_report_sharing(self):
        viewer = create_user("avery.viewer@example.com")
        other_viewer = create_user("other@example.com")
        outside_user = create_user("avery.outside@example.com")
        outside_org = Organization.objects.create(name="Outside Org")
        Membership.objects.create(
            organization=self.organization,
            user=viewer,
            role=Membership.Role.VIEWER,
        )
        Membership.objects.create(
            organization=self.organization,
            user=other_viewer,
            role=Membership.Role.VIEWER,
        )
        Membership.objects.create(
            organization=outside_org,
            user=outside_user,
            role=Membership.Role.VIEWER,
        )
        report = self.create_report()
        report.status = Report.Status.PUBLISHED
        report.save()
        report.shared_with.add(other_viewer)
        self.client.force_login(self.creator)

        response = self.client.get(
            reverse("core:report_share_options", args=[report.id]),
            {"q": "avery"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        result_emails = {user["email"] for user in payload["results"]}
        shared_emails = {user["email"] for user in payload["shared_users"]}
        self.assertIn("avery.viewer@example.com", result_emails)
        self.assertNotIn("avery.outside@example.com", result_emails)
        self.assertIn("other@example.com", shared_emails)

    def test_share_options_without_query_returns_first_org_users(self):
        selected = create_user("selected@example.com")
        unselected = create_user("unselected@example.com")
        outside_user = create_user("aaa-outside@example.com")
        outside_org = Organization.objects.create(name="Outside Org")
        Membership.objects.create(
            organization=self.organization,
            user=selected,
            role=Membership.Role.VIEWER,
        )
        Membership.objects.create(
            organization=self.organization,
            user=unselected,
            role=Membership.Role.VIEWER,
        )
        Membership.objects.create(
            organization=outside_org,
            user=outside_user,
            role=Membership.Role.VIEWER,
        )
        report = self.create_report()
        report.status = Report.Status.PUBLISHED
        report.save()
        report.shared_with.add(selected)
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:report_share_options", args=[report.id]))

        self.assertEqual(response.status_code, 200)
        result_emails = {user["email"] for user in response.json()["results"]}
        self.assertIn("selected@example.com", result_emails)
        self.assertIn("unselected@example.com", result_emails)
        self.assertNotIn("aaa-outside@example.com", result_emails)

    def test_share_options_limits_default_dropdown(self):
        for index in range(25):
            user = create_user(f"user{index:02d}@example.com")
            Membership.objects.create(
                organization=self.organization,
                user=user,
                role=Membership.Role.VIEWER,
            )
        report = self.create_report()
        report.status = Report.Status.PUBLISHED
        report.save()
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:report_share_options", args=[report.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["results"]), 20)

    def test_non_owner_cannot_share_published_report(self):
        viewer = create_user("viewer@example.com")
        Membership.objects.create(
            organization=self.organization,
            user=viewer,
            role=Membership.Role.VIEWER,
        )
        report = self.create_report()
        report.status = Report.Status.PUBLISHED
        report.sharing_scope = Report.SharingScope.ORGANIZATION
        report.save()
        self.client.force_login(viewer)

        response = self.client.get(reverse("core:report_share", args=[report.id]))

        self.assertEqual(response.status_code, 403)

    def test_owner_can_delete_published_report(self):
        report = self.create_report()
        report.status = Report.Status.PUBLISHED
        report.save()
        self.client.force_login(self.creator)

        response = self.client.post(reverse("core:report_delete", args=[report.id]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("core:reports_placeholder"))
        self.assertFalse(Report.objects.filter(id=report.id).exists())

    def test_non_owner_cannot_delete_shared_published_report(self):
        viewer = create_user("viewer@example.com")
        Membership.objects.create(
            organization=self.organization,
            user=viewer,
            role=Membership.Role.VIEWER,
        )
        report = self.create_report()
        report.status = Report.Status.PUBLISHED
        report.save()
        report.shared_with.add(viewer)
        self.client.force_login(viewer)

        response = self.client.post(reverse("core:report_delete", args=[report.id]))

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Report.objects.filter(id=report.id).exists())

    def test_creator_can_delete_owned_draft(self):
        report = self.create_report()
        self.client.force_login(self.creator)

        response = self.client.post(reverse("core:report_draft_delete", args=[report.id]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("core:builder_home"))
        self.assertFalse(Report.objects.filter(id=report.id).exists())

    def test_creator_cannot_delete_published_report_from_draft_delete(self):
        report = self.create_report()
        report.status = Report.Status.PUBLISHED
        report.save()
        self.client.force_login(self.creator)

        response = self.client.post(reverse("core:report_draft_delete", args=[report.id]))

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Report.objects.filter(id=report.id).exists())

    def test_report_dataset_endpoint_runs_primary_sql(self):
        report = self.create_report()
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:report_primary_dataset", args=[report.id]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["row_count"], 2)
        self.assertEqual(payload["rows"][0]["stage"], "Qualified")

    def test_report_dataset_endpoint_caches_primary_sql_result(self):
        report = self.create_report()
        self.client.force_login(self.creator)

        first_response = self.client.get(reverse("core:report_primary_dataset", args=[report.id]))
        second_response = self.client.get(reverse("core:report_primary_dataset", args=[report.id]))

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertFalse(first_response.json()["cache_hit"])
        self.assertTrue(second_response.json()["cache_hit"])
        self.assertEqual(ReportDatasetCache.objects.filter(report=report).count(), 1)
        cache = ReportDatasetCache.objects.get(report=report)
        self.assertGreater(cache.compressed_bytes, 0)
        self.assertGreater(cache.raw_bytes, 0)
        self.assertEqual(cache.row_count, 2)
        self.assertTrue(
            ReportDatasetCacheLock.objects.filter(cache_key=cache.cache_key).exists()
        )
        cache_statuses = list(
            QueryExecutionLog.objects.order_by("created_at").values_list(
                "cache_status",
                flat=True,
            )
        )
        self.assertEqual(
            cache_statuses,
            [
                QueryExecutionLog.CacheStatus.MISS,
                QueryExecutionLog.CacheStatus.HIT,
            ],
        )

    def test_report_dataset_cache_key_changes_when_sql_changes(self):
        report = self.create_report()
        self.client.force_login(self.creator)

        self.client.get(reverse("core:report_primary_dataset", args=[report.id]))
        report.primary_sql = "select stage from pipeline order by stage"
        report.save(update_fields=["primary_sql", "updated_at"])
        response = self.client.get(reverse("core:report_primary_dataset", args=[report.id]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["cache_hit"])
        self.assertEqual(ReportDatasetCache.objects.filter(report=report).count(), 2)

    def test_report_dataset_expired_cache_reruns_query(self):
        report = self.create_report()
        self.client.force_login(self.creator)
        self.client.get(reverse("core:report_primary_dataset", args=[report.id]))
        cache = ReportDatasetCache.objects.get(report=report)
        cache.expires_at = timezone.now() - timezone.timedelta(seconds=1)
        cache.save(update_fields=["expires_at"])

        response = self.client.get(reverse("core:report_primary_dataset", args=[report.id]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["cache_hit"])
        self.assertEqual(
            QueryExecutionLog.objects.filter(
                cache_status=QueryExecutionLog.CacheStatus.MISS,
            ).count(),
            2,
        )

    def test_cleanup_report_cache_command_deletes_expired_caches(self):
        report = self.create_report()
        self.client.force_login(self.creator)
        self.client.get(reverse("core:report_primary_dataset", args=[report.id]))
        cache = ReportDatasetCache.objects.get(report=report)
        cache.expires_at = timezone.now() - timezone.timedelta(seconds=1)
        cache.save(update_fields=["expires_at"])

        call_command("cleanup_report_cache", verbosity=0)

        self.assertFalse(ReportDatasetCache.objects.filter(id=cache.id).exists())
        self.assertFalse(ReportDatasetCacheLock.objects.filter(cache_key=cache.cache_key).exists())

    def test_every_tenth_cache_hit_schedules_expired_cache_cleanup(self):
        report = self.create_report()
        self.client.force_login(self.creator)
        self.client.get(reverse("core:report_primary_dataset", args=[report.id]))
        cache = ReportDatasetCache.objects.get(report=report)
        report_cache._cache_hit_counter = 0

        with patch("core.report_cache.schedule_expired_cache_cleanup") as schedule_cleanup:
            for _index in range(9):
                report_cache.log_cache_hit(cache, user=self.creator)
            self.assertEqual(schedule_cleanup.call_count, 0)

            report_cache.log_cache_hit(cache, user=self.creator)

        self.assertEqual(schedule_cleanup.call_count, 1)
        report_cache._cache_hit_counter = 0

    def test_async_expired_cache_cleanup_deletes_expired_caches(self):
        report = self.create_report()
        self.client.force_login(self.creator)
        self.client.get(reverse("core:report_primary_dataset", args=[report.id]))
        cache = ReportDatasetCache.objects.get(report=report)
        cache.expires_at = timezone.now() - timezone.timedelta(seconds=1)
        cache.save(update_fields=["expires_at"])

        report_cache.run_expired_cache_cleanup()

        self.assertFalse(ReportDatasetCache.objects.filter(id=cache.id).exists())
        self.assertFalse(ReportDatasetCacheLock.objects.filter(cache_key=cache.cache_key).exists())

    def test_report_dataset_cache_error_still_logs_query_attempt(self):
        report = self.create_report()
        self.organization.max_compressed_bytes = 1
        self.organization.save(update_fields=["max_compressed_bytes"])
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:report_primary_dataset", args=[report.id]))

        self.assertEqual(response.status_code, 400)
        self.assertFalse(ReportDatasetCache.objects.filter(report=report).exists())
        log = QueryExecutionLog.objects.get()
        self.assertEqual(log.cache_status, QueryExecutionLog.CacheStatus.MISS)
        self.assertTrue(log.succeeded)
        self.assertEqual(log.row_count, 2)

    def test_report_dataset_compressed_cache_limit_fails_safely(self):
        report = self.create_report()
        self.organization.max_compressed_bytes = 1
        self.organization.save(update_fields=["max_compressed_bytes"])
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:report_primary_dataset", args=[report.id]))

        self.assertEqual(response.status_code, 400)
        self.assertIn("compressed bytes", response.json()["error"])
        self.assertFalse(ReportDatasetCache.objects.filter(report=report).exists())

    def test_report_preview_injects_runtime_sdk(self):
        report = self.create_report()
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:report_preview", args=[report.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "window.sr")
        self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertContains(response, reverse("core:report_primary_dataset", args=[report.id]))
        self.assertContains(response, "return payload.rows || []")
        self.assertContains(response, "datasetMeta")
        self.assertContains(response, "Pipeline")

    def test_report_ip_allowlist_blocks_report_access(self):
        report = self.create_report()
        self.organization.report_ip_allowlist_enabled = True
        self.organization.report_ip_allowlist = "203.0.113.10"
        self.organization.save(
            update_fields=["report_ip_allowlist_enabled", "report_ip_allowlist"]
        )
        self.client.force_login(self.creator)

        response = self.client.get(
            reverse("core:report_preview", args=[report.id]),
            REMOTE_ADDR="198.51.100.20",
        )

        self.assertEqual(response.status_code, 403)

    def test_report_ip_allowlist_allows_cidr_match(self):
        report = self.create_report()
        self.organization.report_ip_allowlist_enabled = True
        self.organization.report_ip_allowlist = "198.51.100.0/24"
        self.organization.save(
            update_fields=["report_ip_allowlist_enabled", "report_ip_allowlist"]
        )
        self.client.force_login(self.creator)

        response = self.client.get(
            reverse("core:report_preview", args=[report.id]),
            REMOTE_ADDR="198.51.100.20",
        )

        self.assertEqual(response.status_code, 200)

    def test_report_preview_enforces_url_whitelist_with_csp_and_runtime_policy(self):
        report = self.create_report()
        self.organization.report_url_whitelist_enabled = True
        self.organization.report_url_whitelist = "cdn.jsdelivr.net\ncdnjs.cloudflare.com"
        self.organization.save(
            update_fields=["report_url_whitelist_enabled", "report_url_whitelist"]
        )
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:report_preview", args=[report.id]))

        self.assertEqual(response.status_code, 200)
        self.assertIn("Content-Security-Policy", response.headers)
        self.assertIn("https://cdn.jsdelivr.net", response.headers["Content-Security-Policy"])
        self.assertContains(response, '"whitelistEnabled": true')
        self.assertContains(response, "Blocked by report URL policy")

    def test_report_preview_neutralizes_blacklisted_external_urls(self):
        report = self.create_report()
        report.html = '<script src="https://evil.example/app.js"></script><img src="https://cdn.example/logo.png">'
        report.save(update_fields=["html"])
        self.organization.report_url_blacklist_enabled = True
        self.organization.report_url_blacklist = "evil.example"
        self.organization.save(
            update_fields=["report_url_blacklist_enabled", "report_url_blacklist"]
        )
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:report_preview", args=[report.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-blocked-src="https://evil.example/app.js"')
        self.assertNotIn(
            '<script src="https://evil.example/app.js"',
            response.content.decode(),
        )
        self.assertContains(response, 'src="https://cdn.example/logo.png"')

    def test_report_chat_updates_report_with_ai_draft(self):
        report = self.create_report()
        self.client.force_login(self.creator)

        with patch(
            "core.views.async_generate_report_chat_response",
            new_callable=AsyncMock,
        ) as chat_response:
            chat_response.return_value = (
                "Updated the report.",
                {
                    "title": "AI Pipeline",
                    "database_connection_id": self.database_connection.id,
                    "primary_sql": "select stage, sum(amount) as amount from pipeline group by stage",
                    "html": "<h1>AI Pipeline</h1>",
                },
            )
            response = self.client.post(
                reverse("core:report_chat_send", args=[report.id]),
                {"message": "Make this a pipeline summary."},
                HTTP_HX_REQUEST="true",
            )

        report.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(report.title, "AI Pipeline")
        self.assertEqual(report.database_connection, self.database_connection)
        self.assertIn("group by stage", report.primary_sql)
        self.assertIn("AI Pipeline", report.html)
        assistant_message = report.chat_messages.get(
            role=ReportChatMessage.Role.ASSISTANT,
            content="Updated the report.",
        )
        self.assertIn("group by stage", assistant_message.artifact["primary_sql"])

    def test_preview_runtime_reports_browser_and_dataset_errors(self):
        report = self.create_report()
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:report_preview", args=[report.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "window.addEventListener(\"error\"")
        self.assertContains(response, "window.addEventListener(\"unhandledrejection\"")
        self.assertContains(response, reverse("core:report_preview_error", args=[report.id]))
        self.assertContains(response, "safe_reports.preview_error")
        self.assertContains(response, "await reportError(error, \"sr.dataset(\" + name + \")\")")

    def test_preview_error_posts_to_chat_and_triggers_repair(self):
        report = self.create_report()
        self.client.force_login(self.creator)

        with patch(
            "core.views.async_generate_report_chat_response",
            new_callable=AsyncMock,
        ) as chat_response:
            chat_response.return_value = (
                "I fixed the preview error.",
                {
                    "title": "Fixed Pipeline",
                    "database_connection_id": self.database_connection.id,
                    "primary_sql": "select stage from pipeline",
                    "html": "<script>const data = await sr.dataset('primary');</script>",
                },
            )
            response = self.client.post(
                reverse("core:report_preview_error", args=[report.id]),
                data=(
                    '{"context":"window.error",'
                    '"message":"Cannot read properties of undefined",'
                    '"stack":"TypeError: Cannot read properties of undefined"}'
                ),
                content_type="application/json",
            )

        report.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["repaired"])
        self.assertEqual(report.title, "Fixed Pipeline")
        self.assertTrue(
            report.chat_messages.filter(
                role=ReportChatMessage.Role.ASSISTANT,
                content__contains="Preview error 1/4",
                artifact__preview_error=True,
            ).exists()
        )
        self.assertTrue(
            report.chat_messages.filter(
                role=ReportChatMessage.Role.ASSISTANT,
                content="I fixed the preview error.",
            ).exists()
        )
        self.assertIn("Cannot read properties", chat_response.call_args.args[1])

    def test_preview_error_gives_up_after_four_browser_errors(self):
        report = self.create_report()
        for index in range(3):
            ReportChatMessage.objects.create(
                report=report,
                role=ReportChatMessage.Role.ASSISTANT,
                content=f"Preview error {index + 1}/4: old error",
                artifact={"preview_error": True},
            )
        self.client.force_login(self.creator)

        with patch(
            "core.views.async_generate_report_chat_response",
            new_callable=AsyncMock,
        ) as chat_response:
            response = self.client.post(
                reverse("core:report_preview_error", args=[report.id]),
                data='{"message":"Still broken","context":"window.error"}',
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["repaired"])
        self.assertTrue(payload["gave_up"])
        self.assertEqual(chat_response.call_count, 0)
        self.assertTrue(
            report.chat_messages.filter(
                content__contains="failed four times in a row",
                artifact__preview_error_give_up=True,
            ).exists()
        )

    def test_report_builder_sends_chat_on_enter_and_keeps_shift_enter_for_newlines(self):
        report = self.create_report()
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:report_builder", args=[report.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'event.key !== "Enter" || event.shiftKey')
        self.assertContains(response, "form.requestSubmit()")

    def test_report_builder_shows_thinking_indicator_for_streaming_reply(self):
        report = self.create_report()
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:report_builder", args=[report.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "thinking-indicator")
        self.assertContains(response, "Assistant is thinking")
        self.assertContains(response, "removeThinking(assistant)")

    def test_report_builder_chat_history_scrolls_above_composer(self):
        report = self.create_report()
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:report_builder", args=[report.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "report-chat-messages")
        self.assertContains(response, "overflow-y: auto")
        self.assertContains(response, "report-chat-composer")
        self.assertContains(response, "min-height: 0")

    def test_report_builder_renders_ai_model_selector(self):
        report = self.create_report()
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:report_builder", args=[report.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("core:report_model_update", args=[report.id]))
        self.assertContains(response, 'id="id_ai_provider_key"')
        self.assertContains(response, 'id="id_ai_model_name"')
        self.assertContains(response, "provider-model-choices")
        self.assertContains(response, "Use this model")
        self.assertContains(response, 'class="model-picker"')
        self.assertContains(response, "selected-model-summary")

    def test_report_builder_model_selector_uses_admin_default_for_stale_report_model(self):
        report = self.create_report()
        report.ai_model_name = "old-model"
        report.save(update_fields=["ai_model_name"])
        self.provider_key.model_name = "gpt-5.5"
        self.provider_key.save(update_fields=["model_name"])
        AIProviderModel.objects.create(
            provider_key=self.provider_key,
            provider_model_id="gpt-5.4-mini",
            display_name="GPT-5.4 mini",
            allowed=True,
            available=True,
        )
        AIProviderModel.objects.create(
            provider_key=self.provider_key,
            provider_model_id="gpt-5.5",
            display_name="GPT-5.5",
            allowed=True,
            available=True,
        )
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:report_builder", args=[report.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<option value="gpt-5.5" selected>GPT-5.5</option>', html=True)
        self.assertContains(response, '"default_model": "gpt-5.5"')

    def test_creator_can_update_report_ai_model(self):
        report = self.create_report()
        AIProviderModel.objects.create(
            provider_key=self.provider_key,
            provider_model_id="gpt-5.4-mini",
            display_name="GPT-5.4 mini",
            allowed=True,
            available=True,
        )
        AIProviderModel.objects.create(
            provider_key=self.provider_key,
            provider_model_id="gpt-5.5",
            display_name="GPT-5.5",
            allowed=True,
            available=True,
        )
        self.client.force_login(self.creator)

        response = self.client.post(
            reverse("core:report_model_update", args=[report.id]),
            {
                "ai_provider_key": self.provider_key.id,
                "ai_model_name": "gpt-5.5",
            },
        )

        report.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(report.ai_provider_key, self.provider_key)
        self.assertEqual(report.ai_model_name, "gpt-5.5")

    def test_creator_cannot_update_report_to_disallowed_model(self):
        report = self.create_report()
        original_model_name = report.ai_model_name
        AIProviderModel.objects.create(
            provider_key=self.provider_key,
            provider_model_id="gpt-5.4-mini",
            display_name="GPT-5.4 mini",
            allowed=True,
            available=True,
        )
        AIProviderModel.objects.create(
            provider_key=self.provider_key,
            provider_model_id="gpt-5.5",
            display_name="GPT-5.5",
            allowed=False,
            available=True,
        )
        self.client.force_login(self.creator)

        response = self.client.post(
            reverse("core:report_model_update", args=[report.id]),
            {
                "ai_provider_key": self.provider_key.id,
                "ai_model_name": "gpt-5.5",
            },
        )

        report.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(report.ai_model_name, original_model_name)

    def test_report_chat_history_renders_raw_html_artifact(self):
        report = self.create_report()
        ReportChatMessage.objects.create(
            report=report,
            role=ReportChatMessage.Role.ASSISTANT,
            content="Updated the report.",
            artifact={
                "primary_sql": "select stage from pipeline",
                "html": "<h1>Pipeline</h1>",
            },
        )
        self.client.force_login(self.creator)

        response = self.client.get(reverse("core:report_builder", args=[report.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Report changes")
        self.assertContains(response, "SQL")
        self.assertContains(response, "HTML")
        self.assertContains(response, "&lt;h1&gt;Pipeline&lt;/h1&gt;")

    def test_report_chat_can_reply_without_updating_report(self):
        report = self.create_report()
        original_sql = report.primary_sql
        self.client.force_login(self.creator)

        with patch(
            "core.views.async_generate_report_chat_response",
            new_callable=AsyncMock,
        ) as chat_response:
            chat_response.return_value = ("Yes, I can help with that.", {})
            response = self.client.post(
                reverse("core:report_chat_send", args=[report.id]),
                {"message": "Can you explain what this report does?"},
                HTTP_HX_REQUEST="true",
            )

        report.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(report.primary_sql, original_sql)
        self.assertTrue(
            report.chat_messages.filter(
                role=ReportChatMessage.Role.ASSISTANT,
                content="Yes, I can help with that.",
                artifact={},
            ).exists()
        )

    def test_report_chat_does_not_set_output_token_cap(self):
        report = self.create_report()
        self.client.force_login(self.creator)

        with patch(
            "core.report_generation.async_generate_text",
            new_callable=AsyncMock,
        ) as generate_text:
            generate_text.return_value.content = "No report change."
            response = self.client.post(
                reverse("core:report_chat_send", args=[report.id]),
                {"message": "Make a very rich HTML report."},
                HTTP_HX_REQUEST="true",
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("max_tokens", generate_text.call_args.kwargs)

    def test_report_chat_stream_applies_artifact(self):
        report = self.create_report()
        self.client.force_login(self.creator)

        async def fake_stream(*_args, **_kwargs):
            yield "delta", "Done"
            yield "done", {
                "content": "Done",
                "artifact": {
                    "title": "Streamed Pipeline",
                    "database_connection_id": self.database_connection.id,
                    "primary_sql": "select stage from pipeline",
                    "html": "<h1>Streamed Pipeline</h1>",
                },
                "report_updated": True,
                "title": "Streamed Pipeline",
            }

        async def collect_streaming_content(streaming_content):
            chunks = []
            async for chunk in streaming_content:
                chunks.append(chunk)
            return b"".join(chunks)

        with patch("core.views.async_stream_report_chat_response", side_effect=fake_stream):
            response = self.client.post(
                reverse("core:report_chat_stream", args=[report.id]),
                {"message": "Build it"},
            )
            body = async_to_sync(collect_streaming_content)(response.streaming_content).decode()

        report.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: status", body)
        self.assertIn("Preparing database context", body)
        self.assertIn("event: delta", body)
        self.assertIn("event: done", body)
        self.assertEqual(report.title, "Streamed Pipeline")
        self.assertTrue(
            report.chat_messages.filter(
                role=ReportChatMessage.Role.ASSISTANT,
                artifact__title="Streamed Pipeline",
            ).exists()
        )

    def test_company_admin_can_view_creator_report(self):
        report = self.create_report()
        self.client.force_login(self.admin)

        response = self.client.get(reverse("core:report_builder", args=[report.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, report.title)


class AIProviderSettingsTests(TestCase):
    def test_company_admin_can_add_openai_key(self):
        admin = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        self.client.force_login(admin)

        response = self.client.post(
            reverse("core:settings_ai_provider_add"),
            {
                "name": "OpenAI Production",
                "provider": AIProviderKey.Provider.OPENAI,
                "model_name": "gpt-4.1",
                "allowed_model_ids": curated_model_ids(),
                "api_key": "sk-test-secret-value",
            },
        )

        self.assertEqual(response.status_code, 302)
        provider_key = AIProviderKey.objects.get(name="OpenAI Production")
        self.assertEqual(provider_key.organization, organization)
        self.assertEqual(provider_key.key_last_four, "alue")
        self.assertNotEqual(provider_key.encrypted_api_key, "sk-test-secret-value")
        self.assertEqual(provider_key.get_api_key(), "sk-test-secret-value")
        self.assertEqual(
            provider_key.available_models.filter(allowed=True).count(),
            len(CURATED_OPENAI_MODELS),
        )

    def test_add_openai_key_redirects_to_key_detail(self):
        admin = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        self.client.force_login(admin)

        response = self.client.post(
            reverse("core:settings_ai_provider_add"),
            {
                "name": "OpenAI Production",
                "provider": AIProviderKey.Provider.OPENAI,
                "model_name": "gpt-4.1",
                "allowed_model_ids": curated_model_ids(),
                "api_key": "sk-test-secret-value",
            },
        )

        provider_key = AIProviderKey.objects.get(name="OpenAI Production")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            reverse("core:settings_ai_provider_detail", args=[provider_key.id]),
        )

    def test_company_admin_can_add_claude_key(self):
        admin = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        self.client.force_login(admin)

        response = self.client.post(
            reverse("core:settings_ai_provider_add"),
            {
                "name": "Claude Production",
                "provider": AIProviderKey.Provider.ANTHROPIC,
                "model_name": "claude-sonnet-4-6",
                "allowed_model_ids": curated_model_ids(
                    AIProviderKey.Provider.ANTHROPIC
                ),
                "api_key": "sk-ant-test-secret-value",
            },
        )

        self.assertEqual(response.status_code, 302)
        provider_key = AIProviderKey.objects.get(name="Claude Production")
        self.assertEqual(provider_key.provider, AIProviderKey.Provider.ANTHROPIC)
        self.assertEqual(provider_key.model_name, "claude-sonnet-4-6")
        self.assertEqual(
            provider_key.available_models.filter(allowed=True).count(),
            len(CURATED_ANTHROPIC_MODELS),
        )

    def test_company_admin_can_add_gemini_key(self):
        admin = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        self.client.force_login(admin)

        response = self.client.post(
            reverse("core:settings_ai_provider_add"),
            {
                "name": "Gemini Production",
                "provider": AIProviderKey.Provider.GEMINI,
                "model_name": "gemini-3.5-flash",
                "allowed_model_ids": curated_model_ids(AIProviderKey.Provider.GEMINI),
                "api_key": "AIza-test-secret-value",
            },
        )

        self.assertEqual(response.status_code, 302)
        provider_key = AIProviderKey.objects.get(name="Gemini Production")
        self.assertEqual(provider_key.provider, AIProviderKey.Provider.GEMINI)
        self.assertEqual(provider_key.model_name, "gemini-3.5-flash")
        self.assertEqual(
            provider_key.available_models.filter(allowed=True).count(),
            len(CURATED_GEMINI_MODELS),
        )

    def test_ai_provider_list_never_shows_raw_key(self):
        admin = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        provider_key = AIProviderKey(
            organization=organization,
            name="OpenAI Production",
            provider=AIProviderKey.Provider.OPENAI,
            model_name="gpt-4.1",
        )
        provider_key.set_api_key("sk-test-secret-value")
        provider_key.save()
        self.client.force_login(admin)

        response = self.client.get(reverse("core:settings_ai_providers"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "****alue")
        self.assertNotContains(response, "sk-test-secret-value")

    def test_creator_cannot_view_ai_provider_settings(self):
        user = create_user("creator@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=user,
            role=Membership.Role.CREATOR,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("core:settings_ai_providers"))

        self.assertEqual(response.status_code, 403)

    def test_ai_provider_list_shows_delete_instead_of_disable(self):
        admin = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        provider_key = AIProviderKey(
            organization=organization,
            name="OpenAI Production",
            provider=AIProviderKey.Provider.OPENAI,
            model_name="gpt-4.1",
        )
        provider_key.set_api_key("sk-test-secret-value")
        provider_key.save()
        self.client.force_login(admin)

        response = self.client.get(reverse("core:settings_ai_providers"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Delete")
        self.assertContains(
            response,
            reverse("core:settings_ai_provider_delete", args=[provider_key.id]),
        )
        self.assertNotContains(response, "Disable")
        self.assertNotContains(response, "Enable")

    def test_company_admin_can_delete_provider_key(self):
        admin = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        provider_key = AIProviderKey(
            organization=organization,
            name="OpenAI Production",
            provider=AIProviderKey.Provider.OPENAI,
            model_name="gpt-4.1",
        )
        provider_key.set_api_key("sk-test-secret-value")
        provider_key.save()
        provider_model = AIProviderModel.objects.create(
            provider_key=provider_key,
            provider_model_id="gpt-4.1",
            display_name="gpt-4.1",
            allowed=True,
        )
        self.client.force_login(admin)

        response = self.client.post(
            reverse("core:settings_ai_provider_delete", args=[provider_key.id])
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            reverse("core:settings_ai_providers"),
        )
        self.assertFalse(AIProviderKey.objects.filter(id=provider_key.id).exists())
        self.assertFalse(AIProviderModel.objects.filter(id=provider_model.id).exists())

    def test_creator_cannot_delete_provider_key(self):
        user = create_user("creator@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=user,
            role=Membership.Role.CREATOR,
        )
        provider_key = AIProviderKey(
            organization=organization,
            name="OpenAI Production",
            provider=AIProviderKey.Provider.OPENAI,
            model_name="gpt-4.1",
        )
        provider_key.set_api_key("sk-test-secret-value")
        provider_key.save()
        self.client.force_login(user)

        response = self.client.post(
            reverse("core:settings_ai_provider_delete", args=[provider_key.id])
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(AIProviderKey.objects.filter(id=provider_key.id).exists())

    def test_company_admin_can_manage_key_and_provider_models_from_detail_page(self):
        admin = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        provider_key = AIProviderKey(
            organization=organization,
            name="OpenAI Production",
            provider=AIProviderKey.Provider.OPENAI,
            model_name="gpt-4.1",
        )
        provider_key.set_api_key("sk-test-secret-value")
        provider_key.save()
        AIProviderModel.objects.create(
            provider_key=provider_key,
            provider_model_id="gpt-4.1",
            display_name="gpt-4.1",
            allowed=True,
        )
        self.client.force_login(admin)

        response = self.client.get(
            reverse("core:settings_ai_provider_detail", args=[provider_key.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Provider Key")
        self.assertContains(response, "gpt-4.1")
        self.assertContains(response, "Allowed")

    def test_creator_cannot_manage_provider_key_detail(self):
        user = create_user("creator@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=user,
            role=Membership.Role.CREATOR,
        )
        provider_key = AIProviderKey(
            organization=organization,
            name="OpenAI Production",
            provider=AIProviderKey.Provider.OPENAI,
            model_name="gpt-4.1",
        )
        provider_key.set_api_key("sk-test-secret-value")
        provider_key.save()
        self.client.force_login(user)

        response = self.client.get(
            reverse("core:settings_ai_provider_detail", args=[provider_key.id])
        )

        self.assertEqual(response.status_code, 403)

    def test_company_admin_can_update_key_settings_from_detail_page(self):
        admin = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        provider_key = AIProviderKey(
            organization=organization,
            name="OpenAI Production",
            provider=AIProviderKey.Provider.OPENAI,
            model_name="gpt-4.1",
        )
        provider_key.set_api_key("sk-test-secret-value")
        provider_key.save()
        AIProviderModel.objects.create(
            provider_key=provider_key,
            provider_model_id="gpt-5.4-mini",
            display_name="GPT-5.4 mini",
            allowed=False,
        )
        self.client.force_login(admin)

        response = self.client.post(
            reverse("core:settings_ai_provider_detail", args=[provider_key.id]),
            {
                "name": "OpenAI Updated",
                "model_name": "gpt-5.4-mini",
                "allowed_model_ids": ["gpt-5.4-mini"],
                "api_key": "sk-replacement-value",
            },
        )

        provider_key.refresh_from_db()
        model = provider_key.available_models.get(provider_model_id="gpt-5.4-mini")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(provider_key.name, "OpenAI Updated")
        self.assertEqual(provider_key.model_name, "gpt-5.4-mini")
        self.assertEqual(provider_key.key_last_four, "alue")
        self.assertEqual(provider_key.get_api_key(), "sk-replacement-value")
        self.assertTrue(model.allowed)

    def test_company_admin_can_save_model_checklist_from_detail_page(self):
        admin = create_user("admin@example.com")
        organization = Organization.objects.create(name="Internal Test Org")
        Membership.objects.create(
            organization=organization,
            user=admin,
            role=Membership.Role.ADMIN,
        )
        provider_key = AIProviderKey(
            organization=organization,
            name="OpenAI Production",
            provider=AIProviderKey.Provider.OPENAI,
            model_name="gpt-4.1",
        )
        provider_key.set_api_key("sk-test-secret-value")
        provider_key.save()
        sync_provider_models(provider_key, allowed_model_ids=curated_model_ids())
        self.client.force_login(admin)

        response = self.client.post(
            reverse("core:settings_ai_provider_detail", args=[provider_key.id]),
            {
                "name": "OpenAI Production",
                "model_name": "gpt-5.4-mini",
                "allowed_model_ids": ["gpt-5.4-mini", "gpt-4.1-mini"],
            },
        )

        allowed_ids = set(
            provider_key.available_models.filter(allowed=True).values_list(
                "provider_model_id",
                flat=True,
            )
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(allowed_ids, {"gpt-5.4-mini", "gpt-4.1-mini"})


class AIProviderModelSyncTests(TestCase):
    def test_curated_openai_models_are_seeded_in_database(self):
        model_ids = set(AIModelCatalog.objects.values_list("model_id", flat=True))

        self.assertIn("gpt-5.5", model_ids)
        self.assertIn("gpt-5.4-mini", model_ids)
        self.assertIn("gpt-4.1-nano", model_ids)
        self.assertNotIn("whisper-1", model_ids)

    def test_curated_claude_and_gemini_models_are_seeded_in_database(self):
        model_ids = set(AIModelCatalog.objects.values_list("model_id", flat=True))

        self.assertIn("claude-opus-4-8", model_ids)
        self.assertIn("claude-sonnet-4-6", model_ids)
        self.assertIn("claude-haiku-4-5", model_ids)
        self.assertIn("gemini-3.5-flash", model_ids)
        self.assertIn("gemini-2.5-flash-lite", model_ids)
        self.assertNotIn("claude-haiku-4-5-20251001", model_ids)
        self.assertNotIn("gemini-2.5-flash-image", model_ids)

    def test_add_provider_form_uses_database_catalog_for_default_model_choices(self):
        form = AIProviderKeyCreateForm()
        rendered_field = str(form["model_name"])
        rendered_checklist = str(form["allowed_model_ids"])

        self.assertIn(("gpt-5.4-mini", "GPT-5.4 mini"), form.fields["model_name"].choices)
        self.assertIn('value="gpt-5.4-mini"', rendered_field)
        self.assertIn(">GPT-5.4 mini</option>", rendered_field)
        self.assertIn('value="gpt-5.4-mini"', rendered_checklist)
        self.assertIn("checked", rendered_checklist)

    def test_add_provider_form_uses_selected_provider_model_choices(self):
        form = AIProviderKeyCreateForm(
            data={
                "name": "Gemini Production",
                "provider": AIProviderKey.Provider.GEMINI,
                "model_name": "gemini-3.5-flash",
                "allowed_model_ids": ["gemini-3.5-flash"],
                "api_key": "AIza-test-secret-value",
            }
        )

        self.assertTrue(form.is_valid())
        self.assertIn(
            ("gemini-3.5-flash", "Gemini 3.5 Flash"),
            form.fields["model_name"].choices,
        )
        self.assertNotIn(
            ("gpt-5.4-mini", "GPT-5.4 mini"),
            form.fields["model_name"].choices,
        )

    def test_curated_openai_models_are_sane_for_report_generation(self):
        model_ids = [model["id"] for model in CURATED_OPENAI_MODELS]

        self.assertLessEqual(len(model_ids), 10)
        self.assertIn("gpt-5.5", model_ids)
        self.assertIn("gpt-5.4-mini", model_ids)
        self.assertIn("gpt-5.4-nano", model_ids)
        self.assertNotIn("whisper-1", model_ids)
        self.assertNotIn("gpt-4o-transcribe", model_ids)
        self.assertFalse(any("tts" in model_id for model_id in model_ids))
        self.assertFalse(any("image" in model_id for model_id in model_ids))
        self.assertFalse(any("realtime" in model_id for model_id in model_ids))
        self.assertFalse(
            any("-202" in model_id or "-203" in model_id for model_id in model_ids)
        )

    def test_curated_claude_and_gemini_models_are_sane_for_report_generation(self):
        model_ids = [
            model["id"]
            for model in CURATED_ANTHROPIC_MODELS + CURATED_GEMINI_MODELS
        ]

        self.assertLessEqual(len(CURATED_ANTHROPIC_MODELS), 10)
        self.assertLessEqual(len(CURATED_GEMINI_MODELS), 10)
        self.assertIn("claude-sonnet-4-6", model_ids)
        self.assertIn("claude-haiku-4-5", model_ids)
        self.assertIn("gemini-3.5-flash", model_ids)
        self.assertIn("gemini-2.5-flash-lite", model_ids)
        self.assertFalse(any("tts" in model_id for model_id in model_ids))
        self.assertFalse(any("image" in model_id for model_id in model_ids))
        self.assertFalse(any("live" in model_id for model_id in model_ids))
        self.assertFalse(
            any("-202" in model_id or "-203" in model_id for model_id in model_ids)
        )

    def test_sync_creates_curated_models_and_allows_all_by_default(self):
        organization = Organization.objects.create(name="Internal Test Org")
        provider_key = AIProviderKey(
            organization=organization,
            name="OpenAI Production",
            provider=AIProviderKey.Provider.OPENAI,
            model_name="gpt-5.4-mini",
        )
        provider_key.set_api_key("sk-test-secret-value")
        provider_key.save()

        count = sync_provider_models(provider_key)

        self.assertEqual(count, len(CURATED_OPENAI_MODELS))
        default_model = AIProviderModel.objects.get(
            provider_key=provider_key,
            provider_model_id="gpt-5.4-mini",
        )
        other_model = AIProviderModel.objects.get(
            provider_key=provider_key,
            provider_model_id="gpt-5.5",
        )
        self.assertTrue(default_model.allowed)
        self.assertTrue(default_model.available)
        self.assertTrue(other_model.allowed)
        self.assertTrue(other_model.available)

    def test_sync_creates_curated_models_with_selected_allowed_models(self):
        organization = Organization.objects.create(name="Internal Test Org")
        provider_key = AIProviderKey(
            organization=organization,
            name="OpenAI Production",
            provider=AIProviderKey.Provider.OPENAI,
            model_name="gpt-5.4-mini",
        )
        provider_key.set_api_key("sk-test-secret-value")
        provider_key.save()

        count = sync_provider_models(
            provider_key,
            allowed_model_ids=["gpt-5.4-mini", "gpt-4.1-mini"],
        )

        self.assertEqual(count, len(CURATED_OPENAI_MODELS))
        self.assertTrue(
            AIProviderModel.objects.get(
                provider_key=provider_key,
                provider_model_id="gpt-5.4-mini",
            ).allowed
        )
        self.assertFalse(
            AIProviderModel.objects.get(
                provider_key=provider_key,
                provider_model_id="gpt-5.5",
            ).allowed
        )

    def test_sync_creates_curated_models_for_claude(self):
        organization = Organization.objects.create(name="Internal Test Org")
        provider_key = AIProviderKey(
            organization=organization,
            name="Claude Production",
            provider=AIProviderKey.Provider.ANTHROPIC,
            model_name="claude-sonnet-4-6",
        )
        provider_key.set_api_key("sk-ant-test-secret-value")
        provider_key.save()

        count = sync_provider_models(provider_key)

        self.assertEqual(count, len(CURATED_ANTHROPIC_MODELS))
        self.assertTrue(
            AIProviderModel.objects.get(
                provider_key=provider_key,
                provider_model_id="claude-sonnet-4-6",
            ).allowed
        )

    def test_sync_creates_curated_models_for_gemini(self):
        organization = Organization.objects.create(name="Internal Test Org")
        provider_key = AIProviderKey(
            organization=organization,
            name="Gemini Production",
            provider=AIProviderKey.Provider.GEMINI,
            model_name="gemini-3.5-flash",
        )
        provider_key.set_api_key("AIza-test-secret-value")
        provider_key.save()

        count = sync_provider_models(provider_key)

        self.assertEqual(count, len(CURATED_GEMINI_MODELS))
        self.assertTrue(
            AIProviderModel.objects.get(
                provider_key=provider_key,
                provider_model_id="gemini-3.5-flash",
            ).allowed
        )

    def test_refresh_marks_non_curated_models_unavailable_without_changing_allowed(self):
        organization = Organization.objects.create(name="Internal Test Org")
        provider_key = AIProviderKey(
            organization=organization,
            name="OpenAI Production",
            provider=AIProviderKey.Provider.OPENAI,
            model_name="gpt-4.1",
        )
        provider_key.set_api_key("sk-test-secret-value")
        provider_key.save()
        old_model = AIProviderModel.objects.create(
            provider_key=provider_key,
            provider_model_id="old-model",
            display_name="old-model",
            allowed=True,
            available=True,
        )

        sync_provider_models(provider_key)

        old_model.refresh_from_db()
        self.assertFalse(old_model.available)
        self.assertTrue(old_model.allowed)
