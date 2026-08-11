"""Lease-fenced Telegram delivery for Referat artifacts."""

import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiogram.types import FSInputFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.outbox.models import OutboxEvent

from modules.files.storage import Storage
from modules.service_orders.catalog import ServiceType
from modules.service_orders.document_services import document_service_profile
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderStatus
from modules.users.models import User


class EssayDeliveryInProgress(RuntimeError):
    def __init__(self, *, retry_after_ms: int) -> None:
        super().__init__("essay delivery is owned by an active lease")
        self.retry_after_ms = max(1_000, retry_after_ms)


class EssayDeliveryLeaseLost(RuntimeError):
    pass


class EssayDeliveryAmbiguous(RuntimeError):
    pass


class EssayDeliveryAmbiguousPersistencePending(RuntimeError):
    pass


class EssayArtifactTooLarge(RuntimeError):
    pass


_CAPTIONS = {
    ServiceType.ESSAY: {
        "uz": {"docx": "✅ Referat tayyor — DOCX", "pdf": "✅ Referat tayyor — PDF"},
        "ru": {"docx": "✅ Реферат готов — DOCX", "pdf": "✅ Реферат готов — PDF"},
        "en": {"docx": "✅ Academic essay ready — DOCX", "pdf": "✅ Academic essay ready — PDF"},
    },
    ServiceType.INDEPENDENT_WORK: {
        "uz": {"docx": "✅ Mustaqil ish tayyor — DOCX", "pdf": "✅ Mustaqil ish tayyor — PDF"},
        "ru": {
            "docx": "✅ Самостоятельная работа готова — DOCX",
            "pdf": "✅ Самостоятельная работа готова — PDF",
        },
        "en": {
            "docx": "✅ Independent assignment ready — DOCX",
            "pdf": "✅ Independent assignment ready — PDF",
        },
    },
    ServiceType.COURSE_WORK: {
        "uz": {"docx": "✅ Kurs ishi tayyor — DOCX", "pdf": "✅ Kurs ishi tayyor — PDF"},
        "ru": {
            "docx": "✅ Курсовая работа готова — DOCX",
            "pdf": "✅ Курсовая работа готова — PDF",
        },
        "en": {
            "docx": "✅ Course paper ready — DOCX",
            "pdf": "✅ Course paper ready — PDF",
        },
    },
    ServiceType.SUMMARY: {
        "uz": {"docx": "✅ Konspekt tayyor — DOCX", "pdf": "✅ Konspekt tayyor — PDF"},
        "ru": {
            "docx": "✅ Конспект готов — DOCX",
            "pdf": "✅ Конспект готов — PDF",
        },
        "en": {
            "docx": "✅ Summary notes ready — DOCX",
            "pdf": "✅ Summary notes ready — PDF",
        },
    },
    ServiceType.QUIZ: {
        "uz": {"docx": "✅ Quiz tayyor — DOCX", "pdf": "✅ Quiz tayyor — PDF"},
        "ru": {"docx": "✅ Тест готов — DOCX", "pdf": "✅ Тест готов — PDF"},
        "en": {"docx": "✅ Quiz ready — DOCX", "pdf": "✅ Quiz ready — PDF"},
    },
    ServiceType.WRITE_TEXT: {
        "uz": {"docx": "✅ Matn tayyor — DOCX", "pdf": "✅ Matn tayyor — PDF"},
        "ru": {"docx": "✅ Текст готов — DOCX", "pdf": "✅ Текст готов — PDF"},
        "en": {"docx": "✅ Text ready — DOCX", "pdf": "✅ Text ready — PDF"},
    },
}


