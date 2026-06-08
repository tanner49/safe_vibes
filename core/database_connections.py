from dataclasses import dataclass

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
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
