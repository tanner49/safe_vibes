from django.conf import settings

from .memberships import get_current_membership


def app_settings(request):
    membership = get_current_membership(request.user)
    return {
        "app_name": settings.APP_NAME,
        "current_membership": membership,
        "current_organization": membership.organization if membership else None,
        "is_company_admin": bool(membership and membership.is_company_admin),
    }
