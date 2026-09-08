"""Per-request GraphQL context."""
from dataclasses import dataclass
from typing import Optional

from flask_login import current_user

from auth import admin_account_created


@dataclass
class GraphQLContext:
    user: Optional[object]
    can_admin: bool
    can_shop: bool


def build_context(user=None) -> GraphQLContext:
    """Build the GraphQL context from the current Flask request.

    `user` is the caller an alternate auth path already resolved (Basic Auth); without
    one the Flask-Login session user is used. Permissions are read off the user object
    either way, so the two paths cannot drift.

    When no admin user has been provisioned (initial setup) auth is disabled;
    callers are treated as admin/shop to mirror the rest of the API.
    """
    if not admin_account_created():
        return GraphQLContext(user=None, can_admin=True, can_shop=True)
    if user is None and current_user.is_authenticated:
        user = current_user
    if user is None:
        return GraphQLContext(user=None, can_admin=False, can_shop=False)
    return GraphQLContext(
        user=user,
        can_admin=bool(user.has_admin_access()),
        can_shop=bool(user.has_shop_access()),
    )


def role_key(ctx: GraphQLContext) -> str:
    """Stable ETag key so caches don't bleed across roles; must cover every gated permission."""
    return f"admin={int(ctx.can_admin)},shop={int(ctx.can_shop)}"
