"""Idempotent background presentation generation pipeline."""

import logging
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modules.ai.contracts import ContentGenerationRequest
from modules.ai.providers.base import AIProvider
from modules.files.storage import Storage
from modules.orders.domain import OrderStateMachine, OrderStatus
from modules.orders.models import Order
from modules.plans.models import Plan
from modules.presentations.engine import (
    NodePresentationRenderer,
    build_engine_payload,
    validate_pptx,
)
from modules.presentations.models import GenerationAttempt, GenerationJob, Presentation
from modules.presentations.pdf import validate_pdf
from modules.users.models import User
from modules.users.service import FreeDemoService

logger = logging.getLogger(__name__)
ProgressCallback = Callable[[int, str], Awaitable[None]]


class PdfConverter(Protocol):
    async def convert(self, pptx_path: Path, output_pdf: Path) -> Path: ...


class GenerationQualityError(RuntimeError):
    pass


class GenerationLeaseLost(RuntimeError):
    """Raised when a stale generation attempt loses its fencing token."""


class GenerationLeaseActive(RuntimeError):
    """Raised when another worker still owns a non-expired generation lease."""

    def __init__(self, *, retry_after_ms: int) -> None:
        super().__init__("generation is already owned by an active worker lease")
        self.retry_after_ms = max(1_000, retry_after_ms)


@dataclass(frozen=True, slots=True)
class GenerationResult:
    presentation_id: int
    completed: bool
    pptx_storage_key: str
    pdf_storage_key: str


async def mark_terminal_generation_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    presentation_id: int,
    error: Exception,
) -> bool:
    """Persist terminal failure when setup exits before normal attempt recovery."""

    detail = f"{type(error).__name__}: terminal generation failure"
    now = datetime.now(UTC)
    machine = OrderStateMachine()
    async with session_factory() as session, session.begin():
        reference = await session.get(Presentation, presentation_id)
        if reference is None:
            return False
        order = await session.scalar(
            select(Order).where(Order.id == reference.order_id).with_for_update()
        )
        presentation = await session.scalar(
            select(Presentation).where(Presentation.id == presentation_id).with_for_update()
        )
        if order is None or presentation is None:
            return False
        job = await session.scalar(
            select(GenerationJob)
            .where(GenerationJob.presentation_id == presentation_id)
            .with_for_update()
        )
        if job is None:
            return False
        current = OrderStatus(order.status)
        if current == OrderStatus.CANCELLED:
            return True
        if presentation.status == OrderStatus.COMPLETED:
            return False
        lease_expires_at = job.lease_expires_at
        if lease_expires_at is not None and lease_expires_at.tzinfo is None:
            lease_expires_at = lease_expires_at.replace(tzinfo=UTC)
        if job.status == "RUNNING" and lease_expires_at is not None and lease_expires_at > now:
            return False
        if current == OrderStatus.FAILED and job.status == "FAILED":
            return True
        if current == OrderStatus.QUEUED:
            current = machine.transition(current, OrderStatus.GENERATING)
        if current != OrderStatus.FAILED:
            if not machine.can_transition(current, OrderStatus.FAILED):
                return False
            current = machine.transition(current, OrderStatus.FAILED)
        order.status = current
        presentation.status = OrderStatus.FAILED
        presentation.error_message = detail
        job.status = "FAILED"
        job.retries += 1
        job.error_message = detail
        job.current_step = "failed"
        job.lease_token = None
        job.lease_expires_at = None
        job.heartbeat_at = now
        attempt = await session.scalar(
            select(GenerationAttempt)
            .where(
                GenerationAttempt.generation_job_id == job.id,
                GenerationAttempt.status == "RUNNING",
            )
            .order_by(GenerationAttempt.id.desc())
            .with_for_update()
        )
        if attempt is not None:
            attempt.status = "FAILED"
            attempt.error_message = detail
            attempt.finished_at = now
            attempt.heartbeat_at = now
        return True


