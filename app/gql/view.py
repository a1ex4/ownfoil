"""Flask view for the GraphQL endpoint with auth + ETag/304 handling."""
import json

from flask import Response, jsonify, make_response, request
from strawberry.flask.views import GraphQLView

from .cache import etag_for, world_hash
from .context import GraphQLContext, build_context, role_key
from .schema import schema


class OwnfoilGraphQLView(GraphQLView):
    schema = schema  # type: ignore[assignment]
    graphql_ide = "graphiql"  # interactive UI when browsing the endpoint

    def get_context(self, request, response) -> GraphQLContext:  # type: ignore[override]
        return build_context()


_view = OwnfoilGraphQLView.as_view("ownfoil_graphql", schema=schema)


def _parse_request():
    """Extract (query, variables, operation_name) from a GraphQL GET or POST."""
    if request.method == "GET":
        return (
            request.args.get("query"),
            _safe_load(request.args.get("variables")),
            request.args.get("operationName"),
        )
    if request.is_json:
        body = request.get_json(silent=True) or {}
        return (
            body.get("query"),
            body.get("variables"),
            body.get("operationName"),
        )
    return None, None, None


def _safe_load(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def graphql_dispatch():
    """Dispatch /api/graphql with auth gating, ETag handling, and a 304 fast path."""
    from auth import admin_account_created
    from flask_login import current_user

    if admin_account_created():
        if not current_user.is_authenticated:
            return Response("Unauthorized", status=401)
        if not (current_user.has_shop_access() or current_user.has_admin_access()):
            return Response("Forbidden", status=403)

    ctx = build_context()
    query, variables, operation_name = _parse_request()
    etag = etag_for(query, variables, operation_name, role_key(ctx), world_hash())
    etag_unquoted = etag.strip('"')

    if request.if_none_match and request.if_none_match.contains(etag_unquoted):
        resp = Response(status=304)
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "private, must-revalidate"
        return resp

    inner = _view()
    resp = make_response(inner)
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "private, must-revalidate"
    resp.headers["Vary"] = "Authorization, Cookie"
    return resp
