"""Idempotent background presentation generation pipeline."""

import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from modules.users.models import FreeDemoClaim, User


class PdfConverter(Protocol):
    async def convert(self, pptx_path: Path, output_pdf: Path) -> Path: ...


class GenerationQualityError(RuntimeError):
    pass


class GenerationLeaseLost(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GenerationResult:
    presentation_id: int
    completed: bool
    pptx_storage_key: str
    pdf_storage_key: str


class GenerationPipeline:
    def __init__(
        self,
        *,
        renderer: NodePresentationRenderer,
        pdf_converter: PdfConverter,
        storage: Storage,
        work_root: Path,
    ) -> None:
        self._renderer = renderer
        self._pdf_converter = pdf_converter
        self._storage = storage
        self._work_root = work_root.expanduser().resolve()
        self._work_root.mkdir(parents=True, exist_ok=True)
        self._machine = OrderStateMachine()

    async def run(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        presentation_id: int,
    ) -> GenerationResult:
        lease_token = uuid4().hex
        async with session_factory() as session, session.begin():
            presentation = await session.scalar(
                select(Presentation).where(Presentation.id == presentation_id).with_for_update()
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
            order = await session.get(Order, presentation.order_id, with_for_update=True)
            if order is None:
                raise LookupError("presentation order not found")
            job = await session.scalar(
                select(GenerationJob)
                .where(GenerationJob.presentation_id == presentation.id)
                .with_for_update()
            )
            if job is None:
                raise LookupError("generation job not found")
            now = datetime.now(UTC)
            lease_expires_at = job.lease_expires_at
            if lease_expires_at is not None and lease_expires_at.tzinfo is None:
                lease_expires_at = lease_expires_at.replace(tzinfo=UTC)
            if job.status == "RUNNING" and lease_expires_at is not None and lease_expires_at > now:
                return GenerationResult(
                    presentation_id=presentation.id,
                    completed=False,
                    pptx_storage_key="",
                    pdf_storage_key="",
                )
            current = OrderStatus(order.status)
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
            claim = await session.scalar(
                select(FreeDemoClaim).where(FreeDemoClaim.order_id == order.id).with_for_update()
            )
            if claim is not None and claim.status == "RESERVED":
                claim.status = "CONSUMED"
                claim.consumed_at = datetime.now(UTC)
                user = await session.get(User, claim.user_id, with_for_update=True)
                if user is not None:
                    user.free_demo_used = True
            payload = build_engine_payload(
                title=presentation.title,
                language=presentation.language,
                direction=presentation.direction,
                design_style=presentation.design_style,
                plan_code=plan.code,
                outline=dict(presentation.outline),
            )
            expected_slides = presentation.slide_count
            user_id = presentation.user_id
            order_id = order.id

        try:
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
                presentation = await session.get(
                    Presentation, presentation_id, with_for_update=True
                )
                order = await session.get(Order, order_id, with_for_update=True)
                if presentation is None or order is None:
                    raise LookupError("generation aggregate disappeared")
                job = await session.scalar(
                    select(GenerationJob)
                    .where(GenerationJob.presentation_id == presentation_id)
                    .with_for_update()
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
            )
            raise

    async def _mark_quality_check(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        presentation_id: int,
        order_id: int,
        lease_token: str,
    ) -> None:
        async with session_factory() as session, session.begin():
            presentation = await session.get(Presentation, presentation_id, with_for_update=True)
            order = await session.get(Order, order_id, with_for_update=True)
            if presentation is None or order is None:
                raise LookupError("generation aggregate disappeared")
            order.status = self._machine.transition(
                OrderStatus(order.status),
                OrderStatus.QUALITY_CHECK,
            )
            presentation.status = OrderStatus.QUALITY_CHECK
            job = await session.scalar(
                select(GenerationJob).where(GenerationJob.presentation_id == presentation_id)
            )
            if job is None or job.lease_token != lease_token:
                raise GenerationLeaseLost("generation lease expired during rendering")
            now = datetime.now(UTC)
            job.progress = 80
            job.current_step = "quality_check"
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
    ) -> None:
        detail = f"{type(error).__name__}: {error}"[:2000]
        async with session_factory() as session, session.begin():
            job = await session.scalar(
                select(GenerationJob)
                .where(GenerationJob.presentation_id == presentation_id)
                .with_for_update()
            )
            if job is None or job.lease_token != lease_token:
                return
            presentation = await session.get(Presentation, presentation_id, with_for_update=True)
            order = await session.get(Order, order_id, with_for_update=True)
            if presentation is not None and presentation.status != OrderStatus.COMPLETED:
                presentation.status = OrderStatus.FAILED
                presentation.error_message = detail
            if order is not None and order.status != OrderStatus.COMPLETED:
                current = OrderStatus(order.status)
                if self._machine.can_transition(current, OrderStatus.FAILED):
                    order.status = OrderStatus.FAILED
            job.status = "FAILED"
            job.retries += 1
            job.error_message = detail
            job.current_step = "failed"
            job.lease_token = None
            job.lease_expires_at = None
            attempt = await session.scalar(
                select(GenerationAttempt).where(GenerationAttempt.fencing_token == lease_token)
            )
            if attempt is not None:
                attempt.status = "FAILED"
                attempt.finished_at = datetime.now(UTC)
                attempt.error_message = detail