async def _ensure_orphan_cleanup_event(
    session: AsyncSession, order: ServiceOrder, *, now: datetime
) -> None:
    candidates = [
        key
        for key in order.generation_metadata.get("orphan_candidate_keys", [])
        if isinstance(key, str)
    ]
    if not candidates:
        return
    profile = document_service_profile(order.service_type)
    idempotency_key = f"service-{profile.service_type.value}-cleanup:{order.id}"
    existing = await session.scalar(
        select(OutboxEvent.id).where(OutboxEvent.idempotency_key == idempotency_key)
    )
    if existing is None:
        session.add(
            OutboxEvent(
                event_type=profile.cleanup_event,
                aggregate_type="service_order",
                aggregate_id=str(order.id),
                idempotency_key=idempotency_key,
                payload={"service_order_id": order.id},
                status="PENDING",
                available_at=now + timedelta(hours=24),
            )
        )


@dataclass(frozen=True, slots=True)
class EssayDeliveryResult:
    service_order_id: int
    delivered: bool
    docx_message_id: int | None
    pdf_message_id: int | None


class TelegramEssayDelivery:
    def __init__(
        self,
        *,
        storage: Storage,
        work_root: Path,
        max_upload_bytes: int = 49_000_000,
        lease_seconds: int = 300,
        service_type: ServiceType = ServiceType.ESSAY,
    ) -> None:
        if max_upload_bytes < 1 or lease_seconds < 30:
            raise ValueError("invalid delivery limits")
        self._storage = storage
        self._work_root = work_root.expanduser().resolve()
        self._work_root.mkdir(parents=True, exist_ok=True)
        self._max_upload_bytes = max_upload_bytes
        self._lease_seconds = lease_seconds
        self._profile = document_service_profile(service_type)

    async def deliver(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        service_order_id: int,
        bot: Any,
    ) -> EssayDeliveryResult:
        lease_token = uuid4().hex
        now = datetime.now(UTC)
        async with session_factory() as session, session.begin():
            order = await session.scalar(
                select(ServiceOrder).where(ServiceOrder.id == service_order_id).with_for_update()
            )
            if order is None or order.service_type != self._profile.service_type.value:
                raise LookupError("document service order not found")
            if order.delivery_status in {"DELIVERED", "AMBIGUOUS", "TERMINAL_FAILED"}:
                return self._result(order)
            expires_at = order.delivery_lease_expires_at
            if expires_at is not None and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if order.delivery_status == "SENDING" and expires_at is not None and expires_at > now:
                raise EssayDeliveryInProgress(
                    retry_after_ms=int((expires_at - now).total_seconds() * 1_000) + 1_000
                )
            if order.delivery_status == "SENDING":
                order.delivery_status = "AMBIGUOUS"
                order.delivery_lease_token = None
                order.delivery_lease_expires_at = None
                order.delivery_error = "delivery acceptance is ambiguous; automatic resend disabled"
                return self._result(order)
            if order.status != ServiceOrderStatus.ARTIFACTS_READY or not order.docx_storage_key:
                raise ValueError("document service artifacts are not ready")
            user = await session.get(User, order.user_id)
            if user is None or user.is_blocked:
                raise PermissionError("document service owner is missing or blocked")
            order.delivery_status = "SENDING"
            order.delivery_attempts += 1
            order.delivery_lease_token = lease_token
            order.delivery_lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            order.delivery_error = None
            telegram_user_id = user.telegram_user_id
            user_id = user.id
            captions = _CAPTIONS[self._profile.service_type]
            language = user.selected_language if user.selected_language in captions else "uz"
            docx_key = order.docx_storage_key
            pdf_key = order.pdf_storage_key
            docx_message_id = order.docx_telegram_message_id
            pdf_message_id = order.pdf_telegram_message_id

        try:
            with tempfile.TemporaryDirectory(
                prefix=f"{self._profile.artifact_stem}-delivery-{service_order_id}-",
                dir=self._work_root,
            ) as directory:
                workdir = Path(directory)
                docx_path = await self._storage.download_for_user(
                    docx_key,
                    workdir / f"{self._profile.artifact_stem}_{service_order_id}.docx",
                    user_id=user_id,
                )
                self._check_size(docx_path)
                if docx_message_id is None:
                    docx_message_id = await self._send_checkpointed(
                        session_factory,
                        bot=bot,
                        service_order_id=service_order_id,
                        lease_token=lease_token,
                        telegram_user_id=telegram_user_id,
                        path=docx_path,
                        artifact="docx",
                        caption=captions[language]["docx"],
                    )
                if pdf_key is not None and pdf_message_id is None:
                    pdf_path = await self._storage.download_for_user(
                        pdf_key,
                        workdir / f"{self._profile.artifact_stem}_{service_order_id}.pdf",
                        user_id=user_id,
                    )
                    self._check_size(pdf_path)
                    pdf_message_id = await self._send_checkpointed(
                        session_factory,
                        bot=bot,
                        service_order_id=service_order_id,
                        lease_token=lease_token,
                        telegram_user_id=telegram_user_id,
                        path=pdf_path,
                        artifact="pdf",
                        caption=captions[language]["pdf"],
                    )
            return await self._mark_delivered(
                session_factory,
                service_order_id=service_order_id,
                lease_token=lease_token,
            )
        except (EssayDeliveryAmbiguous, EssayDeliveryAmbiguousPersistencePending):
            raise
        except Exception as exc:
            await self._record_failure(
                session_factory,
                service_order_id=service_order_id,
                lease_token=lease_token,
                error=exc,
            )
            raise

    async def _send_checkpointed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        bot: Any,
        service_order_id: int,
        lease_token: str,
        telegram_user_id: int,
        path: Path,
        artifact: str,
        caption: str,
    ) -> int:
        await self._renew_delivery_lease(
            session_factory,
            service_order_id=service_order_id,
            lease_token=lease_token,
        )
        try:
            message = await bot.send_document(
                chat_id=telegram_user_id,
                document=FSInputFile(
                    path,
                    filename=f"{self._profile.artifact_stem}_{service_order_id}.{artifact}",
                ),
                caption=caption,
            )
            message_id = int(message.message_id)
            await self._record_message(
                session_factory,
                service_order_id=service_order_id,
                lease_token=lease_token,
                artifact=artifact,
                message_id=message_id,
            )
            return message_id
        except Exception as exc:
            try:
                await self._mark_ambiguous(
                    session_factory,
                    service_order_id=service_order_id,
                    lease_token=lease_token,
                )
            except Exception as persistence_exc:
                raise EssayDeliveryAmbiguousPersistencePending(
                    f"{artifact} ambiguity persistence is pending"
                ) from persistence_exc
            raise EssayDeliveryAmbiguous(f"{artifact} delivery acceptance is ambiguous") from exc

    async def _renew_delivery_lease(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        service_order_id: int,
        lease_token: str,
    ) -> None:
        async with session_factory() as session, session.begin():
            order = await session.get(ServiceOrder, service_order_id, with_for_update=True)
            if (
                order is None
                or order.delivery_status != "SENDING"
                or order.delivery_lease_token != lease_token
            ):
                raise EssayDeliveryLeaseLost("delivery lease was lost")
            order.delivery_lease_expires_at = datetime.now(UTC) + timedelta(
                seconds=self._lease_seconds
            )

    def _check_size(self, path: Path) -> None:
        if path.stat().st_size > self._max_upload_bytes:
            raise EssayArtifactTooLarge("artifact exceeds Telegram upload limit")

    @staticmethod
    async def _record_message(
        session_factory: async_sessionmaker[AsyncSession],
        *,
        service_order_id: int,
        lease_token: str,
        artifact: str,
        message_id: int,
    ) -> None:
        async with session_factory() as session, session.begin():
            order = await session.get(ServiceOrder, service_order_id, with_for_update=True)
            if order is None or order.delivery_lease_token != lease_token:
                raise EssayDeliveryLeaseLost("delivery lease was lost")
            if artifact == "docx":
                order.docx_telegram_message_id = message_id
            elif artifact == "pdf":
                order.pdf_telegram_message_id = message_id
            else:
                raise ValueError("unsupported essay artifact")
            order.delivery_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)

    @staticmethod
    async def _mark_ambiguous(
        session_factory: async_sessionmaker[AsyncSession],
        *,
        service_order_id: int,
        lease_token: str,
    ) -> None:
        async with session_factory() as session, session.begin():
            order = await session.get(ServiceOrder, service_order_id, with_for_update=True)
            if order is None or order.delivery_lease_token != lease_token:
                return
            order.delivery_status = "AMBIGUOUS"
            order.delivery_lease_token = None
            order.delivery_lease_expires_at = None
            order.delivery_error = "delivery acceptance is ambiguous; automatic resend disabled"

    @staticmethod
    async def _mark_delivered(
        session_factory: async_sessionmaker[AsyncSession],
        *,
        service_order_id: int,
        lease_token: str,
    ) -> EssayDeliveryResult:
        async with session_factory() as session, session.begin():
            order = await session.get(ServiceOrder, service_order_id, with_for_update=True)
            if order is None or order.delivery_lease_token != lease_token:
                raise EssayDeliveryLeaseLost("delivery lease was lost")
            if order.docx_telegram_message_id is None:
                raise RuntimeError("DOCX was not delivered")
            if order.pdf_storage_key is not None and order.pdf_telegram_message_id is None:
                raise RuntimeError("PDF was not delivered")
            order.delivery_status = "DELIVERED"
            order.status = ServiceOrderStatus.DELIVERED
            order.delivered_at = datetime.now(UTC)
            order.delivery_lease_token = None
            order.delivery_lease_expires_at = None
            order.delivery_error = None
            return TelegramEssayDelivery._result(order)

    @staticmethod
    async def _record_failure(
        session_factory: async_sessionmaker[AsyncSession],
        *,
        service_order_id: int,
        lease_token: str,
        error: Exception,
    ) -> None:
        async with session_factory() as session, session.begin():
            order = await session.get(ServiceOrder, service_order_id, with_for_update=True)
            if order is None or order.delivery_lease_token != lease_token:
                return
            order.delivery_status = "FAILED"
            order.delivery_lease_token = None
            order.delivery_lease_expires_at = None
            order.delivery_error = f"{type(error).__name__}: delivery failed"

    @staticmethod
    def _result(order: ServiceOrder) -> EssayDeliveryResult:
        return EssayDeliveryResult(
            service_order_id=order.id,
            delivered=order.delivery_status == "DELIVERED",
            docx_message_id=order.docx_telegram_message_id,
            pdf_message_id=order.pdf_telegram_message_id,
        )


