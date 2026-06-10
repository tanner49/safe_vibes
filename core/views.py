from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.middleware.csrf import get_token
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.decorators.clickjacking import xframe_options_sameorigin
from asgiref.sync import sync_to_async
import json
import re

from .ai_provider_models import (
    get_curated_model_choices,
    get_curated_model_choices_by_provider,
    get_default_models_by_provider,
    get_default_model_for_provider,
    sync_provider_models,
)
from .forms import (
    AIProviderKeyCreateForm,
    AIProviderKeyUpdateForm,
    CompanyUserCreateForm,
    DatabaseConnectionCreateForm,
    DatabaseConnectionUpdateForm,
    OrganizationPolicyForm,
    ReportAIModelForm,
    ReportChatForm,
    ReportImportForm,
    ReportSharingForm,
    ReportUpdateForm,
)
from .memberships import get_current_membership
from .models import AIProviderKey, DatabaseConnection, Membership, Report, ReportChatMessage
from .database_connections import test_database_connection
from .query_execution import QueryExecutionError
from .report_cache import ReportCacheError, async_get_report_dataset, get_report_dataset
from .report_generation import (
    ReportGenerationError,
    apply_report_artifact,
    async_generate_report_chat_response,
    async_stream_report_chat_response,
    generate_report_chat_response,
    stream_report_chat_response,
)


MAX_BROWSER_PREVIEW_ERRORS = 4
SHARE_USER_SEARCH_LIMIT = 20
User = get_user_model()


REPORT_ARTIFACT_STREAM_RE = re.compile(
    r"```report_artifact\s*.*?```",
    re.DOTALL | re.IGNORECASE,
)
REPORT_ARTIFACT_MARKER_RE = re.compile(
    r"```report_artifact\b",
    re.IGNORECASE,
)


def require_company_admin(user):
    membership = get_current_membership(user)
    if not membership or not membership.is_company_admin:
        raise PermissionDenied
    return membership


def home(request):
    if request.user.is_authenticated:
        return redirect("core:dashboard")
    return render(request, "core/home.html")


@login_required
def dashboard(request):
    membership = get_current_membership(request.user)
    organization = membership.organization if membership else None
    return render(
        request,
        "core/dashboard.html",
        {
            "membership": membership,
            "organization": organization,
            "limits": {
                "query_timeout_seconds": settings.REPORT_QUERY_TIMEOUT_SECONDS,
                "cache_ttl_seconds": settings.REPORT_CACHE_TTL_SECONDS,
                "max_rows": settings.REPORT_MAX_ROWS,
                "max_raw_bytes": settings.REPORT_MAX_RAW_BYTES,
                "max_compressed_bytes": settings.REPORT_MAX_COMPRESSED_BYTES,
            },
        },
    )


@login_required
def reports_placeholder(request):
    membership = get_current_membership(request.user)
    if not membership:
        return render(
            request,
            "core/reports_placeholder.html",
            {
                "membership": membership,
                "organization": None,
            },
        )
    reports = visible_published_reports(request.user, membership).select_related(
        "database_connection",
        "owner",
    ).prefetch_related("shared_with").order_by("-updated_at")
    for report in reports:
        report.can_manage = can_edit_report(request.user, membership, report)
    return render(
        request,
        "core/reports.html",
        {
            "membership": membership,
            "organization": membership.organization,
            "reports": reports,
        },
    )


def get_default_ai_provider_key(organization):
    return organization.ai_provider_keys.order_by("provider", "name").first()


def get_default_ai_model_name(provider_key):
    if not provider_key:
        return ""
    if provider_key.model_name and provider_key.available_models.filter(
        provider_model_id=provider_key.model_name,
        allowed=True,
        available=True,
    ).exists():
        return provider_key.model_name
    provider_model = (
        provider_key.available_models.filter(allowed=True, available=True)
        .order_by("provider_model_id")
        .first()
    )
    if provider_model:
        return provider_model.provider_model_id
    return provider_key.model_name or get_default_model_for_provider(provider_key.provider)


def create_draft_report(organization, user):
    provider_key = get_default_ai_provider_key(organization)
    return Report.objects.create(
        organization=organization,
        owner=user,
        ai_provider_key=provider_key,
        ai_model_name=get_default_ai_model_name(provider_key),
        title="Draft report",
    )


