"""Helpers that put this schema's documentation in front of clients.

Every type, field, argument, input and enum member carries a description: the
GraphiQL docs pane is the only reference this API has, and anything not written
there has to be discovered by reading a resolver.

Strawberry does **not** publish Python docstrings as GraphQL descriptions - a
documented class whose decorator is a bare `@strawberry.type` is documented for
readers of the source and nobody else. `described` and `described_field` close that
gap by passing the docstring through, so one piece of prose serves both audiences
instead of being written twice and drifting.
"""
import inspect

import strawberry


def desc(text: str, **kwargs):
    """A schema field with a description. `kwargs` passes through to
    `strawberry.field` - `default=` and `name=` are the ones that matter."""
    return strawberry.field(description=text, **kwargs)


def arg(description: str):
    """A described argument, used inside an `Annotated[...]` parameter type."""
    return strawberry.argument(description=description)


def described(decorator):
    """Wrap `strawberry.type` / `.input` / `.enum` so the class docstring becomes the
    GraphQL description. Used as `@described(strawberry.type)`."""
    def wrap(cls):
        return decorator(cls, description=inspect.getdoc(cls))
    return wrap


def described_field(resolver):
    """`@strawberry.field` for a resolver method, publishing its docstring."""
    return strawberry.field(resolver, description=inspect.getdoc(resolver))


def described_mutation(resolver):
    """`@strawberry.mutation` for a resolver method, publishing its docstring."""
    return strawberry.mutation(resolver, description=inspect.getdoc(resolver))
