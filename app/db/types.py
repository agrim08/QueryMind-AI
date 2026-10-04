"""pgvector column type without extra dependencies.

The `pgvector` Python package pulls in numpy and needs a codec registered on every
asyncpg connection. We only ever *write* vectors and *order by distance*, so a tiny
type is enough: values are sent as text ('[0.1,0.2,…]') and cast to halfvec in SQL.
Vectors are never read back into Python.
"""
from collections.abc import Sequence

from sqlalchemy import Float, Text, cast, literal_column
from sqlalchemy.types import UserDefinedType


class HalfVector(UserDefinedType):
    """`halfvec(dim)`: half-precision vector (2 bytes per dimension)."""

    cache_ok = True

    def __init__(self, dim: int):
        self.dim = dim

    def get_col_spec(self, **kw) -> str:
        return f"halfvec({self.dim})"

    def bind_processor(self, dialect):
        def process(value: Sequence[float] | None) -> str | None:
            if value is None:
                return None
            if len(value) != self.dim:
                raise ValueError(f"expected {self.dim} dimensions, got {len(value)}")
            return "[" + ",".join(repr(float(x)) for x in value) + "]"

        return process

    def bind_expression(self, bindvalue):
        # Bind as text so asyncpg needs no halfvec codec; Postgres does the cast.
        return cast(bindvalue, Text).op("::")(literal_column(f"halfvec({self.dim})"))

    class comparator_factory(UserDefinedType.Comparator):
        def cosine_distance(self, other: Sequence[float]):
            """`<=>`: 0 = same direction, 2 = opposite. Similarity = 1 - distance."""
            return self.op("<=>", return_type=Float)(other)
