from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .ai_provider_models import (
    CURATED_ANTHROPIC_MODELS,
    CURATED_GEMINI_MODELS,
    CURATED_OPENAI_MODELS,
    sync_provider_models,
)
from .database_connections import redact_connection_error
from .forms import AIProviderKeyCreateForm, DatabaseConnectionCreateForm
from .models import (
    AIModelCatalog,
    AIProviderKey,
    AIProviderModel,
    DatabaseConnection,
    Membership,
    Organization,
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
        self.assertEqual(response.headers["Location"], reverse("core:dashboard"))


class DashboardTests(TestCase):
    def test_dashboard_shows_current_organization(self):
        user = create_user("creator@example.com")
        organization = Organization.objects.create(name="Demo Sales Org")
        Membership.objects.create(
            organization=organization,
            user=user,
            role=Membership.Role.CREATOR,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("core:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Demo Sales Org")
        self.assertContains(response, "Creator")

    def test_creator_dashboard_does_not_render_settings_links(self):
        user = create_user("creator@example.com")
        organization = Organization.objects.create(name="Demo Sales Org")
        Membership.objects.create(
            organization=organization,
            user=user,
            role=Membership.Role.CREATOR,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("core:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, reverse("core:settings_home"))
        self.assertNotContains(response, "Company settings")

    def test_company_admin_dashboard_renders_settings_links(self):
        user = create_user("admin@example.com")
        organization = Organization.objects.create(name="Demo Sales Org")
        Membership.objects.create(
            organization=organization,
            user=user,
            role=Membership.Role.ADMIN,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("core:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("core:settings_home"))
        self.assertContains(response, "Company settings")


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
                "connection_string": raw_connection_string,
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
                "connection_string": "sqlite:///updated.sqlite3",
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
                "provider": DatabaseConnection.Provider.OTHER,
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
