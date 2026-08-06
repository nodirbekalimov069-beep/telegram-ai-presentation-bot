from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import core.database.models  # noqa: F401
from core.database.base import Base
from core.outbox.models import OutboxEvent
from core.outbox.service import OutboxService


@pytest.mark.asyncio
async def test_outbox_enqueue_is_idempotent_and_dispatches_once(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'outbox.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = OutboxService(max_attempts=3)

    async with factory() as session, session.begin():
        first = await service.enqueue(
            session,
            event_type="generation.requested",
            aggregate_type="presentation",
            aggregate_id="15",
            idempotency_key="generation:15",
            payload={"presentation_id": 15, "priority": 100},
        )
        second = await service.enqueue(
            session,
            event_type="generation.requested",
            aggregate_type="presentation",
            aggregate_id="15",
            idempotency_key="generation:15",
            payload={"presentation_id": 15, "priority": 100},
        )
        assert first.id == second.id

    published: list[dict[str, object]] = []

    async def publish(event: OutboxEvent) -> None:
        published.append(dict(event.payload))

    assert await service.dispatch_once(factory, publishers={"generation.requested": publish})
    assert not await service.dispatch_once(factory, publishers={"generation.requested": publish})

    async with factory() as session:
        count = await session.scalar(select(func.count(OutboxEvent.id)))
        event = await session.scalar(select(OutboxEvent))
    assert count == 1
    assert event is not None and event.status == "PUBLISHED"
    assert published == [{"presentation_id": 15, "priority": 100}]
    await engine.dispose()
