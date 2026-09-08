"""Per-request GraphQL context."""
from dataclasses import dataclass
from typing import Optional

from auth import admin_account_created
from settings import get_settings


@dataclass
class GraphQLContext:
    user: Optional[object]
    can_admin: bool
    can_shop: bool


def build_context(user=None) -> GraphQLContext:
    """Build the GraphQL context for the caller the endpoint's gate resolved (None if anonymous)."""
    if not admin_account_created():
        return GraphQLContext(user=None, can_admin=True, can_shop=True)
    public = get_settings()['shop']['public']
    if user is None:
        return GraphQLContext(user=None, can_admin=False, can_shop=public)
    return GraphQLContext(
        user=user,
        can_admin=bool(user.has_admin_access()),
        can_shop=bool(user.has_shop_access()) or public,
    )


def role_key(ctx: GraphQLContext) -> str:
    """Stable ETag key so caches don't bleed across roles; must cover every gated permission."""
    return f"admin={int(ctx.can_admin)},shop={int(ctx.can_shop)}"