def build_import_adaptation_prompt(sql, html, instructions=""):
    extra_instructions = instructions.strip() or "Preserve the intent and visual structure as much as possible."
    return f"""
I am importing an existing HTML report and SQL query into this product.

Please adapt the report so it works in the governed runtime:
- Use the SQL below as the primary dataset, revising it only if needed.
- Make the HTML load data with `const data = await sr.dataset("primary");`.
- Do not call external URLs unless they are already allowed by the product.
- Return a short note and exactly one report_artifact block with the corrected title, SQL, and HTML.

Additional instructions:
{extra_instructions}

Existing SQL:
```sql
{sql}
```

Existing HTML:
```html
{html}
```
"""


def get_provider_model_choices_by_key(organization):
    choices = {}
    for provider_key in organization.ai_provider_keys.all():
        models = list(
            provider_key.available_models.filter(
                allowed=True,
                available=True,
            ).values("provider_model_id", "display_name")
        )
        if not models:
            models = [
                {"provider_model_id": model_id, "display_name": display_name}
                for model_id, display_name in get_curated_model_choices(provider_key.provider)
            ]
        choices[str(provider_key.id)] = {
            "default_model": get_default_ai_model_name(provider_key),
            "models": models,
        }
    return choices


@login_required
def builder_home(request):
    membership = get_current_membership(request.user)
    if not membership:
        raise PermissionDenied
    drafts = (
        membership.organization.reports.filter(
            owner=request.user,
            status=Report.Status.DRAFT,
        )
        .select_related("database_connection", "ai_provider_key", "owner")
        .prefetch_related("chat_messages")
        .order_by("-updated_at", "-id")
    )
    for draft in drafts:
        draft.last_chat_message = next(
            reversed(list(draft.chat_messages.all())),
            None,
        )
    return render(
        request,
        "core/builder_home.html",
        {
            "drafts": drafts,
            "import_form": ReportImportForm(),
            "membership": membership,
            "organization": membership.organization,
        },
    )


@login_required
def builder_new(request):
    membership = get_current_membership(request.user)
    if not membership:
        raise PermissionDenied
    report = create_draft_report(membership.organization, request.user)
    return redirect("core:report_builder", report.id)


@login_required
@require_POST
async def builder_import(request):
    user = await request.auser()
    membership = await sync_to_async(get_current_membership, thread_sensitive=True)(user)
    if not membership:
        raise PermissionDenied
    post_data = await sync_to_async(lambda: request.POST.copy(), thread_sensitive=True)()
    form = ReportImportForm(post_data)
    if not form.is_valid():
        messages.error(request, "Paste both SQL and HTML to import an existing report.")
        return redirect("core:builder_home")

    report = await sync_to_async(create_draft_report, thread_sensitive=True)(
        membership.organization,
        user,
    )
    report.title = "Imported report draft"
    report.primary_sql = form.cleaned_data["primary_sql"]
    report.html = form.cleaned_data["html"]
    await sync_to_async(report.save, thread_sensitive=True)(
        update_fields=["title", "primary_sql", "html", "updated_at"]
    )

    prompt = build_import_adaptation_prompt(
        form.cleaned_data["primary_sql"],
        form.cleaned_data["html"],
        form.cleaned_data["instructions"],
    )
    await sync_to_async(ReportChatMessage.objects.create, thread_sensitive=True)(
        report=report,
        user=user,
        role=ReportChatMessage.Role.USER,
        content=prompt,
    )
    await async_apply_ai_draft_to_report(report, prompt, user=user)
    return redirect("core:report_builder", report.id)


def get_report_for_user(user, report_id):
    membership = get_current_membership(user)
    if not membership:
        raise PermissionDenied
    report = get_object_or_404(
        Report.objects.select_related(
            "organization",
            "database_connection",
            "ai_provider_key",
            "owner",
        ),
        id=report_id,
        organization=membership.organization,
    )
    if can_view_report(user, membership, report):
        return report, membership
    raise PermissionDenied


def get_editable_report_for_user(user, report_id):
    report, membership = get_report_for_user(user, report_id)
    if not can_edit_report(user, membership, report):
        raise PermissionDenied
    return report, membership


def can_edit_report(user, membership, report):
    return report.owner_id == user.id or membership.is_company_admin


def can_view_report(user, membership, report):
    if can_edit_report(user, membership, report):
        return True
    if report.status != Report.Status.PUBLISHED:
        return False
    if report.sharing_scope == Report.SharingScope.ORGANIZATION:
        return True
    return report.shared_with.filter(id=user.id).exists()


