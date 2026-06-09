from dataclasses import dataclass

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError


class DatabaseConnectionError(Exception):
    pass


@dataclass
class ConnectionTestResult:
    succeeded: bool
    message: str


def sanitize_connection_string(connection_string):
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
    password,
    database,
    schema,
    warehouse,
    role,
):
    query = {}
    if warehouse:
        query["warehouse"] = warehouse
    if role:
        query["role"] = role
    return URL.create(
        "snowflake",
        username=username,
        password=password,
        host=account,
        database="/".join(part for part in [database, schema] if part),
        query=query,
    ).render_as_string(hide_password=False)


def build_bigquery_connection_string(project_id, dataset_id, credentials_path):
    query = {}
    if credentials_path:
        query["credentials_path"] = credentials_path
    return URL.create(
        "bigquery",
        host=project_id,
        database=dataset_id,
        query=query,
    ).render_as_string(hide_password=False)


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