class TelegramIndependentWorkDelivery(TelegramEssayDelivery):
    def __init__(
        self,
        *,
        storage: Storage,
        work_root: Path,
        max_upload_bytes: int = 49_000_000,
        lease_seconds: int = 300,
    ) -> None:
        super().__init__(
            storage=storage,
            work_root=work_root,
            max_upload_bytes=max_upload_bytes,
            lease_seconds=lease_seconds,
            service_type=ServiceType.INDEPENDENT_WORK,
        )


class TelegramCourseWorkDelivery(TelegramEssayDelivery):
    def __init__(
        self,
        *,
        storage: Storage,
        work_root: Path,
        max_upload_bytes: int = 49_000_000,
        lease_seconds: int = 300,
    ) -> None:
        super().__init__(
            storage=storage,
            work_root=work_root,
            max_upload_bytes=max_upload_bytes,
            lease_seconds=lease_seconds,
            service_type=ServiceType.COURSE_WORK,
        )


class TelegramSummaryDelivery(TelegramEssayDelivery):
    def __init__(
        self,
        *,
        storage: Storage,
        work_root: Path,
        max_upload_bytes: int = 49_000_000,
        lease_seconds: int = 300,
    ) -> None:
        super().__init__(
            storage=storage,
            work_root=work_root,
            max_upload_bytes=max_upload_bytes,
            lease_seconds=lease_seconds,
            service_type=ServiceType.SUMMARY,
        )


