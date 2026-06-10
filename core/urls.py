from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("builder/", views.builder_home, name="builder_home"),
    path("builder/new/", views.builder_new, name="builder_new"),
    path("builder/import/", views.builder_import, name="builder_import"),
    path("builder/<int:report_id>/", views.report_builder, name="report_builder"),
    path(
        "builder/<int:report_id>/delete/",
        views.report_draft_delete,
        name="report_draft_delete",
    ),
    path(
        "builder/<int:report_id>/model/",
        views.report_model_update,
        name="report_model_update",
    ),
    path(
        "builder/<int:report_id>/publish/",
        views.report_publish,
        name="report_publish",
    ),
    path("builder/<int:report_id>/chat/", views.report_chat_send, name="report_chat_send"),
    path(
        "builder/<int:report_id>/chat/stream/",
        views.report_chat_stream,
        name="report_chat_stream",
    ),
    path("reports/", views.reports_placeholder, name="reports_placeholder"),
    path("reports/<int:report_id>/preview/", views.report_preview, name="report_preview"),
    path("reports/<int:report_id>/share/", views.report_share, name="report_share"),
    path(
        "reports/<int:report_id>/share/options/",
        views.report_share_options,
        name="report_share_options",
    ),
    path("reports/<int:report_id>/delete/", views.report_delete, name="report_delete"),
    path(
        "reports/<int:report_id>/preview/error/",
        views.report_preview_error,
        name="report_preview_error",
    ),
    path(
        "reports/<int:report_id>/dataset/primary/",
        views.report_primary_dataset,
        name="report_primary_dataset",
    ),
    path("settings/", views.settings_home, name="settings_home"),
    path("settings/users/", views.settings_users, name="settings_users"),
    path(
        "settings/report-limits/",
        views.settings_report_limits,
        name="settings_report_limits",
    ),
    path("settings/security/", views.settings_security, name="settings_security"),
    path(
        "settings/connections/",
        views.settings_database_connections,
        name="settings_database_connections",
    ),
    path(
        "settings/connections/add/",
        views.settings_database_connection_add,
        name="settings_database_connection_add",
    ),
    path(
        "settings/connections/<int:connection_id>/test/",
        views.settings_database_connection_test,
        name="settings_database_connection_test",
    ),
    path(
        "settings/connections/<int:connection_id>/delete/",
        views.settings_database_connection_delete,
        name="settings_database_connection_delete",
    ),
    path(
        "settings/connections/<int:connection_id>/",
        views.settings_database_connection_detail,
        name="settings_database_connection_detail",
    ),
    path("settings/users/add/", views.settings_user_add, name="settings_user_add"),
    path(
        "settings/users/<int:membership_id>/remove/",
        views.settings_user_remove,
        name="settings_user_remove",
    ),
    path(
        "settings/ai-providers/",
        views.settings_ai_providers,
        name="settings_ai_providers",
    ),
    path(
        "settings/ai-providers/add/",
        views.settings_ai_provider_add,
        name="settings_ai_provider_add",
    ),
    path(
        "settings/ai-providers/<int:provider_key_id>/delete/",
        views.settings_ai_provider_delete,
        name="settings_ai_provider_delete",
    ),
    path(
        "settings/ai-providers/<int:provider_key_id>/",
        views.settings_ai_provider_detail,
        name="settings_ai_provider_detail",
    ),
    path(
        "settings/ai-providers/<int:provider_key_id>/models/",
        views.settings_ai_provider_models,
        name="settings_ai_provider_models",
    ),
]
