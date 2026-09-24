"""Selection-set inspection helpers used by resolvers to skip unrequested work."""
from strawberry.types.nodes import SelectedField


def _expand(selections):
    """Yield concrete SelectedField nodes, flattening inline/spread fragments."""
    for s in selections or ():
        if isinstance(s, SelectedField):
            yield s
        else:
            yield from _expand(getattr(s, "selections", None))


class Selection:
    """Wraps the sub-selections at the current point in the query.

    Built from ``info.selected_fields`` via :meth:`from_info`, then walked with
    :meth:`has` (does this field appear?) and :meth:`child` (descend into it).
    Field names are GraphQL camelCase as written in the query.
    """

    __slots__ = ("_fields",)

    def __init__(self, fields):
        self._fields = list(fields or [])

    @classmethod
    def from_info(cls, info) -> "Selection":
        # info.selected_fields is the current field (one entry per alias). The
        # "selection set" we care about is the union of their sub-selections.
        inner = []
        for sf in info.selected_fields:
            inner.extend(_expand(getattr(sf, "selections", None)))
        return cls(inner)

    def has(self, name: str) -> bool:
        return any(f.name == name for f in self._fields)

    def child(self, *names: str) -> "Selection":
        # A field aliased more than once asks for its own sub-selections under each alias;
        # several names merge fields of one type, e.g. a group's kept and removed files.
        return Selection(s for f in self._fields if f.name in names for s in _expand(f.selections))
