from pathlib import Path
from datetime import UTC, datetime, timedelta

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


@pytest.mark.asyncio
@pytest.mark.parametrize("service_name", ["essay", "independent"])
async def test_cleanup_outbox_never_dies_during_broker_outage(
    tmp_path: Path, service_name: str
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cleanup-outbox.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = OutboxService(max_attempts=2)

    async with factory() as session, session.begin():
        await service.enqueue(
            session,
            event_type=f"service.{service_name}.cleanup_requested",
            aggregate_type="service_order",
            aggregate_id="51",
            idempotency_key=f"service-{service_name}-cleanup:51",
            payload={"service_order_id": 51},
        )

    async def unavailable(_: OutboxEvent) -> None:
        raise ConnectionError("broker unavailable")

    for _ in range(5):
        async with factory() as session, session.begin():
            event = await session.scalar(select(OutboxEvent).with_for_update())
            assert event is not None
            event.available_at = datetime.now(UTC) - timedelta(seconds=1)
        assert await service.dispatch_once(
            factory,
            publishers={f"service.{service_name}.cleanup_requested": unavailable},
        )

    async with factory() as session:
        event = await session.scalar(select(OutboxEvent))
    assert event is not None
    assert event.status == "PENDING"
    assert event.attempts == 5
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("service_name", ["essay", "independent"])
async def test_cleanup_outbox_never_dies_when_publisher_is_not_registered(
    tmp_path: Path, service_name: str
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'missing-publisher.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = OutboxService(max_attempts=2)

    async with factory() as session, session.begin():
        await service.enqueue(
            session,
            event_type=f"service.{service_name}.cleanup_requested",
            aggregate_type="service_order",
            aggregate_id="52",
            idempotency_key=f"service-{service_name}-cleanup:52",
            payload={"service_order_id": 52},
        )

    for _ in range(4):
        async with factory() as session, session.begin():
            event = await session.scalar(select(OutboxEvent).with_for_update())
            assert event is not None
            event.available_at = datetime.now(UTC) - timedelta(seconds=1)
        assert await service.dispatch_once(factory, publishers={})

    async with factory() as session:
        event = await session.scalar(select(OutboxEvent))
    assert event is not None
    assert event.status == "PENDING"
    assert event.attempts == 4
    assert (
        event.last_error == f"no publisher registered for service.{service_name}.cleanup_requested"
    )
    available_at = event.available_at
    if available_at.tzinfo is None:
        available_at = available_at.replace(tzinfo=UTC)
    assert available_at > datetime.now(UTC)
    await engine.dispose()


@pytest.mark.asyncio
async def test_non_cleanup_event_dies_when_publisher_is_not_registered(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'dead-missing-publisher.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = OutboxService(max_attempts=2)
    async with factory() as session, session.begin():
        await service.enqueue(
            session,
            event_type="generation.requested",
            aggregate_type="presentation",
            aggregate_id="53",
            idempotency_key="generation:53",
            payload={"presentation_id": 53},
        )

    assert await service.dispatch_once(factory, publishers={})
    async with factory() as session:
        event = await session.scalar(select(OutboxEvent))
    assert event is not None
    assert event.status == "DEAD"
    assert event.last_error == "no publisher registered for generation.requested"
    await engine.dispose()


@pytest.mark.asyncio
async def test_non_cleanup_event_dies_after_publisher_exception_limit(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'dead-publisher-error.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = OutboxService(max_attempts=2)
    async with factory() as session, session.begin():
        await service.enqueue(
            session,
            event_type="generation.requested",
            aggregate_type="presentation",
            aggregate_id="54",
            idempotency_key="generation:54",
            payload={"presentation_id": 54},
        )

    async def unavailable(_: OutboxEvent) -> None:
        raise ConnectionError("broker unavailable")

    for _ in range(2):
        async with factory() as session, session.begin():
            event = await session.scalar(select(OutboxEvent).with_for_update())
            assert event is not None
            event.available_at = datetime.now(UTC) - timedelta(seconds=1)
        assert await service.dispatch_once(
            factory,
            publishers={"generation.requested": unavailable},
        )

    async with factory() as session:
        event = await session.scalar(select(OutboxEvent))
    assert event is not None
    assert event.status == "DEAD"
    assert event.attempts == 2
    await engine.dispose()