class GenerationPipeline:
    def __init__(
        self,
        *,
        renderer: NodePresentationRenderer,
        pdf_converter: PdfConverter,
        storage: Storage,
        work_root: Path,
        content_provider: AIProvider | None = None,
    ) -> None:
        self._renderer = renderer
        self._pdf_converter = pdf_converter
        self._storage = storage
        self._content_provider = content_provider
        self._work_root = work_root.expanduser().resolve()
        self._work_root.mkdir(parents=True, exist_ok=True)
        self._machine = OrderStateMachine()

    async def run(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        presentation_id: int,
        progress_callback: ProgressCallback | None = None,
        terminal_failure: bool = True,
    ) -> GenerationResult:
        try:
            return await self._run_once(
                session_factory,
                presentation_id=presentation_id,
                progress_callback=progress_callback,
                terminal_failure=terminal_failure,
            )
        except (GenerationLeaseActive, GenerationLeaseLost):
            raise
        except Exception as error:
            if terminal_failure:
                await mark_terminal_generation_failure(
                    session_factory,
                    presentation_id=presentation_id,
                    error=error,
                )
            raise

    async def _run_once(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        presentation_id: int,
        progress_callback: ProgressCallback | None = None,
        terminal_failure: bool = True,
    ) -> GenerationResult:
        lease_token = uuid4().hex
        async with session_factory() as session, session.begin():
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
            if (
                presentation.status == OrderStatus.COMPLETED
                and presentation.pptx_storage_key
                and presentation.pdf_storage_key
            ):
                return GenerationResult(
                    presentation_id=presentation.id,
                    completed=True,
                    pptx_storage_key=presentation.pptx_storage_key,
                    pdf_storage_key=presentation.pdf_storage_key,
                )
            current = OrderStatus(order.status)
            if current == OrderStatus.CANCELLED:
                return GenerationResult(
                    presentation_id=presentation.id,
                    completed=False,
                    pptx_storage_key="",
                    pdf_storage_key="",
                )
            job = await session.scalar(
                select(GenerationJob)
                .where(GenerationJob.presentation_id == presentation.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if job is None:
                raise LookupError("generation job not found")
            now = datetime.now(UTC)
            lease_expires_at = job.lease_expires_at
            if lease_expires_at is not None and lease_expires_at.tzinfo is None:
                lease_expires_at = lease_expires_at.replace(tzinfo=UTC)
            if job.status == "RUNNING" and lease_expires_at is not None and lease_expires_at > now:
                raise GenerationLeaseActive(
                    retry_after_ms=int((lease_expires_at - now).total_seconds() * 1_000) + 1_000
                )
            if current == OrderStatus.FAILED:
                current = self._machine.transition(current, OrderStatus.QUEUED)
                order.status = current
            if current != OrderStatus.GENERATING:
                order.status = self._machine.transition(current, OrderStatus.GENERATING)
            presentation.status = OrderStatus.GENERATING
            presentation.generation_started_at = now
            presentation.error_message = None
            job.status = "RUNNING"
            job.progress = 10
            job.current_step = "content_and_design"
            job.error_message = None
            job.lease_token = lease_token
            job.heartbeat_at = now
            job.lease_expires_at = now + timedelta(minutes=15)
            session.add(
                GenerationAttempt(
                    generation_job_id=job.id,
                    fencing_token=lease_token,
                    status="RUNNING",
                    started_at=now,
                    heartbeat_at=now,
                )
            )
            plan = await session.get(Plan, order.plan_id)
            if plan is None:
                raise LookupError("order plan not found")
            await FreeDemoService().consume_reserved_in_transaction(
                session,
                order_id=order.id,
            )
            title = presentation.title
            language = presentation.language
            direction = presentation.direction
            design_style = presentation.design_style
            plan_code = plan.code
            outline = dict(presentation.outline)
            content = dict(presentation.content or {})
            order_settings = dict(order.settings or {})
            expected_slides = presentation.slide_count
            user_id = presentation.user_id
            order_id = order.id

        await self._notify_progress(progress_callback, 10, "content_and_design")
        try:
            if not content and self._content_provider is not None:
                await self._notify_progress(progress_callback, 20, "generating_content")
                request_language = language if language in {"uz", "ru", "en"} else "uz"
                raw_level = str(order_settings.get("knowledge_level", "intermediate"))
                knowledge_level = (
                    raw_level
                    if raw_level in {"beginner", "intermediate", "advanced"}
                    else "intermediate"
                )
                generated = await self._content_provider.generate_content(
                    ContentGenerationRequest.model_validate(
                        {
                            "title": title,
                            "language": request_language,
                            "direction": direction,
                            "purpose": str(order_settings.get("purpose") or "taqdimot uchun"),
                            "audience": str(order_settings.get("audience") or "umumiy auditoriya"),
                            "knowledge_level": knowledge_level,
                            "additional_requirements": (
                                str(order_settings["additional_requirements"]).strip()
                                if order_settings.get("additional_requirements")
                                else None
                            ),
                            "outline": outline,
                        }
                    )
                )
                content = generated.model_dump(mode="json")
                await self._store_generated_content(
                    session_factory,
                    presentation_id=presentation_id,
                    lease_token=lease_token,
                    content=content,
                )
            payload = build_engine_payload(
                title=title,
                language=language,
                direction=direction,
                design_style=design_style,
                plan_code=plan_code,
                outline=outline,
                content=content,
            )
            await self._update_job_progress(
                session_factory,
                presentation_id=presentation_id,
                lease_token=lease_token,
                progress=30,
                current_step="rendering_pptx",
            )
            await self._notify_progress(progress_callback, 30, "rendering_pptx")
            with tempfile.TemporaryDirectory(
                prefix=f"presentation-{presentation_id}-",
                dir=self._work_root,
            ) as directory:
                workdir = Path(directory)
                pptx_path = await self._renderer.render(payload, workdir / "presentation.pptx")
                pptx_report = validate_pptx(pptx_path, expected_slides=expected_slides)
                if not pptx_report.is_valid:
                    raise GenerationQualityError("; ".join(pptx_report.issues))

                await self._mark_quality_check(
                    session_factory,
                    presentation_id=presentation_id,
                    order_id=order_id,
                    lease_token=lease_token,
                )
                await self._notify_progress(progress_callback, 60, "converting_pdf")
                pdf_path = await self._pdf_converter.convert(
                    pptx_path,
                    workdir / "presentation.pdf",
                )
                pdf_report = validate_pdf(pdf_path, expected_pages=expected_slides)
                if not pdf_report.is_valid:
                    raise GenerationQualityError("; ".join(pdf_report.issues))

                pptx_key = await self._storage.put_file(
                    pptx_path,
                    user_id=user_id,
                    presentation_id=presentation_id,
                    artifact_type="pptx",
                )
                pdf_key = await self._storage.put_file(
                    pdf_path,
                    user_id=user_id,
                    presentation_id=presentation_id,
                    artifact_type="pdf",
                )

            async with session_factory() as session, session.begin():
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
                if presentation is None or order is None:
                    raise LookupError("generation aggregate disappeared")
                job = await session.scalar(
                    select(GenerationJob)
                    .where(GenerationJob.presentation_id == presentation_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if job is None or job.lease_token != lease_token:
                    raise GenerationLeaseLost("generation fencing token is no longer current")
                if presentation.status != OrderStatus.COMPLETED:
                    order.status = self._machine.transition(
                        OrderStatus(order.status),
                        OrderStatus.COMPLETED,
                    )
                    presentation.status = OrderStatus.COMPLETED
                    presentation.pptx_storage_key = pptx_key
                    presentation.pdf_storage_key = pdf_key
                    presentation.delivery_status = "PENDING"
                    presentation.delivery_error = None
                    presentation.generation_finished_at = datetime.now(UTC)
                    user = await session.get(User, user_id, with_for_update=True)
                    if user is not None:
                        user.presentations_count += 1
                job.status = "ARTIFACTS_READY"
                job.progress = 90
                job.current_step = "delivery_pending"
                job.lease_token = None
                job.lease_expires_at = None
                job.heartbeat_at = datetime.now(UTC)
                attempt = await session.scalar(
                    select(GenerationAttempt).where(GenerationAttempt.fencing_token == lease_token)
                )
                if attempt is not None:
                    attempt.status = "COMPLETED"
                    attempt.finished_at = datetime.now(UTC)
                    attempt.heartbeat_at = datetime.now(UTC)
            await self._notify_progress(progress_callback, 90, "delivery_pending")
            return GenerationResult(
                presentation_id=presentation_id,
                completed=True,
                pptx_storage_key=pptx_key,
                pdf_storage_key=pdf_key,
            )
        except GenerationLeaseLost:
            raise
        except Exception as exc:
            await self._record_failure(
                session_factory,
                presentation_id,
                order_id,
                lease_token,
                exc,
                terminal_failure=terminal_failure,
            )
            raise

    @staticmethod
    async def _notify_progress(
        progress_callback: ProgressCallback | None,
        progress: int,
        current_step: str,
    ) -> None:
        if progress_callback is None:
            return
        try:
            await progress_callback(progress, current_step)
        except Exception:
            logger.warning(
                "generation progress callback failed progress=%s current_step=%s",
                progress,
                current_step,
                exc_info=True,
            )

    async def _update_job_progress(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        presentation_id: int,
        lease_token: str,
        progress: int,
        current_step: str,
    ) -> None:
        async with session_factory() as session, session.begin():
            job = await session.scalar(
                select(GenerationJob)
                .where(GenerationJob.presentation_id == presentation_id)
                .with_for_update()
            )
            if job is None or job.lease_token != lease_token:
                raise GenerationLeaseLost("generation lease expired during progress update")
            now = datetime.now(UTC)
            job.progress = progress
            job.current_step = current_step
            job.heartbeat_at = now
            job.lease_expires_at = now + timedelta(minutes=15)

    async def _store_generated_content(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        presentation_id: int,
        lease_token: str,
        content: dict[str, object],
    ) -> None:
        async with session_factory() as session, session.begin():
            presentation = await session.get(Presentation, presentation_id, with_for_update=True)
            job = await session.scalar(
                select(GenerationJob)
                .where(GenerationJob.presentation_id == presentation_id)
                .with_for_update()
            )
            if presentation is None or job is None or job.lease_token != lease_token:
                raise GenerationLeaseLost("generation lease expired during content generation")
            presentation.content = content
            now = datetime.now(UTC)
            job.progress = 25
            job.current_step = "content_ready"
            job.heartbeat_at = now
            job.lease_expires_at = now + timedelta(minutes=15)

    async def _mark_quality_check(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        presentation_id: int,
        order_id: int,
        lease_token: str,
    ) -> None:
        async with session_factory() as session, session.begin():
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
            if presentation is None or order is None:
                raise LookupError("generation aggregate disappeared")
            order.status = self._machine.transition(
                OrderStatus(order.status),
                OrderStatus.QUALITY_CHECK,
            )
            presentation.status = OrderStatus.QUALITY_CHECK
            job = await session.scalar(
                select(GenerationJob)
                .where(GenerationJob.presentation_id == presentation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if job is None or job.lease_token != lease_token:
                raise GenerationLeaseLost("generation lease expired during rendering")
            now = datetime.now(UTC)
            job.progress = 60
            job.current_step = "converting_pdf"
            job.heartbeat_at = now
            job.lease_expires_at = now + timedelta(minutes=15)
            attempt = await session.scalar(
                select(GenerationAttempt).where(GenerationAttempt.fencing_token == lease_token)
            )
            if attempt is not None:
                attempt.heartbeat_at = now

    async def _record_failure(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        presentation_id: int,
        order_id: int,
        lease_token: str,
        error: Exception,
        *,
        terminal_failure: bool,
    ) -> None:
        detail = f"{type(error).__name__}: {error}"[:2000]
        async with session_factory() as session, session.begin():
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
            job = await session.scalar(
                select(GenerationJob)
                .where(GenerationJob.presentation_id == presentation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if job is None or job.lease_token != lease_token:
                return
            if order is not None and order.status != OrderStatus.COMPLETED:
                current = OrderStatus(order.status)
                if current == OrderStatus.FAILED and not terminal_failure:
                    order.status = self._machine.transition(current, OrderStatus.QUEUED)
                elif self._machine.can_transition(current, OrderStatus.FAILED):
                    failed = self._machine.transition(current, OrderStatus.FAILED)
                    order.status = (
                        failed
                        if terminal_failure
                        else self._machine.transition(failed, OrderStatus.QUEUED)
                    )
            if presentation is not None and presentation.status != OrderStatus.COMPLETED:
                presentation.status = (
                    OrderStatus(order.status) if order is not None else OrderStatus.FAILED
                )
                presentation.error_message = detail
            job.status = "FAILED" if terminal_failure else "RETRYING"
            job.retries += 1
            job.error_message = detail
            job.current_step = "failed" if terminal_failure else "retrying"
            job.lease_token = None
            job.lease_expires_at = None
            attempt = await session.scalar(
                select(GenerationAttempt).where(GenerationAttempt.fencing_token == lease_token)
            )
            if attempt is not None:
                attempt.status = "FAILED"
                attempt.finished_at = datetime.now(UTC)
                attempt.error_message = detail
