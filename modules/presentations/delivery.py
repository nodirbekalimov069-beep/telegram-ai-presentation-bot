"""Idempotent Telegram delivery for generated presentation artifacts."""

import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiogram.types import FSInputFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modules.files.security import sanitize_filename
from modules.files.storage import Storage
from modules.orders.domain import OrderStatus
from modules.orders.models import Order
from modules.presentations.models import GenerationJob, Presentation
from modules.users.models import User


class DeliveryInProgress(RuntimeError):
    def __init__(self, *, retry_after_ms: int) -> None:
        super().__init__("artifact delivery is already owned by an active lease")
        self.retry_after_ms = max(1_000, retry_after_ms)


class DeliveryLeaseLost(RuntimeError):
    pass


class ArtifactTooLarge(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    presentation_id: int
    delivered: bool
    pptx_message_id: int | None
    pdf_message_id: int | None


async def mark_terminal_delivery_timeout(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    presentation_id: int,
    error: Exception,
) -> bool:
    """Fence and persist exhausted delivery timeout recovery."""

    now = datetime.now(UTC)
    detail = f"{type(error).__name__}: terminal artifact delivery timeout"
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
        if OrderStatus(order.status) == OrderStatus.CANCELLED:
            return True
        if presentation.delivery_status == "DELIVERED":
            return True
        if presentation.status == OrderStatus.FAILED:
            return True
        if presentation.status != OrderStatus.COMPLETED:
            return False
        expires_at = presentation.delivery_lease_expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if (
            presentation.delivery_status == "SENDING"
            and expires_at is not None
            and expires_at > now
        ):
            return False
        job = await session.scalar(
            select(GenerationJob)
            .where(GenerationJob.presentation_id == presentation_id)
            .with_for_update()
        )
        presentation.delivery_status = "FAILED"
        presentation.delivery_error = detail
        presentation.delivery_lease_token = None
        presentation.delivery_lease_expires_at = None
        if job is not None:
            job.status = "DELIVERY_FAILED"
            job.current_step = "delivery_failed"
            job.error_message = detail
        return True


class TelegramArtifactDelivery:
    def __init__(
        self,
        *,
        storage: Storage,
        work_root: Path,
        max_upload_bytes: int = 49_000_000,
    ) -> None:
        self._storage = storage
        self._work_root = work_root.expanduser().resolve()
        self._work_root.mkdir(parents=True, exist_ok=True)
        self._max_upload_bytes = max_upload_bytes

    async def deliver(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        presentation_id: int,
        bot: Any,
    ) -> DeliveryResult:
        lease_token = uuid4().hex
        now = datetime.now(UTC)
        async with session_factory() as session, session.begin():
            presentation = await session.get(Presentation, presentation_id, with_for_update=True)
            if presentation is None:
                raise LookupError(f"presentation {presentation_id} not found")
            if presentation.delivery_status == "DELIVERED":
                return self._result(presentation)
            expires_at = presentation.delivery_lease_expires_at
            if expires_at is not None and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if (
                presentation.delivery_status == "SENDING"
                and expires_at is not None
                and expires_at > now
            ):
                raise DeliveryInProgress(
                    retry_after_ms=int((expires_at - now).total_seconds() * 1_000) + 1_000
                )
            if (
                presentation.status != OrderStatus.COMPLETED
                or not presentation.pptx_storage_key
                or not presentation.pdf_storage_key
            ):
                raise ValueError("presentation artifacts are not ready")
            order = await session.get(Order, presentation.order_id)
            user = await session.get(User, presentation.user_id)
            if order is None or user is None:
                raise LookupError("presentation owner or order is missing")
            presentation.delivery_status = "SENDING"
            presentation.delivery_attempts += 1
            presentation.delivery_lease_token = lease_token
            presentation.delivery_lease_expires_at = now + timedelta(minutes=5)
            presentation.delivery_error = None
            telegram_user_id = user.telegram_user_id
            user_id = user.id
            title = presentation.title
            pptx_key = presentation.pptx_storage_key
            pdf_key = presentation.pdf_storage_key
            sent_pptx_id = presentation.pptx_telegram_message_id
            sent_pdf_id = presentation.pdf_telegram_message_id

        try:
            with tempfile.TemporaryDirectory(
                prefix=f"delivery-{presentation_id}-",
                dir=self._work_root,
            ) as directory:
                workdir = Path(directory)
                pptx_path = await self._storage.download_for_user(
                    pptx_key,
                    workdir / "presentation.pptx",
                    user_id=user_id,
                )
                pdf_path = await self._storage.download_for_user(
                    pdf_key,
                    workdir / "presentation.pdf",
                    user_id=user_id,
                )
                self._check_size(pptx_path)
                self._check_size(pdf_path)
                pptx_filename = sanitize_filename(
                    f"{title}.pptx",
                    allowed_extensions={".pptx"},
                )
                pdf_filename = sanitize_filename(
                    f"{title}.pdf",
                    allowed_extensions={".pdf"},
                )
                if sent_pptx_id is None:
                    message = await bot.send_document(
                        chat_id=telegram_user_id,
                        document=FSInputFile(pptx_path, filename=pptx_filename),
                        caption=f"✅ {title} — PPTX",
                    )
                    sent_pptx_id = int(message.message_id)
                    await self._record_message(
                        session_factory,
                        presentation_id=presentation_id,
                        lease_token=lease_token,
                        artifact="pptx",
                        message_id=sent_pptx_id,
                    )
                if sent_pdf_id is None:
                    message = await bot.send_document(
                        chat_id=telegram_user_id,
                        document=FSInputFile(pdf_path, filename=pdf_filename),
                        caption=f"✅ {title} — PDF",
                    )
                    sent_pdf_id = int(message.message_id)
                    await self._record_message(
                        session_factory,
                        presentation_id=presentation_id,
                        lease_token=lease_token,
                        artifact="pdf",
                        message_id=sent_pdf_id,
                    )
            return await self._mark_delivered(
                session_factory,
                presentation_id=presentation_id,
                lease_token=lease_token,
            )
        except Exception as exc:
            await self._record_failure(
                session_factory,
                presentation_id=presentation_id,
                lease_token=lease_token,
                error=exc,
            )
            raise

    def _check_size(self, path: Path) -> None:
        if path.stat().st_size > self._max_upload_bytes:
            raise ArtifactTooLarge("artifact exceeds the configured Telegram upload limit")

    @staticmethod
    async def _record_message(
        session_factory: async_sessionmaker[AsyncSession],
        *,
        presentation_id: int,
        lease_token: str,
        artifact: str,
        message_id: int,
    ) -> None:
        async with session_factory() as session, session.begin():
            presentation = await session.get(Presentation, presentation_id, with_for_update=True)
            if presentation is None or presentation.delivery_lease_token != lease_token:
                raise DeliveryLeaseLost("delivery fencing token is no longer current")
            if artifact == "pptx":
                presentation.pptx_telegram_message_id = message_id
            elif artifact == "pdf":
                presentation.pdf_telegram_message_id = message_id
            else:
                raise ValueError(f"unsupported artifact: {artifact}")
            presentation.delivery_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)

    @staticmethod
    async def _mark_delivered(
        session_factory: async_sessionmaker[AsyncSession],
        *,
        presentation_id: int,
        lease_token: str,
    ) -> DeliveryResult:
        async with session_factory() as session, session.begin():
            presentation = await session.get(Presentation, presentation_id, with_for_update=True)
            if presentation is None or presentation.delivery_lease_token != lease_token:
                raise DeliveryLeaseLost("delivery fencing token is no longer current")
            if (
                presentation.pptx_telegram_message_id is None
                or presentation.pdf_telegram_message_id is None
            ):
                raise RuntimeError("not all artifacts were delivered")
            presentation.delivery_status = "DELIVERED"
            presentation.delivered_at = datetime.now(UTC)
            presentation.delivery_lease_token = None
            presentation.delivery_lease_expires_at = None
            presentation.delivery_error = None
            job = await session.scalar(
                select(GenerationJob).where(GenerationJob.presentation_id == presentation_id)
            )
            if job is not None:
                job.status = "COMPLETED"
                job.progress = 100
                job.current_step = "completed"
            return TelegramArtifactDelivery._result(presentation)

    @staticmethod
    async def _record_failure(
        session_factory: async_sessionmaker[AsyncSession],
        *,
        presentation_id: int,
        lease_token: str,
        error: Exception,
    ) -> None:
        async with session_factory() as session, session.begin():
            presentation = await session.get(Presentation, presentation_id, with_for_update=True)
            if presentation is None or presentation.delivery_lease_token != lease_token:
                return
            presentation.delivery_status = "FAILED"
            presentation.delivery_lease_token = None
            presentation.delivery_lease_expires_at = None
            presentation.delivery_error = f"{type(error).__name__}: delivery failed"
            job = await session.scalar(
                select(GenerationJob).where(GenerationJob.presentation_id == presentation_id)
            )
            if job is not None:
                job.status = "DELIVERY_FAILED"
                job.current_step = "delivery_failed"
                job.error_message = f"{type(error).__name__}: delivery failed"

    @staticmethod
    def _result(presentation: Presentation) -> DeliveryResult:
        return DeliveryResult(
            presentation_id=presentation.id,
            delivered=presentation.delivery_status == "DELIVERED",
            pptx_message_id=presentation.pptx_telegram_message_id,
            pdf_message_id=presentation.pdf_telegram_message_id,
        )
