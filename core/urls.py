from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("reports/", views.reports_placeholder, name="reports_placeholder"),
    path("settings/", views.settings_home, name="settings_home"),
    path(
        "settings/report-limits/",
        views.settings_report_limits,
        name="settings_report_limits",
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
