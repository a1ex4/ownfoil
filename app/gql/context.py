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


def build_context() -> GraphQLContext:
    """Build the GraphQL context from the current Flask request.

    When no admin user has been provisioned (initial setup) auth is disabled;
    callers are treated as admin/shop to mirror the rest of the API.
    """
    if not admin_account_created():
        return GraphQLContext(user=None, can_admin=True, can_shop=True)
    if not current_user.is_authenticated:
        return GraphQLContext(user=None, can_admin=False, can_shop=False)
    return GraphQLContext(
        user=current_user,
        can_admin=bool(current_user.has_admin_access()),
        can_shop=bool(current_user.has_shop_access()),
    )


def role_key(ctx: GraphQLContext) -> str:
    """Stable string fed into the ETag so caches don't bleed across roles."""
    if ctx.can_admin:
        return "admin"
    if ctx.can_shop:
        return "shop"
    return "anon"