class TelegramQuizDelivery(TelegramEssayDelivery):
    def __init__(
        self,
        *,
        storage: Storage,
        work_root: Path,
        max_upload_bytes: int = 49_000_000,
        lease_seconds: int = 300,
    ) -> None:
        super().__init__(
            storage=storage,
            work_root=work_root,
            max_upload_bytes=max_upload_bytes,
            lease_seconds=lease_seconds,
            service_type=ServiceType.QUIZ,
        )


class TelegramWriteTextDelivery(TelegramEssayDelivery):
    def __init__(
        self,
        *,
        storage: Storage,
        work_root: Path,
        max_upload_bytes: int = 49_000_000,
        lease_seconds: int = 300,
    ) -> None:
        super().__init__(
            storage=storage,
            work_root=work_root,
            max_upload_bytes=max_upload_bytes,
            lease_seconds=lease_seconds,
            service_type=ServiceType.WRITE_TEXT,
        )


async def mark_terminal_document_delivery_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    service_order_id: int,
    service_type: ServiceType,
) -> bool:
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        order = await session.get(ServiceOrder, service_order_id, with_for_update=True)
        if order is None or order.service_type != service_type.value:
            return True
        if order.delivery_status in {"DELIVERED", "AMBIGUOUS", "TERMINAL_FAILED"}:
            await _ensure_orphan_cleanup_event(session, order, now=now)
            return True
        expires_at = order.delivery_lease_expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if order.delivery_status == "SENDING":
            if expires_at is not None and expires_at > now:
                raise EssayDeliveryInProgress(
                    retry_after_ms=int((expires_at - now).total_seconds() * 1_000) + 1_000
                )
            order.delivery_status = "AMBIGUOUS"
            order.delivery_error = "delivery acceptance is ambiguous; automatic resend disabled"
        elif order.status == ServiceOrderStatus.ARTIFACTS_READY and order.delivery_status in {
            "FAILED",
            "PENDING",
        }:
            order.delivery_status = "TERMINAL_FAILED"
            order.delivery_error = (
                "delivery retries exhausted; artifacts remain available in history"
            )
        else:
            return False
        order.delivery_lease_token = None
        order.delivery_lease_expires_at = None
        await _ensure_orphan_cleanup_event(session, order, now=now)
        return True


