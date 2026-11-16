"""Custom GraphQL scalars."""
from typing import NewType

import strawberry


BigInt = NewType("BigInt", int)

# GraphQL spec's Int is 32-bit, which truncates Switch file sizes (multi-GB).
SCALAR_MAP = {
    BigInt: strawberry.scalar(
        name="BigInt",
        serialize=int,
        parse_value=int,
        description=("A 64-bit signed integer, serialized as a JSON number. The GraphQL "
                     "spec's `Int` is 32-bit, which truncates Switch file sizes - so byte "
                     "counts use this instead."),
    ),
}
