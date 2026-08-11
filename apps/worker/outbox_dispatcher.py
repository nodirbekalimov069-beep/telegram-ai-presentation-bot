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
        cleanup_service_course_artifacts,
        cleanup_service_independent_artifacts,
        cleanup_service_essay_artifacts,
        cleanup_service_summary_artifacts,
        cleanup_service_quiz_artifacts,
        cleanup_service_write_text_artifacts,
        cleanup_service_rewrite_artifacts,
        generate_presentation,
        generate_priority_presentation,
        generate_service_course,
        generate_service_independent,
        generate_service_essay,
        generate_service_summary,
        generate_service_quiz,
        generate_service_write_text,
        generate_service_rewrite,
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

    async def publish_essay(event: OutboxEvent) -> None:
        generate_service_essay.send(_payload_int(event, "service_order_id"))

    async def publish_essay_cleanup(event: OutboxEvent) -> None:
        cleanup_service_essay_artifacts.send(_payload_int(event, "service_order_id"), event.id)

    async def publish_independent(event: OutboxEvent) -> None:
        generate_service_independent.send(_payload_int(event, "service_order_id"))

    async def publish_independent_cleanup(event: OutboxEvent) -> None:
        cleanup_service_independent_artifacts.send(
            _payload_int(event, "service_order_id"), event.id
        )

    async def publish_course(event: OutboxEvent) -> None:
        generate_service_course.send(_payload_int(event, "service_order_id"))

    async def publish_course_cleanup(event: OutboxEvent) -> None:
        cleanup_service_course_artifacts.send(_payload_int(event, "service_order_id"), event.id)

    async def publish_summary(event: OutboxEvent) -> None:
        generate_service_summary.send(_payload_int(event, "service_order_id"))

    async def publish_summary_cleanup(event: OutboxEvent) -> None:
        cleanup_service_summary_artifacts.send(_payload_int(event, "service_order_id"), event.id)

    async def publish_quiz(event: OutboxEvent) -> None:
        generate_service_quiz.send(_payload_int(event, "service_order_id"))

    async def publish_quiz_cleanup(event: OutboxEvent) -> None:
        cleanup_service_quiz_artifacts.send(_payload_int(event, "service_order_id"), event.id)

    async def publish_write_text(event: OutboxEvent) -> None:
        generate_service_write_text.send(_payload_int(event, "service_order_id"))

    async def publish_write_text_cleanup(event: OutboxEvent) -> None:
        cleanup_service_write_text_artifacts.send(_payload_int(event, "service_order_id"), event.id)

    async def publish_rewrite(event: OutboxEvent) -> None:
        generate_service_rewrite.send(_payload_int(event, "service_order_id"))

    async def publish_rewrite_cleanup(event: OutboxEvent) -> None:
        cleanup_service_rewrite_artifacts.send(_payload_int(event, "service_order_id"), event.id)

    publishers: dict[str, Publisher] = {
        "generation.requested": publish_generation,
        "payment.paid": publish_paid,
        "service.essay.requested": publish_essay,
        "service.essay.cleanup_requested": publish_essay_cleanup,
        "service.independent.requested": publish_independent,
        "service.independent.cleanup_requested": publish_independent_cleanup,
        "service.course.requested": publish_course,
        "service.course.cleanup_requested": publish_course_cleanup,
        "service.summary.requested": publish_summary,
        "service.summary.cleanup_requested": publish_summary_cleanup,
        "service.quiz.requested": publish_quiz,
        "service.quiz.cleanup_requested": publish_quiz_cleanup,
        "service.write.requested": publish_write_text,
        "service.write.cleanup_requested": publish_write_text_cleanup,
        "service.rewrite.requested": publish_rewrite,
        "service.rewrite.cleanup_requested": publish_rewrite_cleanup,
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
