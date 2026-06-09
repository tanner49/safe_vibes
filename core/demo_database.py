from django.conf import settings

from .database_connections import build_sqlite_connection_string, sanitize_connection_string
from .models import DatabaseConnection


def demo_database_path():
    return settings.DEMO_DATABASE_PATH


def demo_database_connection_string():
    return build_sqlite_connection_string(str(demo_database_path()))


def ensure_demo_database_connection(organization):
    if not settings.ENABLE_DEMO_DATABASE_CONNECTION:
        return None, False
    if not demo_database_path().exists():
        return None, False

    database_connection, created = DatabaseConnection.objects.get_or_create(
        organization=organization,
        name=settings.DEMO_DATABASE_CONNECTION_NAME,
        defaults={
            "provider": DatabaseConnection.Provider.SQLITE,
            "enabled": True,
            "encrypted_connection_string": "",
            "connection_string_preview": "",
        },
    )
    connection_string = demo_database_connection_string()
    preview = sanitize_connection_string(connection_string)
    if created or database_connection.get_connection_string() != connection_string:
        database_connection.provider = DatabaseConnection.Provider.SQLITE
        database_connection.enabled = True
        database_connection.set_connection_string(connection_string, preview)
        database_connection.save(
            update_fields=[
                "provider",
                "enabled",
                "encrypted_connection_string",
                "connection_string_preview",
                "updated_at",
            ]
        )
    return database_connection, created