async def mark_terminal_essay_delivery_failure(
    session_factory: async_sessionmaker[AsyncSession], *, service_order_id: int
) -> bool:
    return await mark_terminal_document_delivery_failure(
        session_factory,
        service_order_id=service_order_id,
        service_type=ServiceType.ESSAY,
    )


async def mark_terminal_independent_work_delivery_failure(
    session_factory: async_sessionmaker[AsyncSession], *, service_order_id: int
) -> bool:
    return await mark_terminal_document_delivery_failure(
        session_factory,
        service_order_id=service_order_id,
        service_type=ServiceType.INDEPENDENT_WORK,
    )


async def mark_terminal_course_work_delivery_failure(
    session_factory: async_sessionmaker[AsyncSession], *, service_order_id: int
) -> bool:
    return await mark_terminal_document_delivery_failure(
        session_factory,
        service_order_id=service_order_id,
        service_type=ServiceType.COURSE_WORK,
    )


async def mark_terminal_summary_delivery_failure(
    session_factory: async_sessionmaker[AsyncSession], *, service_order_id: int
) -> bool:
    return await mark_terminal_document_delivery_failure(
        session_factory,
        service_order_id=service_order_id,
        service_type=ServiceType.SUMMARY,
    )


async def mark_terminal_quiz_delivery_failure(
    session_factory: async_sessionmaker[AsyncSession], *, service_order_id: int
) -> bool:
    return await mark_terminal_document_delivery_failure(
        session_factory,
        service_order_id=service_order_id,
        service_type=ServiceType.QUIZ,
    )


async def mark_terminal_write_text_delivery_failure(
    session_factory: async_sessionmaker[AsyncSession], *, service_order_id: int
) -> bool:
    return await mark_terminal_document_delivery_failure(
        session_factory,
        service_order_id=service_order_id,
        service_type=ServiceType.WRITE_TEXT,
    )
