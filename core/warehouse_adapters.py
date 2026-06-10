import asyncio
import base64
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from cryptography.hazmat.primitives import serialization

from .database_connections import parse_connection_config
from .models import DatabaseConnection
from .query_execution import (
    QueryExecutionError,
    QueryExecutionResult,
    QueryPolicyError,
    json_safe_row,
    raw_json_size,
)


BIGQUERY_SCOPE = "https://www.googleapis.com/auth/bigquery"
BIGQUERY_API_ROOT = "https://bigquery.googleapis.com/bigquery/v2"
BIGQUERY_TOKEN_AUDIENCE = "https://oauth2.googleapis.com/token"
SNOWFLAKE_API_USER_AGENT = "save-vibes/0.1"


def provider_config(database_connection, expected_provider):
    payload = parse_connection_config(database_connection.get_connection_string())
    if not payload or payload.get("provider") != expected_provider:
        raise QueryExecutionError(
            f"{database_connection.get_provider_display()} credentials need to be saved again before async execution."
        )
    return payload.get("config") or {}


def build_query_result(columns, rows, organization, started):
    if len(rows) > organization.max_rows:
        raise QueryPolicyError(
            f"Query returned more than the allowed {organization.max_rows} rows."
        )
    safe_rows = [json_safe_row(row) for row in rows]
    raw_bytes = raw_json_size({"columns": columns, "rows": safe_rows})
    if raw_bytes > organization.max_raw_bytes:
        raise QueryPolicyError(
            f"Query result is {raw_bytes} bytes, above the allowed {organization.max_raw_bytes} bytes."
        )
    return QueryExecutionResult(
        columns=columns,
        rows=safe_rows,
        row_count=len(safe_rows),
        raw_bytes=raw_bytes,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def error_message(response, default_message):
    try:
        payload = response.json()
    except ValueError:
        return f"{default_message}: HTTP {response.status_code}"
    if isinstance(payload, dict):
        error = payload.get("error") or {}
        if isinstance(error, dict):
            message = error.get("message")
            if message:
                return message
        message = payload.get("message")
        code = payload.get("code")
        if message:
            return f"{code}: {message}" if code else message
    return f"{default_message}: HTTP {response.status_code}"


async def async_execute_bigquery_query(database_connection, sql, organization):
    config = provider_config(database_connection, "bigquery")
    started = time.monotonic()
    token = await bigquery_access_token(config)
    project_id = config["project_id"]
    dataset_id = config["dataset_id"]
    location = config.get("location") or None
    max_rows = organization.max_rows + 1
    timeout_ms = max(1, min(int(organization.query_timeout_seconds * 1000), 200000))

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    request_body = {
        "query": sql,
        "useLegacySql": False,
        "maxResults": max_rows,
        "timeoutMs": 1000,
        "defaultDataset": {
            "projectId": project_id,
            "datasetId": dataset_id,
        },
    }
    if location:
        request_body["location"] = location

    async with httpx.AsyncClient(timeout=timeout_ms / 1000 + 10) as client:
        response = await client.post(
            f"{BIGQUERY_API_ROOT}/projects/{project_id}/queries",
            headers=headers,
            json=request_body,
        )
        if response.status_code >= 400:
            raise QueryExecutionError(error_message(response, "BigQuery query failed"))
        payload = response.json()
        payload = await poll_bigquery_until_complete(
            client,
            payload,
            project_id,
            location,
            headers,
            max_rows,
            organization.query_timeout_seconds,
        )
        columns, rows = bigquery_result_rows(payload, max_rows)
        return build_query_result(columns, rows, organization, started)


async def bigquery_access_token(config):
    try:
        service_account = json.loads(config["service_account_json"])
    except (KeyError, json.JSONDecodeError) as exc:
        raise QueryExecutionError("BigQuery service account JSON is invalid.") from exc

    now = datetime.now(timezone.utc)
    assertion = jwt.encode(
        {
            "iss": service_account["client_email"],
            "scope": BIGQUERY_SCOPE,
            "aud": service_account.get("token_uri") or BIGQUERY_TOKEN_AUDIENCE,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=55)).timestamp()),
        },
        service_account["private_key"],
        algorithm="RS256",
    )
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            service_account.get("token_uri") or BIGQUERY_TOKEN_AUDIENCE,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
    if response.status_code >= 400:
        raise QueryExecutionError(error_message(response, "BigQuery authentication failed"))
    return response.json()["access_token"]


