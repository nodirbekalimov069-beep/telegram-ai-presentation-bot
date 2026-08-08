"""Presentation outline and queue application service."""

from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.outbox.service import OutboxService
from modules.ai.contracts import PresentationRequest
from modules.ai.providers.base import AIProvider
from modules.orders.domain import OrderStateMachine, OrderStatus
from modules.orders.models import Order
from modules.plans.models import Plan
from modules.presentations.models import GenerationJob, Presentation
from modules.users.models import FreeDemoClaim
from modules.users.service import FreeDemoService


class PresentationService:
    def __init__(self, *, ai_provider: AIProvider | None = None) -> None:
        self._ai_provider = ai_provider
        self._machine = OrderStateMachine()
        self._outbox = OutboxService()

    async def generate_outline(self, session: AsyncSession, *, order_id: int) -> Presentation:
        if self._ai_provider is None:
            raise RuntimeError("AI provider is required for outline generation")
        recovery_order_status: OrderStatus | None = None
        recovery_presentation_status: OrderStatus | None = None
        presentation_id: int | None = None
        request: PresentationRequest | None = None
        used_revision_instruction: str | None = None
        transition_started = False
        try:
            async with session.begin():
                order = await session.scalar(
                    select(Order)
                    .where(Order.id == order_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if order is None:
                    raise LookupError(f"order {order_id} not found")
                current = OrderStatus(order.status)
                settings = order.settings
                if (
                    current == OrderStatus.CREATED
                    and settings.get("_private_owner_test") is not True
                ):
                    claim = await session.scalar(
                        select(FreeDemoClaim).where(
                            FreeDemoClaim.order_id == order.id,
                            FreeDemoClaim.status == "RESERVED",
                        )
                    )
                    if claim is None:
                        raise PermissionError(
                            "outline generation requires a paid order or reserved Free Demo"
                        )
                recovery_order_status = current
                order.status = self._machine.transition(current, OrderStatus.OUTLINE_GENERATING)
                transition_started = True
                presentation = await session.scalar(
                    select(Presentation)
                    .where(Presentation.order_id == order.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if presentation is None:
                    recovery_presentation_status = current
                    presentation = Presentation(
                        order_id=order.id,
                        user_id=order.user_id,
                        title=order.topic,
                        topic=order.topic,
                        language=str(settings.get("language", "uz")),
                        direction=str(settings.get("direction", "Boshqa yo‘nalish")),
                        audience=str(settings.get("audience", "Umumiy auditoriya")),
                        slide_count=int(settings["slide_count"]),
                        design_style=str(settings.get("design_style", "ai")),
                        outline={},
                        content={},
                        status=OrderStatus.OUTLINE_GENERATING,
                    )
                    session.add(presentation)
                    await session.flush()
                else:
                    recovery_presentation_status = OrderStatus(presentation.status)
                    presentation.status = OrderStatus.OUTLINE_GENERATING
                language = presentation.language
                if language not in {"uz", "ru", "en"}:
                    raise ValueError("unsupported presentation language")
                request_language = cast(Literal["uz", "ru", "en"], language)
                knowledge_level = str(settings.get("knowledge_level", "beginner"))
                if knowledge_level not in {"beginner", "intermediate", "advanced"}:
                    raise ValueError("unsupported knowledge level")
                request_knowledge_level = cast(
                    Literal["beginner", "intermediate", "advanced"],
                    knowledge_level,
                )
                used_revision_instruction = (
                    str(settings["outline_edit_request"])
                    if settings.get("outline_edit_request")
                    else None
                )
                request = PresentationRequest(
                    topic=order.topic,
                    language=request_language,
                    direction=presentation.direction,
                    purpose=str(settings.get("purpose", "Ma’lumot berish")),
                    audience=presentation.audience,
                    knowledge_level=request_knowledge_level,
                    slide_count=presentation.slide_count,
                    design_style=presentation.design_style,
                    revision_instructions=used_revision_instruction,
                    existing_outline=(
                        dict(presentation.outline)
                        if used_revision_instruction is not None
                        else None
                    ),
                )
                presentation_id = presentation.id
        except Exception as exc:
            if (
                transition_started
                and recovery_order_status is not None
                and recovery_presentation_status is not None
            ):
                await self._recover_outline_failure(
                    session,
                    order_id=order_id,
                    presentation_id=presentation_id,
                    recovery_order_status=recovery_order_status,
                    recovery_presentation_status=recovery_presentation_status,
                    exc=exc,
                )
            raise

        assert request is not None
        assert presentation_id is not None
        assert recovery_order_status is not None
        assert recovery_presentation_status is not None

        try:
            outline = await self._ai_provider.generate_outline(request)
        except Exception as exc:
            await self._recover_outline_failure(
                session,
                order_id=order_id,
                presentation_id=presentation_id,
                recovery_order_status=recovery_order_status,
                recovery_presentation_status=recovery_presentation_status,
                exc=exc,
            )
            raise

        try:
            async with session.begin():
                order = await session.scalar(
                    select(Order)
                    .where(Order.id == order_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                presentation = await session.scalar(
                    select(Presentation)
                    .where(Presentation.id == presentation_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if order is None or presentation is None:
                    raise LookupError("order or presentation disappeared during outline generation")
                order.status = self._machine.transition(
                    OrderStatus(order.status),
                    OrderStatus.WAITING_OUTLINE_APPROVAL,
                )
                presentation.outline = outline.model_dump(mode="json")
                presentation.status = OrderStatus.WAITING_OUTLINE_APPROVAL
                presentation.error_message = None
                if (
                    used_revision_instruction is not None
                    and order.settings.get("outline_edit_request") == used_revision_instruction
                ):
                    updated_settings = dict(order.settings)
                    updated_settings.pop("outline_edit_request", None)
                    order.settings = updated_settings
                await FreeDemoService().consume_reserved_in_transaction(
                    session,
                    order_id=order_id,
                )
        except Exception as exc:
            await self._recover_outline_failure(
                session,
                order_id=order_id,
                presentation_id=presentation_id,
                recovery_order_status=recovery_order_status,
                recovery_presentation_status=recovery_presentation_status,
                exc=exc,
            )
            raise
        assert isinstance(presentation, Presentation)
        return presentation

    async def _recover_outline_failure(
        self,
        session: AsyncSession,
        *,
        order_id: int,
        presentation_id: int | None,
        recovery_order_status: OrderStatus,
        recovery_presentation_status: OrderStatus,
        exc: Exception,
    ) -> None:
        retryable = recovery_order_status in {
            OrderStatus.PAID,
            OrderStatus.WAITING_OUTLINE_APPROVAL,
        }
        reason = f"{type(exc).__name__}: outline generation failed"
        async with session.begin():
            order = await session.scalar(
                select(Order)
                .where(Order.id == order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if presentation_id is None:
                presentation = await session.scalar(
                    select(Presentation)
                    .where(Presentation.order_id == order_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            else:
                presentation = await session.scalar(
                    select(Presentation)
                    .where(Presentation.id == presentation_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            if order is not None and OrderStatus(order.status) in {
                recovery_order_status,
                OrderStatus.OUTLINE_GENERATING,
            }:
                order.status = recovery_order_status if retryable else OrderStatus.FAILED
            if presentation is not None and OrderStatus(presentation.status) in {
                recovery_presentation_status,
                OrderStatus.OUTLINE_GENERATING,
            }:
                presentation.status = (
                    recovery_presentation_status if retryable else OrderStatus.FAILED
                )
                presentation.error_message = reason
            if (
                not retryable
                and order is not None
                and OrderStatus(order.status) == OrderStatus.FAILED
            ):
                await FreeDemoService().release_system_failure_in_transaction(
                    session,
                    order_id=order_id,
                    reason=reason,
                )

    async def fail_outline_before_start(
        self,
        session: AsyncSession,
        *,
        order_id: int,
        reason: str,
    ) -> bool:
        """Fail and release an authorized CREATED order before provider startup."""
        async with session.begin():
            order = await session.scalar(
                select(Order)
                .where(Order.id == order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if order is None:
                return False
            current = OrderStatus(order.status)
            if current not in {OrderStatus.CREATED, OrderStatus.FAILED}:
                return False
            claim = await session.scalar(
                select(FreeDemoClaim)
                .where(
                    FreeDemoClaim.order_id == order_id,
                    FreeDemoClaim.status == "RESERVED",
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if current == OrderStatus.CREATED:
                if claim is None and order.settings.get("_private_owner_test") is not True:
                    return False
                order.status = OrderStatus.FAILED
            if claim is None:
                return current == OrderStatus.CREATED
            return await FreeDemoService().release_system_failure_in_transaction(
                session,
                order_id=order_id,
                reason=reason,
            )

    async def cancel_outline(self, session: AsyncSession, *, presentation_id: int) -> bool:
        async with session.begin():
            reference = await session.get(Presentation, presentation_id)
            if reference is None:
                raise LookupError(f"presentation {presentation_id} not found")
            order = await session.scalar(
                select(Order)
                .where(Order.id == reference.order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if order is None:
                raise LookupError("presentation order not found")
            presentation = await session.scalar(
                select(Presentation)
                .where(Presentation.id == presentation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if presentation is None:
                raise LookupError(f"presentation {presentation_id} not found")
            current = OrderStatus(order.status)
            if current not in {
                OrderStatus.WAITING_OUTLINE_APPROVAL,
                OrderStatus.QUEUED,
                OrderStatus.CANCELLED,
            }:
                raise ValueError("outline is no longer cancellable")
            job: GenerationJob | None = None
            if current == OrderStatus.QUEUED:
                job = await session.scalar(
                    select(GenerationJob)
                    .where(GenerationJob.presentation_id == presentation_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if job is None or job.status not in {"QUEUED", "CANCELLED"}:
                    raise ValueError("outline is no longer cancellable")
            cancelled = current != OrderStatus.CANCELLED
            if cancelled:
                order.status = self._machine.transition(current, OrderStatus.CANCELLED)
                presentation.status = OrderStatus.CANCELLED
                if job is not None:
                    job.status = "CANCELLED"
                    job.progress = 0
                    job.current_step = "cancelled"
                    job.lease_token = None
                    job.lease_expires_at = None
            order_id = order.id
            await FreeDemoService().release_cancelled_in_transaction(
                session,
                order_id=order_id,
            )
            return cancelled

    async def approve_outline(
        self,
        session: AsyncSession,
        *,
        presentation_id: int,
        progress_chat_id: int | None = None,
        progress_message_id: int | None = None,
    ) -> GenerationJob:
        async with session.begin():
            reference = await session.get(Presentation, presentation_id)
            if reference is None:
                raise LookupError(f"presentation {presentation_id} not found")
            order = await session.scalar(
                select(Order)
                .where(Order.id == reference.order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if order is None:
                raise LookupError("presentation order not found")
            presentation = await session.scalar(
                select(Presentation)
                .where(Presentation.id == presentation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if presentation is None:
                raise LookupError(f"presentation {presentation_id} not found")
            job = await session.scalar(
                select(GenerationJob).where(GenerationJob.presentation_id == presentation.id)
            )
            if job is not None:
                return job
            current = OrderStatus(order.status)
            if current != OrderStatus.QUEUED:
                order.status = self._machine.transition(
                    current,
                    OrderStatus.QUEUED,
                )
            presentation.status = OrderStatus.QUEUED
            plan = await session.get(Plan, order.plan_id)
            job = GenerationJob(
                presentation_id=presentation.id,
                status="QUEUED",
                progress=0,
                current_step="queued",
                progress_chat_id=progress_chat_id,
                progress_message_id=progress_message_id,
                retries=0,
                priority=plan.priority if plan is not None else 0,
            )
            session.add(job)
            await session.flush()
            await self._outbox.enqueue(
                session,
                event_type="generation.requested",
                aggregate_type="presentation",
                aggregate_id=str(presentation.id),
                idempotency_key=f"generation:{presentation.id}",
                payload={
                    "presentation_id": presentation.id,
                    "priority": job.priority,
                },
            )
        return job
