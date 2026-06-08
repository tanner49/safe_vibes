def get_current_membership(user):
    if not user.is_authenticated:
        return None
    return (
        user.organization_memberships.select_related("organization")
        .order_by("organization__name")
        .first()
    )


def user_is_company_admin(user):
    membership = get_current_membership(user)
    return bool(membership and membership.is_company_admin)
