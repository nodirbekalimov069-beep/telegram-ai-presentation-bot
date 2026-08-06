from sqlalchemy import Integer

from core.database.base import Base
import core.database.models  # noqa: F401


REQUIRED_TABLES = {
    "users",
    "plans",
    "orders",
    "payments",
    "payment_events",
    "presentations",
    "templates",
    "generation_jobs",
    "promo_codes",
    "ratings",
    "audit_logs",
    "bot_settings",
    "prompt_versions",
}


def test_database_metadata_contains_required_tables() -> None:
    assert set(Base.metadata.tables) >= REQUIRED_TABLES


def test_money_columns_are_integer_not_float() -> None:
    for table_name, column_name in [
        ("plans", "price_uzs"),
        ("orders", "amount"),
        ("payments", "amount"),
    ]:
        column = Base.metadata.tables[table_name].c[column_name]
        assert isinstance(column.type, Integer)


def test_generation_job_is_unique_per_presentation() -> None:
    table = Base.metadata.tables["generation_jobs"]
    assert table.c.presentation_id.unique is True
