import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time
from decimal import Decimal
from uuid import UUID

from asgiref.sync import async_to_sync, sync_to_async
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from .database_connections import redact_connection_error
from .models import DatabaseConnection, QueryExecutionLog


DISALLOWED_SQL_KEYWORDS = re.compile(
    r"\b(alter|call|copy|create|delete|drop|grant|insert|merge|revoke|truncate|update)\b",
    re.IGNORECASE,
)

ASYNC_SQLALCHEMY_DRIVERS = {
    "postgresql+asyncpg",
    "postgresql+psycopg",
    "sqlite+aiosqlite",
}


class QueryExecutionError(Exception):
    pass


class QueryPolicyError(QueryExecutionError):
    pass


@dataclass
class QueryExecutionResult:
    columns: list[str]
    rows: list[dict]
    row_count: int
    raw_bytes: int
    duration_ms: int


def json_safe_value(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, datetime_time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    return value


def json_safe_row(row):
    return {key: json_safe_value(value) for key, value in row.items()}


def raw_json_size(payload):
    return len(json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"))


def sql_preview(sql):
    return " ".join(sql.split())[:2000]


def normalized_sql_starts_with_read(sql):
    stripped = sql.lstrip()
    while stripped.startswith("--"):
        _line, _separator, stripped = stripped.partition("\n")
        stripped = stripped.lstrip()
    lowered = stripped.lower()
    return lowered.startswith("select") or lowered.startswith("with")


def validate_read_only_sql(sql):
    if not normalized_sql_starts_with_read(sql):
        raise QueryPolicyError("Only read-only SELECT queries are allowed.")
    if DISALLOWED_SQL_KEYWORDS.search(sql):
        raise QueryPolicyError("Write and schema-changing SQL statements are not allowed.")


def execute_query(
    database_connection,
    sql,
    user=None,
    cache_status=QueryExecutionLog.CacheStatus.MISS,
):
    return async_to_sync(async_execute_query)(
        database_connection,
        sql,
        user=user,
        cache_status=cache_status,
    )


async def async_execute_query(
    database_connection,
    sql,
    user=None,
    cache_status=QueryExecutionLog.CacheStatus.MISS,
):
    organization = await sync_to_async(
        lambda: database_connection.organization,
        thread_sensitive=True,
    )()
    started = time.monotonic()
    log = await sync_to_async(QueryExecutionLog.objects.create, thread_sensitive=True)(
        organization=organization,
        database_connection=database_connection,
        user=user if user and user.is_authenticated else None,
        sql_preview=sql_preview(sql),
        cache_status=cache_status,
    )

    try:
        result = await _async_execute_query(database_connection, sql, organization)
    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        log.succeeded = False
        log.duration_ms = duration_ms
        log.error_message = str(exc)[:2000]
        await sync_to_async(log.save, thread_sensitive=True)(
            update_fields=[
                "succeeded",
                "duration_ms",
                "error_message",
            ]
        )
        raise

    log.succeeded = True
    log.row_count = result.row_count
    log.raw_bytes = result.raw_bytes
    log.duration_ms = result.duration_ms
    await sync_to_async(log.save, thread_sensitive=True)(
        update_fields=[
            "succeeded",
            "row_count",
            "raw_bytes",
            "duration_ms",
        ]
    )
    return result


async def _async_execute_query(database_connection, sql, organization):
    if not database_connection.enabled:
        raise QueryPolicyError("This database connection is disabled.")
    validate_read_only_sql(sql)

    if database_connection.provider == DatabaseConnection.Provider.SNOWFLAKE:
        from .warehouse_adapters import async_execute_snowflake_query

        return await async_execute_snowflake_query(database_connection, sql, organization)
    if database_connection.provider == DatabaseConnection.Provider.BIGQUERY:
        from .warehouse_adapters import async_execute_bigquery_query

        return await async_execute_bigquery_query(database_connection, sql, organization)

    connection_string = database_connection.get_connection_string()
    async_connection_string = async_sqlalchemy_connection_string(connection_string)
    engine = None
    started = time.monotonic()
    try:
        engine = create_async_engine(async_connection_string, pool_pre_ping=True)
        async with engine.connect() as connection:
            await apply_connection_timeout(
                connection,
                database_connection.provider,
                organization,
            )
            result = await connection.execute(text(sql))
            rows = result.mappings().fetchmany(organization.max_rows + 1)
            if len(rows) > organization.max_rows:
                raise QueryPolicyError(
                    f"Query returned more than the allowed {organization.max_rows} rows."
                )

            safe_rows = [json_safe_row(dict(row)) for row in rows]
            columns = list(result.keys())
            raw_bytes = raw_json_size({"columns": columns, "rows": safe_rows})
            if raw_bytes > organization.max_raw_bytes:
                raise QueryPolicyError(
                    f"Query result is {raw_bytes} bytes, above the allowed {organization.max_raw_bytes} bytes."
                )
            duration_ms = int((time.monotonic() - started) * 1000)
            return QueryExecutionResult(
                columns=columns,
                rows=safe_rows,
                row_count=len(safe_rows),
                raw_bytes=raw_bytes,
                duration_ms=duration_ms,
            )
    except SQLAlchemyError as exc:
        raise QueryExecutionError(
            redact_connection_error(str(exc), async_connection_string)
        ) from exc
    finally:
        if engine is not None:
            await engine.dispose()


def async_sqlalchemy_connection_string(connection_string):
    url = make_url(connection_string)
    drivername = url.drivername
    if drivername in {"sqlite", "sqlite+pysqlite"}:
        url = url.set(drivername="sqlite+aiosqlite")
    elif drivername in {"postgresql", "postgresql+psycopg", "postgres"}:
        query = dict(url.query)
        sslmode = query.pop("sslmode", "")
        if sslmode and sslmode != "disable":
            query["ssl"] = sslmode
        url = url.set(drivername="postgresql+asyncpg", query=query)

    if url.drivername not in ASYNC_SQLALCHEMY_DRIVERS:
        raise QueryExecutionError(
            f"Connection driver '{drivername}' is not supported for async report execution."
        )
    return url.render_as_string(hide_password=False)


async def apply_connection_timeout(connection, provider, organization):
    timeout_seconds = organization.query_timeout_seconds
    if provider == DatabaseConnection.Provider.POSTGRES:
        timeout_ms = int(timeout_seconds * 1000)
        await connection.execute(text(f"SET statement_timeout = {timeout_ms}"))
    elif provider == DatabaseConnection.Provider.SQLITE:
        timeout_ms = int(timeout_seconds * 1000)
        await connection.execute(text(f"PRAGMA busy_timeout = {timeout_ms}"))
