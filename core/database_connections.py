from dataclasses import dataclass
import json

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError


class DatabaseConnectionError(Exception):
    pass


@dataclass
class ConnectionTestResult:
    succeeded: bool
    message: str


CONNECTION_CONFIG_VERSION = 1
CONNECTION_CONFIG_KEY = "save_vibes_connection"
LEGACY_CONNECTION_CONFIG_KEY = "safe_reports_connection"


def build_connection_config(provider, **config):
    return json.dumps(
        {
            CONNECTION_CONFIG_KEY: CONNECTION_CONFIG_VERSION,
            "provider": provider,
            "config": config,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def parse_connection_config(raw_connection):
    try:
        payload = json.loads(raw_connection)
    except (TypeError, json.JSONDecodeError):
        return None
    config_version = payload.get(CONNECTION_CONFIG_KEY)
    if config_version is None:
        config_version = payload.get(LEGACY_CONNECTION_CONFIG_KEY)
    if config_version != CONNECTION_CONFIG_VERSION:
        return None
    return payload


def connection_config_preview(provider, parts):
    if provider == "snowflake":
        account = parts.get("account") or ""
        database = parts.get("database") or ""
        schema = parts.get("schema") or ""
        namespace = ".".join(part for part in [database, schema] if part)
        auth_type = parts.get("auth_type", "").replace("_", " ")
        return f"snowflake://{account}/{namespace}?auth={auth_type or 'token'}"
    if provider == "bigquery":
        project_id = parts.get("project_id") or ""
        dataset_id = parts.get("dataset_id") or ""
        location = parts.get("location") or ""
        suffix = f"?location={location}" if location else ""
        return f"bigquery://{project_id}/{dataset_id}{suffix}"
    return f"{provider} connection"


def sanitize_connection_string(connection_string):
    connection_config = parse_connection_config(connection_string)
    if connection_config:
        return connection_config_preview(
            connection_config.get("provider"),
            connection_config.get("config", {}),
        )
    try:
        return make_url(connection_string).render_as_string(hide_password=True)
    except Exception as exc:
        raise DatabaseConnectionError("Enter a valid SQLAlchemy connection string.") from exc


def build_postgres_connection_string(host, port, database, username, password, sslmode):
    query = {"sslmode": sslmode} if sslmode else {}
    return URL.create(
        "postgresql+psycopg",
        username=username,
        password=password,
        host=host,
        port=int(port) if port else None,
        database=database,
        query=query,
    ).render_as_string(hide_password=False)


def build_snowflake_connection_string(
    account,
    username,
    token,
    database,
    schema,
    warehouse,
    role,
    auth_type="programmatic_access_token",
    private_key="",
    private_key_passphrase="",
):
    return build_connection_config(
        "snowflake",
        account=account.strip(),
        username=username.strip(),
        token=token,
        auth_type=auth_type,
        private_key=private_key,
        private_key_passphrase=private_key_passphrase,
        database=database.strip(),
        schema=(schema or "").strip(),
        warehouse=(warehouse or "").strip(),
        role=(role or "").strip(),
    )


def build_bigquery_connection_string(
    project_id,
    dataset_id,
    service_account_json,
    location="",
):
    return build_connection_config(
        "bigquery",
        project_id=project_id.strip(),
        dataset_id=dataset_id.strip(),
        service_account_json=service_account_json,
        location=(location or "").strip(),
    )


def build_sqlite_connection_string(path):
    if path == ":memory:":
        return "sqlite:///:memory:"
    return URL.create("sqlite", database=path.replace("\\", "/")).render_as_string(
        hide_password=False
    )


def redact_connection_error(message, connection_string):
    redacted = message
    try:
        url = make_url(connection_string)
        preview = url.render_as_string(hide_password=True)
        redacted = redacted.replace(connection_string, preview)
        if url.password:
            redacted = redacted.replace(str(url.password), "***")
    except Exception:
        redacted = redacted.replace(connection_string, "[connection string redacted]")
    return redacted


def test_sqlalchemy_connection(connection_string):
    engine = None
    try:
        engine = create_engine(connection_string, pool_pre_ping=True)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return ConnectionTestResult(True, "Connection test succeeded.")
    except SQLAlchemyError as exc:
        return ConnectionTestResult(
            False,
            redact_connection_error(str(exc), connection_string),
        )
    finally:
        if engine is not None:
            engine.dispose()


def test_database_connection(database_connection):
    if database_connection.provider in {"snowflake", "bigquery"}:
        from asgiref.sync import async_to_sync

        from .query_execution import async_execute_query

        try:
            async_to_sync(async_execute_query)(database_connection, "select 1")
            return ConnectionTestResult(True, "Connection test succeeded.")
        except Exception as exc:
            return ConnectionTestResult(False, str(exc))
    return test_sqlalchemy_connection(database_connection.get_connection_string())
