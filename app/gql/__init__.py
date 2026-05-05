"""ownfoil GraphQL package."""
from .schema import schema
from .view import graphql_dispatch

__all__ = ["schema", "graphql_dispatch"]