def visible_published_reports(user, membership):
    reports = membership.organization.reports.filter(status=Report.Status.PUBLISHED)
    if membership.is_company_admin:
        return reports
    return reports.filter(
        Q(owner=user)
        | Q(sharing_scope=Report.SharingScope.ORGANIZATION)
        | Q(shared_with=user)
    ).distinct()


def apply_ai_draft_to_report(report, prompt, user=None):
    try:
        assistant_content, artifact = generate_report_chat_response(
            report,
            prompt,
            history=report.chat_messages.order_by("created_at")[:12],
            user=user,
        )
        apply_report_artifact(report, artifact)
    except ReportGenerationError as exc:
        assistant_content = f"I could not update the report: {exc}"
        artifact = {}
    ReportChatMessage.objects.create(
        report=report,
        role=ReportChatMessage.Role.ASSISTANT,
        content=assistant_content,
        artifact=artifact,
    )
    return assistant_content


async def async_apply_ai_draft_to_report(report, prompt, user=None):
    history = await sync_to_async(
        lambda: list(report.chat_messages.order_by("created_at")[:12]),
        thread_sensitive=True,
    )()
    try:
        assistant_content, artifact = await async_generate_report_chat_response(
            report,
            prompt,
            history=history,
            user=user,
        )
        await sync_to_async(apply_report_artifact, thread_sensitive=True)(
            report,
            artifact,
        )
    except ReportGenerationError as exc:
        assistant_content = f"I could not update the report: {exc}"
        artifact = {}
    await sync_to_async(ReportChatMessage.objects.create, thread_sensitive=True)(
        report=report,
        role=ReportChatMessage.Role.ASSISTANT,
        content=assistant_content,
        artifact=artifact,
    )
    return assistant_content


def build_browser_error_message(error_payload, attempt_count):
    message = (error_payload.get("message") or "Unknown browser error").strip()
    context = (error_payload.get("context") or "browser preview").strip()
    stack = (error_payload.get("stack") or "").strip()
    lines = [
        f"Preview error {attempt_count}/{MAX_BROWSER_PREVIEW_ERRORS}: {message}",
        f"Context: {context}",
    ]
    if stack:
        lines.append("Stack:")
        lines.append(stack[:1200])
    return "\n".join(lines)


def build_browser_error_repair_prompt(error_payload):
    message = (error_payload.get("message") or "Unknown browser error").strip()
    context = (error_payload.get("context") or "browser preview").strip()
    stack = (error_payload.get("stack") or "").strip()
    source = (error_payload.get("source") or "").strip()
    lineno = error_payload.get("lineno") or ""
    colno = error_payload.get("colno") or ""
    return f"""
The browser preview for this report threw an error after rendering the current
HTML and/or fetching the current dataset.

Error context: {context}
Message: {message}
Source: {source}
Line: {lineno}
Column: {colno}
Stack:
{stack}

Fix the report so the browser preview works. The SQL endpoint errors are
surfaced to the browser through sr.dataset("primary"), so SQL and JavaScript may
both need changes. Return a short note and exactly one corrected report_artifact
block.
"""


def browser_preview_error_count_since_last_user_message(report):
    count = 0
    for message in report.chat_messages.order_by("-created_at", "-id"):
        if message.role == ReportChatMessage.Role.USER:
            break
        if message.artifact.get("preview_error"):
            count += 1
    return count


@login_required
def report_builder(request, report_id):
    report, membership = get_editable_report_for_user(request.user, report_id)
    if request.method == "POST":
        form = ReportUpdateForm(report.organization, request.POST, instance=report)
        if form.is_valid():
            form.save()
            messages.success(request, "Report saved.")
            return redirect("core:report_builder", report.id)
    else:
        form = ReportUpdateForm(report.organization, instance=report)
    return render(
        request,
        "core/report_builder.html",
        {
            "form": form,
            "model_form": ReportAIModelForm(report.organization, instance=report),
            "chat_form": ReportChatForm(),
            "membership": membership,
            "organization": report.organization,
            "provider_model_choices": get_provider_model_choices_by_key(report.organization),
            "report": report,
        },
    )


