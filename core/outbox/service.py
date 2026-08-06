"""Transactional outbox enqueue and dispatcher service."""

import inspect
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.outbox.models import OutboxEvent

Publisher = Callable[[OutboxEvent], Awaitable[None] | None]


class OutboxService:
    def __init__(self, *, max_attempts: int = 10) -> None:
        self._max_attempts = max_attempts

    async def enqueue(
        self,
        session: AsyncSession,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> OutboxEvent:
        existing = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.idempotency_key == idempotency_key)
        )
        if existing is not None:
            if existing.event_type != event_type or existing.payload != payload:
                raise ValueError("outbox idempotency key conflict")
            return existing
        event = OutboxEvent(
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            idempotency_key=idempotency_key,
            payload=payload,
            status="PENDING",
        )
        session.add(event)
        await session.flush()
        return event

    async def dispatch_once(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        publishers: Mapping[str, Publisher],
    ) -> bool:
        async with session_factory() as session, session.begin():
            event = await session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.status == "PENDING",
                    OutboxEvent.available_at <= datetime.now(UTC),
                )
                .order_by(OutboxEvent.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if event is None:
                return False
            publisher = publishers.get(event.event_type)
            if publisher is None:
                event.status = "DEAD"
                event.last_error = f"no publisher registered for {event.event_type}"
                return True
            try:
                result = publisher(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                event.attempts += 1
                event.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                if event.attempts >= self._max_attempts:
                    event.status = "DEAD"
                else:
                    delay = min(300, 2 ** min(event.attempts, 8))
                    event.available_at = datetime.now(UTC) + timedelta(seconds=delay)
                return True
            event.status = "PUBLISHED"
            event.attempts += 1
            event.published_at = datetime.now(UTC)
            event.last_error = None
            return True