async def poll_bigquery_until_complete(
    client,
    payload,
    project_id,
    location,
    headers,
    max_rows,
    timeout_seconds,
):
    deadline = time.monotonic() + timeout_seconds
    while not payload.get("jobComplete"):
        if time.monotonic() >= deadline:
            raise QueryExecutionError("BigQuery query timed out.")
        job_reference = payload.get("jobReference") or {}
        job_id = job_reference.get("jobId")
        job_project_id = job_reference.get("projectId") or project_id
        if not job_id:
            raise QueryExecutionError("BigQuery did not return a job reference.")
        params = {"maxResults": max_rows, "timeoutMs": 1000}
        if location:
            params["location"] = location
        response = await client.get(
            f"{BIGQUERY_API_ROOT}/projects/{job_project_id}/queries/{job_id}",
            headers=headers,
            params=params,
        )
        if response.status_code >= 400:
            raise QueryExecutionError(error_message(response, "BigQuery results failed"))
        payload = response.json()
        await asyncio.sleep(0.25)
    if payload.get("errors"):
        first_error = payload["errors"][0]
        raise QueryExecutionError(first_error.get("message") or "BigQuery query failed.")
    return payload


def bigquery_result_rows(payload, max_rows):
    fields = (payload.get("schema") or {}).get("fields") or []
    columns = [field["name"] for field in fields]
    rows = []
    for row in payload.get("rows") or []:
        values = row.get("f") or []
        rows.append(
            {
                column: bigquery_cell_value(values[index].get("v"))
                if index < len(values)
                else None
                for index, column in enumerate(columns)
            }
        )
        if len(rows) >= max_rows:
            break
    return columns, rows


def bigquery_cell_value(value):
    if isinstance(value, list):
        return [bigquery_cell_value(item.get("v") if isinstance(item, dict) else item) for item in value]
    if isinstance(value, dict) and "f" in value:
        return [bigquery_cell_value(item.get("v")) for item in value["f"]]
    return value


async def async_execute_snowflake_query(database_connection, sql, organization):
    config = provider_config(database_connection, "snowflake")
    started = time.monotonic()
    token, token_type = snowflake_auth_token(config)
    account = config["account"]
    base_url = snowflake_base_url(account)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Snowflake-Authorization-Token-Type": token_type,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": SNOWFLAKE_API_USER_AGENT,
    }
    body = {
        "statement": sql,
        "timeout": int(organization.query_timeout_seconds),
        "database": config.get("database") or None,
        "schema": config.get("schema") or None,
        "warehouse": config.get("warehouse") or None,
        "role": config.get("role") or None,
    }
    body = {key: value for key, value in body.items() if value not in {"", None}}

    async with httpx.AsyncClient(timeout=organization.query_timeout_seconds + 10) as client:
        response = await client.post(
            f"{base_url}/api/v2/statements",
            headers=headers,
            params={"async": "true"},
            json=body,
        )
        if response.status_code not in {200, 202}:
            raise QueryExecutionError(error_message(response, "Snowflake query failed"))
        payload = response.json()
        payload = await poll_snowflake_until_complete(
            client,
            payload,
            base_url,
            headers,
            organization.query_timeout_seconds,
        )
        columns, rows = await snowflake_result_rows(
            client,
            payload,
            base_url,
            headers,
            organization.max_rows + 1,
        )
        return build_query_result(columns, rows, organization, started)