@login_required
@require_POST
def report_model_update(request, report_id):
    report, _membership = get_editable_report_for_user(request.user, report_id)
    form = ReportAIModelForm(report.organization, request.POST, instance=report)
    if form.is_valid():
        form.save()
        messages.success(request, "AI model updated.")
    else:
        messages.error(request, "Choose an allowed model for this report.")
    return redirect("core:report_builder", report.id)


@login_required
@require_POST
def report_publish(request, report_id):
    report, _membership = get_editable_report_for_user(request.user, report_id)
    if report.status != Report.Status.PUBLISHED:
        report.status = Report.Status.PUBLISHED
        report.save(update_fields=["status", "updated_at"])
        messages.success(request, f"{report.title} was published.")
    return redirect("core:reports_placeholder")


@login_required
@require_POST
def report_draft_delete(request, report_id):
    report, _membership = get_editable_report_for_user(request.user, report_id)
    if report.status != Report.Status.DRAFT:
        raise PermissionDenied
    title = report.title
    report.delete()
    messages.success(request, f"{title} was deleted.")
    return redirect("core:builder_home")


@login_required
@require_POST
def report_delete(request, report_id):
    report, _membership = get_editable_report_for_user(request.user, report_id)
    title = report.title
    report.delete()
    messages.success(request, f"{title} was deleted.")
    return redirect("core:reports_placeholder")


@login_required
def report_share(request, report_id):
    report, membership = get_editable_report_for_user(request.user, report_id)
    if request.method == "POST":
        form = ReportSharingForm(report.organization, request.POST, instance=report)
        if form.is_valid():
            form.save()
            messages.success(request, "Report sharing updated.")
            return redirect("core:reports_placeholder")
    else:
        form = ReportSharingForm(report.organization, instance=report)
    return render(
        request,
        "core/report_share.html",
        {
            "form": form,
            "membership": membership,
            "organization": report.organization,
            "report": report,
        },
    )


@login_required
def report_share_options(request, report_id):
    report, _membership = get_editable_report_for_user(request.user, report_id)
    query = (request.GET.get("q") or "").strip()
    users = User.objects.filter(
        organization_memberships__organization=report.organization,
    ).distinct()
    if query:
        users = users.filter(email__icontains=query)
    users = users.order_by("email")[:SHARE_USER_SEARCH_LIMIT]
    return JsonResponse(
        {
            "sharing_scope": report.sharing_scope,
            "shared_users": [
                {"id": user.id, "email": user.email}
                for user in report.shared_with.order_by("email")
            ],
            "results": [
                {"id": user.id, "email": user.email}
                for user in users
            ],
        }
    )


@login_required
@require_POST
async def report_chat_send(request, report_id):
    user = await request.auser()
    report, _membership = await sync_to_async(
        get_editable_report_for_user,
        thread_sensitive=True,
    )(user, report_id)
    post_data = await sync_to_async(lambda: request.POST.copy(), thread_sensitive=True)()
    form = ReportChatForm(post_data)
    if form.is_valid():
        prompt = form.cleaned_data["message"]
        await sync_to_async(ReportChatMessage.objects.create, thread_sensitive=True)(
            report=report,
            user=user,
            role=ReportChatMessage.Role.USER,
            content=prompt,
        )
        try:
            await async_apply_ai_draft_to_report(report, prompt, user=user)
        except ReportGenerationError as exc:
            await sync_to_async(ReportChatMessage.objects.create, thread_sensitive=True)(
                report=report,
                role=ReportChatMessage.Role.ASSISTANT,
                content=f"I could not update the report: {exc}",
            )
    response = await sync_to_async(render, thread_sensitive=True)(
        request,
        "core/partials/report_chat_messages.html",
        {"report": report},
    )
    response["HX-Trigger"] = "report-chat-updated"
    return response


