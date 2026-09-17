"""Lossless integer storage: preserve invalid input until a CHECK can reject it."""
from sqlalchemy.types import UserDefinedType
from sqlalchemy.ext.compiler import compiles


class ExactInteger(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kw):
        return "NUMERIC"

    def bind_processor(self, dialect):
        def bind(value):
            if value is not None and type(value) is not int:
                raise ValueError("An exact Python integer is required")
            return value
        return bind

    def result_processor(self, dialect, coltype):
        return lambda value: None if value is None else int(value)


@compiles(ExactInteger, "sqlite")
def sqlite_exact_integer(type_, compiler, **kw):
    # BLOB affinity does not convert '100' to integer before CHECK evaluation.
    # Values themselves remain SQLite INTEGER, not serialized blobs.
    return "BLOB"
