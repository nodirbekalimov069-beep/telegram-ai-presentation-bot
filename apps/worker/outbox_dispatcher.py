"""Reliable DB-outbox to Dramatiq bridge.

The database is authoritative. A crash after broker publication may publish the
same stable event again; downstream actors are therefore idempotent.
"""

import asyncio
import signal
from contextlib import suppress

from core.config import get_settings
from core.database.session import create_engine, create_session_factory
from core.outbox.models import OutboxEvent
from core.outbox.service import OutboxService, Publisher


def _payload_int(event: OutboxEvent, key: str, *, default: int | None = None) -> int:
    value = event.payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"outbox payload field {key!r} must be an integer")
    return int(value)


async def run_dispatcher(*, poll_interval: float = 0.5) -> None:
    from apps.worker.tasks import (
        generate_presentation,
        generate_priority_presentation,
        process_paid_order,
    )

    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)

    async def publish_generation(event: OutboxEvent) -> None:
        presentation_id = _payload_int(event, "presentation_id")
        priority = _payload_int(event, "priority", default=0)
        actor = generate_priority_presentation if priority >= 300 else generate_presentation
        actor.send(presentation_id)

    async def publish_paid(event: OutboxEvent) -> None:
        process_paid_order.send(_payload_int(event, "order_id"))

    publishers: dict[str, Publisher] = {
        "generation.requested": publish_generation,
        "payment.paid": publish_paid,
    }
    service = OutboxService()
    try:
        while not stop.is_set():
            processed = await service.dispatch_once(factory, publishers=publishers)
            if not processed:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=poll_interval)
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run_dispatcher())


if __name__ == "__main__":
    main()
