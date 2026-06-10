from django.contrib import admin

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


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 1
    autocomplete_fields = ["user"]


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "slug",
        "sso_required",
        "query_timeout_seconds",
        "report_cache_enabled",
        "cache_ttl_seconds",
        "max_rows",
        "created_at",
    ]
    list_filter = ["sso_required", "created_at"]
    search_fields = ["name", "slug"]
    prepopulated_fields = {"slug": ("name",)}
    inlines = [MembershipInline]
    fieldsets = [
        (None, {"fields": ["name", "slug"]}),
        (
            "SSO",
            {
                "fields": [
                    "sso_oidc_enabled",
                    "sso_required",
                    "sso_oidc_issuer_url",
                    "sso_oidc_client_id",
                    "sso_oidc_client_secret_last_four",
                    "sso_oidc_scopes",
                ]
            },
        ),
        (
            "Report Limits",
            {
                "fields": [
                    "query_timeout_seconds",
                    "report_cache_enabled",
                    "cache_ttl_seconds",
                    "max_rows",
                    "max_raw_bytes",
                    "max_compressed_bytes",
                ]
            },
        ),
        (
            "Security",
            {
                "fields": [
                    "report_ip_allowlist_enabled",
                    "report_ip_allowlist",
                    "report_url_whitelist_enabled",
                    "report_url_whitelist",
                    "report_url_blacklist_enabled",
                    "report_url_blacklist",
                ]
            },
        ),
    ]
    readonly_fields = ["sso_oidc_client_secret_last_four"]
    exclude = ["encrypted_sso_oidc_client_secret"]


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "organization", "role", "created_at"]
    list_filter = ["role", "organization"]
    search_fields = [
        "user__email",
        "organization__name",
        "organization__slug",
    ]
    autocomplete_fields = ["user", "organization"]


@admin.register(AIProviderKey)
class AIProviderKeyAdmin(admin.ModelAdmin):
    list_display = ["name", "organization", "provider", "model_name", "created_at"]
    list_filter = ["provider", "organization"]
    search_fields = ["name", "organization__name", "model_name"]
    readonly_fields = ["key_last_four", "created_at", "updated_at"]
    exclude = ["encrypted_api_key"]


@admin.register(AIProviderModel)
class AIProviderModelAdmin(admin.ModelAdmin):
    list_display = [
        "provider_model_id",
        "provider_key",
        "allowed",
        "available",
        "last_seen_at",
    ]
    list_filter = ["allowed", "available", "provider_key__provider"]
    search_fields = [
        "provider_model_id",
        "display_name",
        "provider_key__name",
        "provider_key__organization__name",
    ]


@admin.register(AIModelCatalog)
class AIModelCatalogAdmin(admin.ModelAdmin):
    list_display = ["display_name", "model_id", "provider", "enabled", "sort_order"]
    list_filter = ["provider", "enabled"]
    search_fields = ["display_name", "model_id"]
    ordering = ["provider", "sort_order", "display_name"]


@admin.register(DatabaseConnection)
class DatabaseConnectionAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "organization",
        "provider",
        "enabled",
        "last_test_succeeded",
        "last_tested_at",
        "created_at",
    ]
    list_filter = ["provider", "enabled", "last_test_succeeded", "organization"]
    search_fields = ["name", "organization__name", "connection_string_preview"]
    readonly_fields = [
        "connection_string_preview",
        "last_tested_at",
        "last_test_succeeded",
        "last_test_error",
        "created_at",
        "updated_at",
    ]
    exclude = ["encrypted_connection_string"]


@admin.register(QueryExecutionLog)
class QueryExecutionLogAdmin(admin.ModelAdmin):
    list_display = [
        "created_at",
        "organization",
        "database_connection",
        "user",
        "succeeded",
        "row_count",
        "raw_bytes",
        "duration_ms",
    ]
    list_filter = ["succeeded", "organization", "database_connection"]
    search_fields = [
        "organization__name",
        "database_connection__name",
        "user__email",
        "sql_preview",
        "error_message",
    ]
    readonly_fields = [
        "organization",
        "database_connection",
        "user",
        "sql_preview",
        "succeeded",
        "row_count",
        "raw_bytes",
        "duration_ms",
        "error_message",
        "created_at",
    ]


class ReportChatMessageInline(admin.TabularInline):
    model = ReportChatMessage
    extra = 0
    readonly_fields = ["user", "role", "content", "created_at"]
    can_delete = False


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ["title", "organization", "owner", "database_connection", "status", "updated_at"]
    list_filter = ["status", "organization", "database_connection"]
    search_fields = ["title", "primary_sql", "html", "owner__email", "organization__name"]
    autocomplete_fields = ["organization", "owner", "database_connection", "ai_provider_key"]
    inlines = [ReportChatMessageInline]


@admin.register(ReportChatMessage)
class ReportChatMessageAdmin(admin.ModelAdmin):
    list_display = ["report", "role", "user", "created_at"]
    list_filter = ["role", "report__organization"]
    search_fields = ["report__title", "user__email", "content"]


@admin.register(ReportDatasetCache)
class ReportDatasetCacheAdmin(admin.ModelAdmin):
    list_display = [
        "report",
        "organization",
        "dataset_name",
        "row_count",
        "raw_bytes",
        "compressed_bytes",
        "expires_at",
        "updated_at",
    ]
    list_filter = ["organization", "dataset_name", "expires_at"]
    search_fields = ["report__title", "organization__name", "sql_preview", "cache_key"]
    readonly_fields = [
        "organization",
        "report",
        "database_connection",
        "dataset_name",
        "cache_key",
        "sql_preview",
        "raw_bytes",
        "compressed_bytes",
        "row_count",
        "expires_at",
        "created_at",
        "updated_at",
    ]
    exclude = ["compressed_payload"]


@admin.register(ReportDatasetCacheLock)
class ReportDatasetCacheLockAdmin(admin.ModelAdmin):
    list_display = ["cache_key", "created_at", "updated_at"]
    search_fields = ["cache_key"]
    readonly_fields = ["cache_key", "created_at", "updated_at"]
