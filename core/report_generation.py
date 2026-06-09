import json
import re

from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import SQLAlchemyError

from .ai_clients import AIMessage, AIProviderError, generate_text, stream_text
from .database_connections import redact_connection_error


class ReportGenerationError(Exception):
    pass


REPORT_ARTIFACT_BLOCK_RE = re.compile(
    r"```report_artifact\s*(.*?)```",
    re.DOTALL | re.IGNORECASE,
)

REPORT_SYSTEM_PROMPT = """
You are an AI report-building assistant inside a governed reporting product.

This is a chat experience. Reply conversationally by default.
Do not force a report update on every turn.

When the user asks you to create or revise the report sandbox, append exactly one
fenced artifact block at the end of your response:

```report_artifact
{
  "title": "short report title",
  "database_connection_id": 123,
  "primary_sql": "one read-only SQL query for the primary dataset",
  "html": "complete report HTML fragment"
}
```

Only include the artifact block when you are actually changing the report.

The report HTML must use JavaScript to load data with:
const data = await sr.dataset("primary");

Do not include database credentials. Keep the SQL compact and aggregate in SQL when possible.
Do not call external URLs from the HTML.
You may choose any available database connection shown in the context.

If the system gives you browser preview errors, fix the SQL and HTML and return
a revised report_artifact block. Do not ask the user to debug browser console
errors.
"""


def discover_schema(database_connection, table_limit=12, column_limit=16):
    connection_string = database_connection.get_connection_string()
    engine = None
    try:
        engine = create_engine(connection_string, pool_pre_ping=True)
        inspector = inspect(engine)
        table_names = inspector.get_table_names()[:table_limit]
        lines = []
        for table_name in table_names:
            columns = inspector.get_columns(table_name)[:column_limit]
            column_summary = ", ".join(
                f"{column['name']} {column['type']}" for column in columns
            )
            lines.append(f"- {table_name}: {column_summary}")
        return "\n".join(lines) if lines else "No tables found."
    except SQLAlchemyError as exc:
        raise ReportGenerationError(
            redact_connection_error(str(exc), connection_string)
        ) from exc
    finally:
        if engine is not None:
            engine.dispose()


def discover_available_databases(organization):
    lines = []
    for database_connection in organization.database_connections.filter(enabled=True):
        try:
            schema = discover_schema(database_connection)
        except ReportGenerationError as exc:
            schema = f"Schema unavailable: {exc}"
        lines.append(
            f"""
Database connection id: {database_connection.id}
Name: {database_connection.name}
Provider: {database_connection.get_provider_display()}
Schema:
{schema}
"""
        )
    return "\n".join(lines) if lines else "No enabled database connections are available."


def parse_report_artifact(content):
    artifact_match = REPORT_ARTIFACT_BLOCK_RE.search(content)
    if not artifact_match:
        return content.strip(), {}

    cleaned = artifact_match.group(1).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ReportGenerationError("AI report artifact was not valid JSON.") from exc
    visible_content = REPORT_ARTIFACT_BLOCK_RE.sub("", content).strip()
    artifact = {
        "title": parsed.get("title") or "Untitled report",
        "database_connection_id": parsed.get("database_connection_id"),
        "primary_sql": parsed.get("primary_sql") or "",
        "html": parsed.get("html") or "",
    }
    return visible_content or "Updated the report sandbox.", artifact


def build_report_chat_messages(report, user_prompt, history=None):
    if not report.ai_provider_key:
        raise ReportGenerationError("Add an AI provider key before generating reports.")

    database_context = discover_available_databases(report.organization)
    context = f"""
Available database tools:
{database_context}

Current report title:
{report.title}

Current selected database connection id:
{report.database_connection_id or "(none)"}

Current primary SQL:
{report.primary_sql or "(none)"}

Current HTML:
{report.html or "(none)"}
"""
    messages = [AIMessage(role="user", content=context)]
    history_messages = list(history or [])
    for message in history_messages:
        if message.role in {"user", "assistant"}:
            messages.append(AIMessage(role=message.role, content=message.content))
    if not history_messages or history_messages[-1].content != user_prompt:
        messages.append(AIMessage(role="user", content=user_prompt))
    return messages


def apply_report_artifact(report, artifact):
    if not artifact:
        return False
    report.title = artifact.get("title") or report.title
    report.primary_sql = artifact.get("primary_sql") or report.primary_sql
    report.html = artifact.get("html") or report.html
    database_connection_id = artifact.get("database_connection_id")
    if database_connection_id:
        connection = report.organization.database_connections.filter(
            id=database_connection_id,
            enabled=True,
        ).first()
        if connection:
            report.database_connection = connection
    report.save(
        update_fields=[
            "title",
            "primary_sql",
            "html",
            "database_connection",
            "updated_at",
        ]
    )
    return True


def generate_report_chat_response(report, user_prompt, history=None, user=None):
    messages = build_report_chat_messages(report, user_prompt, history)
    try:
        response = generate_text(
            report.ai_provider_key,
            messages,
            system_prompt=REPORT_SYSTEM_PROMPT,
            model_name=report.ai_model_name,
        )
    except AIProviderError as exc:
        raise ReportGenerationError(str(exc)) from exc
    visible_content, artifact = parse_report_artifact(response.content)
    apply_report_artifact(report, artifact)
    return visible_content, artifact


def stream_report_chat_response(report, user_prompt, history=None, user=None):
    messages = build_report_chat_messages(report, user_prompt, history)
    try:
        chunks = stream_text(
            report.ai_provider_key,
            messages,
            system_prompt=REPORT_SYSTEM_PROMPT,
            model_name=report.ai_model_name,
        )
        full_content = ""
        for chunk in chunks:
            full_content += chunk
            yield "delta", chunk
    except AIProviderError as exc:
        raise ReportGenerationError(str(exc)) from exc

    visible_content, artifact = parse_report_artifact(full_content)
    updated = apply_report_artifact(report, artifact)
    yield "done", {
        "content": visible_content,
        "artifact": artifact,
        "report_updated": updated,
        "title": report.title,
    }
