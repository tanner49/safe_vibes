from django.contrib import admin

from .models import (
    AIModelCatalog,
    AIProviderKey,
    AIProviderModel,
    Membership,
    Organization,
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
        "cache_ttl_seconds",
        "max_rows",
        "created_at",
    ]
    list_filter = ["sso_required", "created_at"]
    search_fields = ["name", "slug"]
    prepopulated_fields = {"slug": ("name",)}
    inlines = [MembershipInline]
    fieldsets = [
        (None, {"fields": ["name", "slug", "sso_required"]}),
        (
            "Report Limits",
            {
                "fields": [
                    "query_timeout_seconds",
                    "cache_ttl_seconds",
                    "max_rows",
                    "max_raw_bytes",
                    "max_compressed_bytes",
                ]
            },
        ),
    ]


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