def snowflake_auth_token(config):
    auth_type = config.get("auth_type") or "programmatic_access_token"
    if auth_type == "oauth":
        return config.get("token") or "", "OAUTH"
    if auth_type == "programmatic_access_token":
        return config.get("token") or "", "PROGRAMMATIC_ACCESS_TOKEN"
    if auth_type == "key_pair":
        return snowflake_key_pair_jwt(config), "KEYPAIR_JWT"
    raise QueryExecutionError("Unsupported Snowflake authentication type.")


def snowflake_base_url(account):
    account = account.strip().removeprefix("https://").removeprefix("http://").rstrip("/")
    if account.endswith(".snowflakecomputing.com"):
        return f"https://{account}"
    return f"https://{account}.snowflakecomputing.com"


def snowflake_key_pair_jwt(config):
    private_key_text = config.get("private_key") or ""
    private_key = serialization.load_pem_private_key(
        private_key_text.encode("utf-8"),
        password=(config.get("private_key_passphrase") or "").encode("utf-8") or None,
    )
    public_key = private_key.public_key()
    public_der = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    fingerprint = base64.b64encode(hashlib.sha256(public_der).digest()).decode("ascii")
    account_identifier = snowflake_jwt_account_identifier(config["account"])
    username = config["username"].upper()
    qualified_username = f"{account_identifier}.{username}"
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "iss": f"{qualified_username}.SHA256:{fingerprint}",
            "sub": qualified_username,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=55)).timestamp()),
        },
        private_key,
        algorithm="RS256",
    )


def snowflake_jwt_account_identifier(account):
    account = account.strip().removeprefix("https://").removeprefix("http://")
    account = account.removesuffix(".snowflakecomputing.com").split("/")[0]
    if ".global" not in account and "." in account:
        account = account.split(".", 1)[0]
    return account.replace(".", "-").upper()


async def poll_snowflake_until_complete(client, payload, base_url, headers, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while "data" not in payload:
        if time.monotonic() >= deadline:
            raise QueryExecutionError("Snowflake query timed out.")
        statement_handle = payload.get("statementHandle")
        status_url = payload.get("statementStatusUrl")
        if not statement_handle and not status_url:
            raise QueryExecutionError(payload.get("message") or "Snowflake did not return a statement handle.")
        url = f"{base_url}{status_url}" if status_url else f"{base_url}/api/v2/statements/{statement_handle}"
        response = await client.get(url, headers=headers)
        if response.status_code == 422:
            raise QueryExecutionError(error_message(response, "Snowflake query failed"))
        if response.status_code not in {200, 202, 429}:
            raise QueryExecutionError(error_message(response, "Snowflake results failed"))
        payload = response.json()
        if response.status_code in {202, 429}:
            await asyncio.sleep(0.25)
    return payload


async def snowflake_result_rows(client, payload, base_url, headers, max_rows):
    metadata = payload.get("resultSetMetaData") or {}
    columns = [column["name"] for column in metadata.get("rowType") or []]
    rows = rows_from_snowflake_partition(columns, payload.get("data") or [])
    partition_info = metadata.get("partitionInfo") or []
    statement_handle = payload.get("statementHandle")
    for partition_index in range(1, len(partition_info)):
        if len(rows) >= max_rows:
            break
        if not statement_handle:
            break
        response = await client.get(
            f"{base_url}/api/v2/statements/{statement_handle}",
            headers=headers,
            params={"partition": partition_index},
        )
        if response.status_code >= 400:
            raise QueryExecutionError(error_message(response, "Snowflake partition fetch failed"))
        partition_payload = response.json()
        rows.extend(
            rows_from_snowflake_partition(
                columns,
                partition_payload.get("data") or [],
            )
        )
    return columns, rows[:max_rows]


def rows_from_snowflake_partition(columns, data):
    return [
        {
            column: values[index] if index < len(values) else None
            for index, column in enumerate(columns)
        }
        for values in data
    ]
