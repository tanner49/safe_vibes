from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .ai_provider_models import sync_provider_models
from .forms import (
    AIProviderKeyCreateForm,
    AIProviderKeyUpdateForm,
    CompanyUserCreateForm,
    OrganizationPolicyForm,
)
from .memberships import get_current_membership
from .models import AIProviderKey, Membership


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
    return render(
        request,
        "core/reports_placeholder.html",
        {
            "membership": membership,
            "organization": membership.organization if membership else None,
        },
    )


@login_required
def settings_home(request):
    membership = require_company_admin(request.user)
    organization = membership.organization
    memberships = organization.memberships.select_related("user").order_by("user__email")
    return render(
        request,
        "core/settings_home.html",
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
            return redirect("core:settings_home")
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
        return redirect("core:settings_home")

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
    return redirect("core:settings_home")


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
                "model_name": "gpt-5.4-mini",
            }
        )

    return render(
        request,
        "core/settings_ai_provider_add.html",
        {
            "form": form,
            "membership": membership,
            "organization": organization,
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
