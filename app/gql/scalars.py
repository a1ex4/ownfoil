"""Custom GraphQL scalars."""
from typing import NewType

import strawberry


# GraphQL spec's Int is 32-bit, which truncates Switch file sizes (multi-GB).
BigInt = strawberry.scalar(
    NewType("BigInt", int),
    serialize=lambda v: int(v),
    parse_value=lambda v: int(v),
    description=("A 64-bit signed integer, serialized as a JSON number. The GraphQL "
                 "spec's `Int` is 32-bit, which truncates Switch file sizes - so byte "
                 "counts use this instead."),
)