def sse_event(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def strip_report_artifacts_for_stream(content):
    visible = REPORT_ARTIFACT_STREAM_RE.sub("", content)
    open_marker = REPORT_ARTIFACT_MARKER_RE.search(visible)
    if open_marker:
        visible = visible[: open_marker.start()]
    return visible


@login_required
@require_POST
async def report_chat_stream(request, report_id):
    user = await request.auser()
    report, _membership = await sync_to_async(
        get_editable_report_for_user,
        thread_sensitive=True,
    )(user, report_id)
    post_data = await sync_to_async(lambda: request.POST.copy(), thread_sensitive=True)()
    form = ReportChatForm(post_data)
    if not form.is_valid():
        return JsonResponse({"error": "Message is required."}, status=400)

    prompt = form.cleaned_data["message"]
    await sync_to_async(ReportChatMessage.objects.create, thread_sensitive=True)(
        report=report,
        user=user,
        role=ReportChatMessage.Role.USER,
        content=prompt,
    )

    async def event_stream():
        full_content = ""
        sent_visible_content = ""
        final_payload = None
        artifact_status_sent = False
        yield sse_event("status", {"message": "Preparing database context..."})
        try:
            history = await sync_to_async(
                lambda: list(report.chat_messages.order_by("created_at")[:12]),
                thread_sensitive=True,
            )()
            yield sse_event("status", {"message": "Asking the model..."})
            async for event, payload in async_stream_report_chat_response(
                report,
                prompt,
                history=history,
                user=user,
            ):
                if event == "delta":
                    full_content += payload
                    visible_content = strip_report_artifacts_for_stream(full_content)
                    delta = visible_content[len(sent_visible_content) :]
                    sent_visible_content = visible_content
                    if delta:
                        yield sse_event("delta", {"text": delta})
                    elif (
                        not artifact_status_sent
                        and REPORT_ARTIFACT_MARKER_RE.search(full_content)
                    ):
                        artifact_status_sent = True
                        yield sse_event("status", {"message": "Writing report changes..."})
                elif event == "done":
                    final_payload = payload
        except ReportGenerationError as exc:
            message = f"I could not update the report: {exc}"
            await sync_to_async(ReportChatMessage.objects.create, thread_sensitive=True)(
                report=report,
                role=ReportChatMessage.Role.ASSISTANT,
                content=message,
            )
            yield sse_event("error", {"message": message})
            return

        content = (final_payload or {}).get("content") or full_content.strip()
        artifact = (final_payload or {}).get("artifact") or {}
        report_updated = await sync_to_async(apply_report_artifact, thread_sensitive=True)(
            report,
            artifact,
        )
        if report_updated:
            await sync_to_async(report.refresh_from_db, thread_sensitive=True)()
        await sync_to_async(ReportChatMessage.objects.create, thread_sensitive=True)(
            report=report,
            role=ReportChatMessage.Role.ASSISTANT,
            content=content,
            artifact=artifact,
        )
        yield sse_event(
            "done",
            {
                "content": content,
                "artifact": artifact,
                "report_updated": report_updated
                or bool((final_payload or {}).get("report_updated")),
                "title": (final_payload or {}).get("title") or report.title,
            },
        )

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@login_required
@xframe_options_sameorigin
def report_preview(request, report_id):
    report, _membership = get_report_for_user(request.user, report_id)
    html = report.html or "<p>No report HTML yet.</p>"
    dataset_url = request.build_absolute_uri(
        reverse("core:report_primary_dataset", args=[report.id])
    )
    preview_error_url = request.build_absolute_uri(
        reverse("core:report_preview_error", args=[report.id])
    )
    csrf_token = get_token(request)
    sdk = f"""
<script>
(function () {{
  let reportingError = false;
  let lastErrorSignature = "";
  let lastErrorAt = 0;
  async function reportError(error, context) {{
    const payload = {{
      context,
      message: error && error.message ? error.message : String(error || "Unknown error"),
      stack: error && error.stack ? error.stack : "",
      source: error && error.filename ? error.filename : "",
      lineno: error && error.lineno ? error.lineno : null,
      colno: error && error.colno ? error.colno : null
    }};
    const signature = payload.context + "|" + payload.message + "|" + payload.stack;
    const now = Date.now();
    if (reportingError || (signature === lastErrorSignature && now - lastErrorAt < 2000)) return;
    reportingError = true;
    lastErrorSignature = signature;
    lastErrorAt = now;
    try {{
      const response = await fetch("{preview_error_url}", {{
        method: "POST",
        credentials: "same-origin",
        headers: {{
          "Content-Type": "application/json",
          "X-CSRFToken": "{csrf_token}"
        }},
        body: JSON.stringify(payload)
      }});
      const result = await response.json();
      if (window.parent && window.parent !== window) {{
        window.parent.postMessage({{ type: "safe_reports.preview_error", result }}, window.location.origin);
      }}
    }} catch (_reportingFailure) {{
    }} finally {{
      reportingError = false;
    }}
  }}

  window.sr = {{
    _datasets: {{}},
    reportError,
    async dataset(name) {{
      try {{
        if (name !== "primary") throw new Error("Unknown dataset: " + name);
        const response = await fetch("{dataset_url}", {{ credentials: "same-origin" }});
        if (!response.ok) throw new Error((await response.json()).error || "Dataset failed");
        const payload = await response.json();
        window.sr._datasets[name] = payload;
        return payload.rows || [];
      }} catch (error) {{
        await reportError(error, "sr.dataset(" + name + ")");
        throw error;
      }}
    }},
    datasetMeta(name) {{
      return window.sr._datasets[name] || null;
    }}
  }};

  window.addEventListener("error", function (event) {{
    reportError({{
      message: event.message,
      stack: event.error && event.error.stack ? event.error.stack : "",
      filename: event.filename,
      lineno: event.lineno,
      colno: event.colno
    }}, "window.error");
  }});
  window.addEventListener("unhandledrejection", function (event) {{
    const reason = event.reason || {{}};
    reportError(reason, "unhandledrejection");
  }});
}})();
</script>
"""
    return HttpResponse(sdk + html)


@login_required
@require_POST
async def report_preview_error(request, report_id):
    user = await request.auser()
    report, membership = await sync_to_async(
        get_editable_report_for_user,
        thread_sensitive=True,
    )(user, report_id)
    if report.status != Report.Status.DRAFT:
        return JsonResponse({"repaired": False, "message": "Preview error logged for a published report."})

    try:
        body = await sync_to_async(lambda: request.body, thread_sensitive=True)()
        error_payload = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        error_payload = {"message": "Preview reported an invalid error payload."}

    attempt_count = await sync_to_async(
        browser_preview_error_count_since_last_user_message,
        thread_sensitive=True,
    )(report) + 1
    error_message = build_browser_error_message(error_payload, attempt_count)
    await sync_to_async(ReportChatMessage.objects.create, thread_sensitive=True)(
        report=report,
        role=ReportChatMessage.Role.ASSISTANT,
        content=error_message,
        artifact={
            "preview_error": True,
            "browser_error": error_payload,
            "attempt": attempt_count,
        },
    )

    if attempt_count >= MAX_BROWSER_PREVIEW_ERRORS:
        give_up_message = (
            "The preview has failed four times in a row, so I stopped auto-repair "
            "to avoid burning tokens. Send a new chat message when you want me to try again."
        )
        await sync_to_async(ReportChatMessage.objects.create, thread_sensitive=True)(
            report=report,
            role=ReportChatMessage.Role.ASSISTANT,
            content=give_up_message,
            artifact={"preview_error_give_up": True},
        )
        return JsonResponse(
            {
                "repaired": False,
                "gave_up": True,
                "messages": [error_message, give_up_message],
            }
        )

    repair_prompt = build_browser_error_repair_prompt(error_payload)
    try:
        assistant_content = await async_apply_ai_draft_to_report(
            report,
            repair_prompt,
            user=user,
        )
    except ReportGenerationError as exc:
        assistant_content = f"I could not auto-repair the preview error: {exc}"
        await sync_to_async(ReportChatMessage.objects.create, thread_sensitive=True)(
            report=report,
            role=ReportChatMessage.Role.ASSISTANT,
            content=assistant_content,
        )
        return JsonResponse(
            {
                "repaired": False,
                "messages": [error_message, assistant_content],
            },
            status=500,
        )

    return JsonResponse(
        {
            "repaired": True,
            "messages": [error_message, assistant_content],
            "title": report.title,
        }
    )


@login_required
async def report_primary_dataset(request, report_id):
    user = await request.auser()
    report, _membership = await sync_to_async(
        get_report_for_user,
        thread_sensitive=True,
    )(user, report_id)
    if not report.primary_sql.strip():
        return JsonResponse({"error": "This report does not have a primary SQL query."}, status=400)
    if not report.database_connection:
        return JsonResponse({"error": "This report does not have a selected database connection."}, status=400)
    try:
        payload, cache_hit = await async_get_report_dataset(report, user=user)
    except (QueryExecutionError, ReportCacheError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    payload["cache_hit"] = cache_hit
    return JsonResponse(payload)


@login_required
def settings_home(request):
    membership = require_company_admin(request.user)
    organization = membership.organization
    return render(
        request,
        "core/settings_home.html",
        {
            "membership": membership,
            "organization": organization,
        },
    )


@login_required
def settings_users(request):
    membership = require_company_admin(request.user)
    organization = membership.organization
    memberships = organization.memberships.select_related("user").order_by("user__email")
    return render(
        request,
        "core/settings_users.html",
        {
            "membership": membership,
            "organization": organization,
            "memberships": memberships,
        },
    )


@login_required
def settings_report_limits(request):
    membership = require_company_admin(request.user)
    organization = membership.organization

    if request.method == "POST":
        form = OrganizationPolicyForm(request.POST, instance=organization)
        if form.is_valid():
            form.save()
            messages.success(request, "Organization settings updated.")
            return redirect("core:settings_home")
    else:
        form = OrganizationPolicyForm(instance=organization)

    return render(
        request,
        "core/settings_report_limits.html",
        {
            "form": form,
            "membership": membership,
            "organization": organization,
        },
    )


@login_required
def settings_user_add(request):
    membership = require_company_admin(request.user)
    organization = membership.organization
    if organization.sso_required:
        raise PermissionDenied

    if request.method == "POST":
        form = CompanyUserCreateForm(request.POST)
        if form.is_valid():
            user = form.save(organization)
            messages.success(request, f"{user.email} was added to the organization.")
            return redirect("core:settings_users")
    else:
        form = CompanyUserCreateForm()

    return render(
        request,
        "core/settings_user_add.html",
        {
            "form": form,
            "membership": membership,
            "organization": organization,
        },
    )


@login_required
@require_POST
def settings_user_remove(request, membership_id):
    current_membership = require_company_admin(request.user)
    organization = current_membership.organization
    if organization.sso_required:
        raise PermissionDenied

    target_membership = get_object_or_404(
        Membership.objects.select_related("user", "organization"),
        id=membership_id,
        organization=organization,
    )
    if target_membership.user_id == request.user.id:
        messages.error(request, "You cannot remove your own organization access.")
        return redirect("core:settings_users")

    email = target_membership.user.email
    with transaction.atomic():
        target_user = target_membership.user
        target_membership.delete()
        if (
            not target_user.is_staff
            and not target_user.is_superuser
            and not target_user.organization_memberships.exists()
        ):
            target_user.is_active = False
            target_user.save(update_fields=["is_active"])

    messages.success(request, f"{email} was removed from the organization.")
    return redirect("core:settings_users")


@login_required
def settings_database_connections(request):
    membership = require_company_admin(request.user)
    organization = membership.organization
    database_connections = organization.database_connections.order_by(
        "provider",
        "name",
    )
    return render(
        request,
        "core/settings_database_connections.html",
        {
            "membership": membership,
            "organization": organization,
            "database_connections": database_connections,
        },
    )


@login_required
def settings_database_connection_add(request):
    membership = require_company_admin(request.user)
    organization = membership.organization

    if request.method == "POST":
        form = DatabaseConnectionCreateForm(request.POST)
        if form.is_valid():
            database_connection = form.save(organization)
            messages.success(request, f"{database_connection.name} was added.")
            return redirect(
                "core:settings_database_connection_detail",
                database_connection.id,
            )
    else:
        form = DatabaseConnectionCreateForm(
            initial={
                "provider": DatabaseConnection.Provider.POSTGRES,
                "enabled": True,
            }
        )

    return render(
        request,
        "core/settings_database_connection_add.html",
        {
            "form": form,
            "membership": membership,
            "organization": organization,
        },
    )


@login_required
def settings_database_connection_detail(request, connection_id):
    membership = require_company_admin(request.user)
    organization = membership.organization
    database_connection = get_object_or_404(
        DatabaseConnection,
        id=connection_id,
        organization=organization,
    )

    if request.method == "POST":
        form = DatabaseConnectionUpdateForm(request.POST, instance=database_connection)
        if form.is_valid():
            database_connection = form.save()
            messages.success(
                request,
                f"{database_connection.name} settings were updated.",
            )
            return redirect(
                "core:settings_database_connection_detail",
                database_connection.id,
            )
    else:
        form = DatabaseConnectionUpdateForm(instance=database_connection)

    return render(
        request,
        "core/settings_database_connection_detail.html",
        {
            "form": form,
            "membership": membership,
            "organization": organization,
            "database_connection": database_connection,
        },
    )


@login_required
@require_POST
def settings_database_connection_test(request, connection_id):
    membership = require_company_admin(request.user)
    organization = membership.organization
    database_connection = get_object_or_404(
        DatabaseConnection,
        id=connection_id,
        organization=organization,
    )

    result = test_database_connection(database_connection)
    database_connection.last_tested_at = timezone.now()
    database_connection.last_test_succeeded = result.succeeded
    database_connection.last_test_error = "" if result.succeeded else result.message[:2000]
    database_connection.save(
        update_fields=[
            "last_tested_at",
            "last_test_succeeded",
            "last_test_error",
            "updated_at",
        ]
    )
    if result.succeeded:
        messages.success(request, result.message)
    else:
        messages.error(request, f"Connection test failed: {result.message}")
    return redirect("core:settings_database_connection_detail", database_connection.id)


@login_required
@require_POST
def settings_database_connection_delete(request, connection_id):
    membership = require_company_admin(request.user)
    organization = membership.organization
    database_connection = get_object_or_404(
        DatabaseConnection,
        id=connection_id,
        organization=organization,
    )
    name = database_connection.name
    database_connection.delete()
    messages.success(request, f"{name} was deleted.")
    return redirect("core:settings_database_connections")


@login_required
def settings_ai_providers(request):
    membership = require_company_admin(request.user)
    organization = membership.organization
    provider_keys = organization.ai_provider_keys.order_by("provider", "name")
    return render(
        request,
        "core/settings_ai_providers.html",
        {
            "membership": membership,
            "organization": organization,
            "provider_keys": provider_keys,
        },
    )


@login_required
def settings_ai_provider_add(request):
    membership = require_company_admin(request.user)
    organization = membership.organization

    if request.method == "POST":
        form = AIProviderKeyCreateForm(request.POST)
        if form.is_valid():
            provider_key = form.save(organization)
            count = sync_provider_models(
                provider_key,
                allowed_model_ids=form.cleaned_data["allowed_model_ids"],
            )
            messages.success(request, f"{provider_key.name} was added.")
            messages.success(
                request,
                f"Saved {count} curated model permissions for {provider_key.name}.",
            )
            return redirect("core:settings_ai_provider_detail", provider_key.id)
    else:
        form = AIProviderKeyCreateForm(
            initial={
                "name": "OpenAI",
                "provider": AIProviderKey.Provider.OPENAI,
                "model_name": get_default_model_for_provider(
                    AIProviderKey.Provider.OPENAI
                ),
            }
        )

    return render(
        request,
        "core/settings_ai_provider_add.html",
        {
            "form": form,
            "membership": membership,
            "organization": organization,
            "provider_model_choices": get_curated_model_choices_by_provider(),
            "provider_default_models": get_default_models_by_provider(),
        },
    )


@login_required
@require_POST
def settings_ai_provider_delete(request, provider_key_id):
    membership = require_company_admin(request.user)
    organization = membership.organization
    provider_key = get_object_or_404(
        AIProviderKey,
        id=provider_key_id,
        organization=organization,
    )
    name = provider_key.name
    provider_key.delete()
    messages.success(request, f"{name} was deleted.")
    return redirect("core:settings_ai_providers")


@login_required
def settings_ai_provider_detail(request, provider_key_id):
    membership = require_company_admin(request.user)
    organization = membership.organization
    provider_key = get_object_or_404(
        AIProviderKey,
        id=provider_key_id,
        organization=organization,
    )

    if request.method == "POST":
        form = AIProviderKeyUpdateForm(request.POST, instance=provider_key)
        if form.is_valid():
            provider_key = form.save()
            sync_provider_models(
                provider_key,
                allowed_model_ids=form.cleaned_data["allowed_model_ids"],
            )
            messages.success(request, f"{provider_key.name} settings were updated.")
            return redirect("core:settings_ai_provider_detail", provider_key.id)
    else:
        form = AIProviderKeyUpdateForm(instance=provider_key)

    provider_models = provider_key.available_models.order_by(
        "-available",
        "provider_model_id",
    )
    return render(
        request,
        "core/settings_ai_provider_detail.html",
        {
            "form": form,
            "membership": membership,
            "organization": organization,
            "provider_key": provider_key,
            "provider_models": provider_models,
        },
    )


@login_required
def settings_ai_provider_models(request, provider_key_id):
    return redirect("core:settings_ai_provider_detail", provider_key_id)
