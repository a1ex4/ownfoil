"""The GraphiQL docs pane is this API's only reference, so it has to be complete.

Strawberry does not publish Python docstrings as GraphQL descriptions - a type with a
perfectly good docstring and a bare `@strawberry.type` documents itself for readers of
the source and for nobody else. That gap is silent: the schema builds, the queries
work, and the docs pane is simply empty. This test is what makes it loud, and it is
why `gql/docs.py` exists.

Every element is its own case, so a failure names the exact field rather than a count.
"""
import pytest
from graphql import (
    GraphQLEnumType, GraphQLInputObjectType, GraphQLObjectType, GraphQLScalarType,
)

from gql.schema import schema


def _elements():
    """[(label, description), ...] for everything a client can see in the docs pane."""
    out = []
    for name, t in schema._schema.type_map.items():
        if name.startswith("__"):  # introspection types are the spec's, not ours
            continue
        if isinstance(t, (GraphQLObjectType, GraphQLInputObjectType)):
            out.append((f"type {name}", t.description))
            for fname, f in t.fields.items():
                out.append((f"{name}.{fname}", f.description))
                for aname, a in getattr(f, "args", {}).items():
                    out.append((f"{name}.{fname}({aname}:)", a.description))
        elif isinstance(t, GraphQLEnumType):
            out.append((f"enum {name}", t.description))
            for vname, v in t.values.items():
                out.append((f"{name}.{vname}", v.description))
        elif isinstance(t, GraphQLScalarType):
            out.append((f"scalar {name}", t.description))
    return out


ELEMENTS = _elements()


def test_the_schema_is_not_empty():
    """Guards the guard: a broken collector would make every case below vacuous."""
    assert len(ELEMENTS) > 200


@pytest.mark.parametrize("label,description",
                         ELEMENTS, ids=[label for label, _ in ELEMENTS])
def test_every_schema_element_is_documented(label, description):
    assert description, f"{label} has no description - it will be blank in GraphiQL"


@pytest.mark.parametrize("label,description",
                         ELEMENTS, ids=[label for label, _ in ELEMENTS])
def test_no_description_is_a_placeholder(label, description):
    """A description that only restates the field name teaches a reader nothing."""
    assert len(description.strip()) > 15, f"{label} description is too thin: {description!r}"
