import core.database.models  # noqa: F401
from sqlalchemy import DateTime

from core.database.base import Base


def test_all_audit_timestamp_columns_are_timezone_aware() -> None:
    timestamp_columns = [
        column
        for table in Base.metadata.sorted_tables
        for column in table.c
        if column.name in {"created_at", "updated_at"}
    ]

    assert timestamp_columns
    assert all(
        isinstance(column.type, DateTime) and column.type.timezone is True
        for column in timestamp_columns
    )
