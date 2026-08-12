"""Durable, lease-protected Referat generation pipeline."""

import asyncio
import logging
import shutil
from contextlib import suppress
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.outbox.models import OutboxEvent
from modules.ai.contracts import AIContractError
from modules.ai.course_work_contracts import CourseWorkGenerationRequest
from modules.ai.essay_contracts import EssayGenerationRequest
from modules.ai.independent_work_contracts import IndependentWorkGenerationRequest
from modules.ai.quiz_contracts import QuizGenerationRequest
from modules.ai.rewrite_contracts import RewriteGenerationRequest
from modules.ai.summary_contracts import SummaryGenerationRequest
from modules.ai.translation_contracts import (
    TranslationGenerationRequest,
    translation_protected_association_score,
)
from modules.ai.write_text_contracts import WriteTextGenerationRequest
from modules.ai.providers.base import AIProvider
from modules.files.storage import Storage
from modules.service_orders.catalog import ServiceType
from modules.service_orders.course_work_documents import CourseWorkDocumentRenderer
from modules.service_orders.document_services import document_service_profile
from modules.service_orders.documents import DocumentTooLarge, EssayDocumentRenderer
from modules.service_orders.independent_work_documents import IndependentWorkDocumentRenderer
from modules.service_orders.quiz_documents import QuizDocumentRenderer
from modules.service_orders.rewrite_documents import RewriteDocumentRenderer
from modules.service_orders.summary_documents import SummaryDocumentRenderer
from modules.service_orders.translation_documents import (
    TranslationDocumentRenderer,
    normalized_translation_body,
    translation_artifact_parity_score,
    translation_body_digest,
)
from modules.service_orders.write_text_documents import WriteTextDocumentRenderer
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderStatus
from modules.presentations.pdf import validate_text_pdf

PdfConverter = Callable[..., Awaitable[Path]]
logger = logging.getLogger(__name__)


def _promote_pending_artifacts(order: ServiceOrder) -> list[str]:
    metadata = dict(order.generation_metadata)
    pending = [
        str(key) for key in metadata.pop("pending_artifact_keys", []) if isinstance(key, str)
    ]
    candidates = [
        str(key) for key in metadata.get("orphan_candidate_keys", []) if isinstance(key, str)
    ]
    merged = list(dict.fromkeys([*candidates, *pending]))
    if merged:
        metadata["orphan_candidate_keys"] = merged
    else:
        metadata.pop("orphan_candidate_keys", None)
    order.generation_metadata = metadata
    return merged


async def _schedule_orphan_cleanup(
    session: AsyncSession, order: ServiceOrder, *, available_at: datetime
) -> None:
    candidates = _promote_pending_artifacts(order)
    if not candidates:
        return
    profile = document_service_profile(order.service_type)
    idempotency_key = f"service-{profile.service_type.value}-cleanup:{order.id}"
    event = await session.scalar(
        select(OutboxEvent).where(OutboxEvent.idempotency_key == idempotency_key).with_for_update()
    )
    if event is None:
        session.add(
            OutboxEvent(
                event_type=profile.cleanup_event,
                aggregate_type="service_order",
                aggregate_id=str(order.id),
                idempotency_key=idempotency_key,
                payload={"service_order_id": order.id},
                status="PENDING",
                available_at=available_at,
            )
        )
        return
    event.event_type = profile.cleanup_event
    event.aggregate_type = "service_order"
    event.aggregate_id = str(order.id)
    event.payload = {"service_order_id": order.id}
    event.status = "PENDING"
    event.attempts = 0
    event.available_at = available_at
    event.published_at = None
    event.last_error = None


class EssayRetryableError(RuntimeError):
    pass


class EssayTerminalError(RuntimeError):
    pass


class EssayLeaseBusy(EssayRetryableError):
    pass


class EssayPipeline:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: AIProvider,
        storage: Storage,
        *,
        temp_root: Path,
        pdf_converter: PdfConverter,
        renderer: (
            EssayDocumentRenderer
            | IndependentWorkDocumentRenderer
            | CourseWorkDocumentRenderer
            | SummaryDocumentRenderer
            | QuizDocumentRenderer
            | WriteTextDocumentRenderer
            | RewriteDocumentRenderer
            | TranslationDocumentRenderer
            | None
        ) = None,
        service_type: ServiceType = ServiceType.ESSAY,
        lease_seconds: int = 900,
        pdf_timeout_seconds: int = 120,
    ) -> None:
        if lease_seconds < 30:
            raise ValueError("lease_seconds is too short")
        self._session_factory = session_factory
        self._provider = provider
        self._storage = storage
        self._temp_root = temp_root.expanduser().resolve()
        self._pdf_converter = pdf_converter
        self._profile = document_service_profile(service_type)
        self._generation_failure_message = {
            ServiceType.ESSAY: "Referat matnini yaratib bo‘lmadi",
            ServiceType.INDEPENDENT_WORK: "Mustaqil ish matnini yaratib bo‘lmadi",
            ServiceType.COURSE_WORK: "Kurs ishi matnini yaratib bo‘lmadi",
            ServiceType.SUMMARY: "Konspekt matnini yaratib bo‘lmadi",
            ServiceType.QUIZ: "Quiz savollarini yaratib bo‘lmadi",
            ServiceType.WRITE_TEXT: "Matnni yaratib bo‘lmadi",
            ServiceType.REWRITE: "Matnni qayta yozib bo‘lmadi",
            ServiceType.TRANSLATE: "Matnni tarjima qilib bo‘lmadi",
        }[service_type]
        self._document_failure_message = {
            ServiceType.ESSAY: "Referat hujjatini yaratib bo‘lmadi",
            ServiceType.INDEPENDENT_WORK: "Mustaqil ish hujjatini yaratib bo‘lmadi",
            ServiceType.COURSE_WORK: "Kurs ishi hujjatini yaratib bo‘lmadi",
            ServiceType.SUMMARY: "Konspekt hujjatini yaratib bo‘lmadi",
            ServiceType.QUIZ: "Quiz hujjatini yaratib bo‘lmadi",
            ServiceType.WRITE_TEXT: "Matn hujjatini yaratib bo‘lmadi",
            ServiceType.REWRITE: "Qayta yozilgan matn hujjatini yaratib bo‘lmadi",
            ServiceType.TRANSLATE: "Tarjima hujjatini yaratib bo‘lmadi",
        }[service_type]
        if renderer is None:
            renderers: dict[ServiceType, type[Any]] = {
                ServiceType.ESSAY: EssayDocumentRenderer,
                ServiceType.INDEPENDENT_WORK: IndependentWorkDocumentRenderer,
                ServiceType.COURSE_WORK: CourseWorkDocumentRenderer,
                ServiceType.SUMMARY: SummaryDocumentRenderer,
                ServiceType.QUIZ: QuizDocumentRenderer,
                ServiceType.WRITE_TEXT: WriteTextDocumentRenderer,
                ServiceType.REWRITE: RewriteDocumentRenderer,
                ServiceType.TRANSLATE: TranslationDocumentRenderer,
            }
            renderer = renderers[service_type]()
        self._renderer: Any = renderer
        self._lease_seconds = lease_seconds
        self._pdf_timeout_seconds = pdf_timeout_seconds

    async def process(self, order_id: int) -> None:
        lease = await self._acquire(order_id)
        if lease is None:
            return
        token, user_id, request, pending_keys = lease
        if pending_keys:
            try:
                await self._cleanup_attempt_artifacts(pending_keys, user_id=user_id)
                await self._clear_pending_artifacts(order_id, token)
            except Exception as exc:
                await self._release_retryable(
                    order_id,
                    token,
                    error_code="ORPHAN_CLEANUP_FAILED",
                    user_message="Oldingi fayl urinishini xavfsiz tozalab bo‘lmadi",
                )
                raise EssayRetryableError("orphan artifact cleanup failed") from exc
        order_root = self._temp_root / str(order_id)
        work_dir = order_root / token
        work_dir.mkdir(parents=True, exist_ok=False)
        try:
            try:
                document: Any
                if self._profile.service_type is ServiceType.ESSAY:
                    document = await self._provider.generate_essay(request)  # type: ignore[arg-type]
                elif self._profile.service_type is ServiceType.INDEPENDENT_WORK:
                    document = await self._provider.generate_independent_work(request)  # type: ignore[arg-type]
                elif self._profile.service_type is ServiceType.COURSE_WORK:
                    document = await self._provider.generate_course_work(request)  # type: ignore[arg-type]
                elif self._profile.service_type is ServiceType.SUMMARY:
                    document = await self._provider.generate_summary(request)  # type: ignore[arg-type]
                elif self._profile.service_type is ServiceType.QUIZ:
                    document = await self._provider.generate_quiz(request)  # type: ignore[arg-type]
                elif self._profile.service_type is ServiceType.WRITE_TEXT:
                    document = await self._provider.generate_write_text(request)  # type: ignore[arg-type]
                elif self._profile.service_type is ServiceType.REWRITE:
                    document = await self._provider.generate_rewrite(request)  # type: ignore[arg-type]
                elif self._profile.service_type is ServiceType.TRANSLATE:
                    document = await self._provider.generate_translation(request)  # type: ignore[arg-type]
                else:
                    raise RuntimeError("unsupported document generation service")
            except AIContractError as exc:
                if self._profile.service_type in {ServiceType.REWRITE, ServiceType.TRANSLATE}:
                    logger.warning(
                        "source-bound contract rejected service=%s reason=%s",
                        self._profile.service_type.value,
                        str(exc)[:300],
                    )
                    await self._mark_terminal(
                        order_id,
                        token,
                        error_code="AI_CONTRACT_REJECTED",
                        user_message=self._generation_failure_message,
                    )
                    raise EssayTerminalError("source-bound contract rejected AI output") from exc
                await self._release_retryable(
                    order_id,
                    token,
                    error_code="AI_GENERATION_FAILED",
                    user_message=self._generation_failure_message,
                )
                raise EssayRetryableError("AI generation failed") from exc
            except Exception as exc:
                if self._profile.service_type in {ServiceType.REWRITE, ServiceType.TRANSLATE}:
                    await self._mark_terminal(
                        order_id,
                        token,
                        error_code="AI_PROVIDER_FAILED",
                        user_message=self._generation_failure_message,
                    )
                    raise EssayTerminalError(
                        "source-bound provider failed after bounded retries"
                    ) from exc
                await self._release_retryable(
                    order_id,
                    token,
                    error_code="AI_GENERATION_FAILED",
                    user_message=self._generation_failure_message,
                )
                raise EssayRetryableError("AI generation failed") from exc

            if self._profile.service_type is ServiceType.COURSE_WORK:
                section_count = len(document.chapters)
            elif self._profile.service_type is ServiceType.WRITE_TEXT:
                section_count = len(document.blocks)
            elif self._profile.service_type in {ServiceType.REWRITE, ServiceType.TRANSLATE}:
                section_count = len(document.segments)
            elif self._profile.service_type is ServiceType.QUIZ:
                section_count = 0
            else:
                section_count = len(document.sections)
            await self._mark_rendering(
                order_id,
                token,
                generation_metadata={
                    "language": document.language,
                    "section_count": section_count,
                    "segment_count": (
                        len(document.segments)
                        if self._profile.service_type
                        in {ServiceType.REWRITE, ServiceType.TRANSLATE}
                        else 0
                    ),
                    "question_count": (
                        len(document.questions)
                        if self._profile.service_type is ServiceType.QUIZ
                        else 0
                    ),
                    "reference_count": len(document.references),
                    **(
                        {
                            "source_language": document.source_language,
                            "target_language": document.target_language,
                            "translation_risk_gate_passed": document.verifier_passed is True,
                            "protected_association_score": translation_protected_association_score(
                                cast(TranslationGenerationRequest, request), document
                            ),
                            "canonical_body_sha256": translation_body_digest(document),
                            "canonical_body_characters": len(normalized_translation_body(document)),
                        }
                        if self._profile.service_type is ServiceType.TRANSLATE
                        else {}
                    ),
                },
            )
            try:
                docx_path = self._renderer.render(document, work_dir, order_id=order_id)
            except DocumentTooLarge as exc:
                await self._mark_terminal(
                    order_id,
                    token,
                    error_code="DOCX_TOO_LARGE",
                    user_message="Tayyor hujjat ruxsat etilgan hajmdan oshib ketdi",
                )
                raise EssayTerminalError("DOCX exceeded artifact limit") from exc
            except Exception as exc:
                await self._mark_terminal(
                    order_id,
                    token,
                    error_code="DOCX_RENDER_FAILED",
                    user_message=self._document_failure_message,
                )
                raise EssayTerminalError("DOCX rendering failed") from exc

            pdf_path: Path | None = None
            pdf_error: str | None = None
            artifact_parity_score: float | None = None
            try:
                pdf_path = await self._pdf_converter(
                    docx_path,
                    work_dir,
                    timeout_seconds=self._pdf_timeout_seconds,
                )
                report = validate_text_pdf(
                    pdf_path,
                    expected_phrases=(document.title,),
                    minimum_characters=(
                        2 if self._profile.service_type is ServiceType.TRANSLATE else 100
                    ),
                    minimum_pages={
                        ServiceType.COURSE_WORK: 4,
                        ServiceType.SUMMARY: 2,
                        ServiceType.QUIZ: 1,
                    }.get(self._profile.service_type, 1),
                )
                if not report.is_valid:
                    raise ValueError("converted PDF failed content validation")
                if self._profile.service_type is ServiceType.TRANSLATE:
                    artifact_parity_score = translation_artifact_parity_score(
                        document, docx_path=docx_path, pdf_path=pdf_path
                    )
                    if artifact_parity_score != 1.0:
                        raise ValueError("translation DOCX/PDF canonical parity failed")
            except Exception:
                pdf_error = "PDF_CONVERSION_FAILED"
                pdf_path = None

            if pdf_path is None and self._profile.service_type in {
                ServiceType.COURSE_WORK,
                ServiceType.SUMMARY,
                ServiceType.QUIZ,
                ServiceType.WRITE_TEXT,
                ServiceType.REWRITE,
                ServiceType.TRANSLATE,
            }:
                pdf_messages = {
                    ServiceType.COURSE_WORK: {
                        "uz": "Kurs ishi PDF faylini xavfsiz tayyorlab bo‘lmadi",
                        "ru": "Не удалось безопасно подготовить PDF курсовой работы",
                        "en": "The course paper PDF could not be prepared safely",
                    },
                    ServiceType.SUMMARY: {
                        "uz": "Konspekt PDF faylini xavfsiz tayyorlab bo‘lmadi",
                        "ru": "Не удалось безопасно подготовить PDF конспекта",
                        "en": "The summary-notes PDF could not be prepared safely",
                    },
                    ServiceType.QUIZ: {
                        "uz": "Quiz PDF faylini xavfsiz tayyorlab bo‘lmadi",
                        "ru": "Не удалось безопасно подготовить PDF теста",
                        "en": "The quiz PDF could not be prepared safely",
                    },
                    ServiceType.WRITE_TEXT: {
                        "uz": "Matn PDF faylini xavfsiz tayyorlab bo‘lmadi",
                        "ru": "Не удалось безопасно подготовить PDF текста",
                        "en": "The text PDF could not be prepared safely",
                    },
                    ServiceType.REWRITE: {
                        "uz": "Qayta yozilgan matn PDF faylini xavfsiz tayyorlab bo‘lmadi",
                        "ru": "Не удалось безопасно подготовить PDF переписанного текста",
                        "en": "The rewritten text PDF could not be prepared safely",
                    },
                    ServiceType.TRANSLATE: {
                        "uz": "Tarjima PDF faylini xavfsiz tayyorlab bo‘lmadi",
                        "ru": "Не удалось безопасно подготовить PDF перевода",
                        "en": "The translation PDF could not be prepared safely",
                    },
                }[self._profile.service_type]
                await self._mark_terminal(
                    order_id,
                    token,
                    error_code="PDF_CONVERSION_FAILED",
                    user_message=pdf_messages.get(
                        document.language,
                        "Akademik hujjat PDF faylini xavfsiz tayyorlab bo‘lmadi",
                    ),
                )
                service_label = {
                    ServiceType.COURSE_WORK: "Course Work",
                    ServiceType.SUMMARY: "Summary",
                    ServiceType.QUIZ: "Quiz",
                    ServiceType.WRITE_TEXT: "Write Text",
                    ServiceType.REWRITE: "Rewrite",
                    ServiceType.TRANSLATE: "Translation",
                }[self._profile.service_type]
                raise EssayTerminalError(f"{service_label} PDF conversion or validation failed")

            docx_key = self._storage.service_file_key(
                user_id=user_id,
                service_order_id=order_id,
                artifact_type="docx",
                attempt_token=token,
                **self._storage_profile_kwargs(),
            )
            pdf_key = (
                self._storage.service_file_key(
                    user_id=user_id,
                    service_order_id=order_id,
                    artifact_type="pdf",
                    attempt_token=token,
                    **self._storage_profile_kwargs(),
                )
                if pdf_path is not None
                else None
            )
            uploaded_keys = [docx_key, *([pdf_key] if pdf_key is not None else [])]
            await self._register_pending_artifacts(order_id, token, uploaded_keys)
            try:
                await self._assert_owned_lease(order_id, token)
                stored_docx_key = await self._storage.put_service_file(
                    docx_path,
                    user_id=user_id,
                    service_order_id=order_id,
                    artifact_type="docx",
                    attempt_token=token,
                    **self._storage_profile_kwargs(),
                )
                if stored_docx_key != docx_key:
                    raise RuntimeError("storage returned an unexpected DOCX key")
                await self._assert_owned_lease(order_id, token)
                if pdf_path is not None:
                    stored_pdf_key = await self._storage.put_service_file(
                        pdf_path,
                        user_id=user_id,
                        service_order_id=order_id,
                        artifact_type="pdf",
                        attempt_token=token,
                        **self._storage_profile_kwargs(),
                    )
                    if stored_pdf_key != pdf_key:
                        raise RuntimeError("storage returned an unexpected PDF key")
                    await self._assert_owned_lease(order_id, token)
            except EssayLeaseBusy:
                await self._cleanup_attempt_artifacts(uploaded_keys, user_id=user_id)
                raise
            except Exception as exc:
                await self._cleanup_attempt_artifacts(uploaded_keys, user_id=user_id)
                await self._release_retryable(
                    order_id,
                    token,
                    error_code="ARTIFACT_STORAGE_FAILED",
                    user_message="Tayyor faylni xavfsiz saqlab bo‘lmadi",
                )
                raise EssayRetryableError("artifact storage failed") from exc

            try:
                await self._mark_ready(
                    order_id,
                    token,
                    docx_key=docx_key,
                    pdf_key=pdf_key,
                    pdf_error=pdf_error,
                    docx_size=docx_path.stat().st_size,
                    pdf_size=pdf_path.stat().st_size if pdf_path is not None else None,
                    generation_metadata_updates=(
                        {"canonical_parity_score": artifact_parity_score}
                        if artifact_parity_score is not None
                        else None
                    ),
                )
            except EssayLeaseBusy:
                await self._cleanup_attempt_artifacts(uploaded_keys, user_id=user_id)
                raise
        finally:
            await asyncio.to_thread(shutil.rmtree, work_dir, True)
            with suppress(OSError):
                order_root.rmdir()

    async def _acquire(
        self, order_id: int
    ) -> (
        tuple[
            str,
            int,
            EssayGenerationRequest
            | IndependentWorkGenerationRequest
            | CourseWorkGenerationRequest
            | SummaryGenerationRequest
            | QuizGenerationRequest
            | WriteTextGenerationRequest
            | RewriteGenerationRequest
            | TranslationGenerationRequest,
            list[str],
        ]
        | None
    ):
        if order_id <= 0:
            raise ValueError("order_id must be positive")
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            order = await session.scalar(
                select(ServiceOrder).where(ServiceOrder.id == order_id).with_for_update()
            )
            if order is None or order.service_type != self._profile.service_type.value:
                raise EssayTerminalError("document service order not found")
            if order.status in {
                ServiceOrderStatus.ARTIFACTS_READY,
                ServiceOrderStatus.DELIVERED,
            }:
                return None
            if order.status in {ServiceOrderStatus.FAILED, ServiceOrderStatus.CANCELLED}:
                raise EssayTerminalError("document service order is terminal")
            if (
                order.generation_lease_token
                and order.generation_lease_expires_at is not None
                and _as_utc(order.generation_lease_expires_at) > now
            ):
                raise EssayLeaseBusy("generation lease is active")
            token = uuid4().hex
            pending_keys = [
                str(key)
                for key in order.generation_metadata.get("pending_artifact_keys", [])
                if isinstance(key, str)
            ]
            order.generation_lease_token = token
            order.generation_lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            order.generation_attempts += 1
            order.started_at = order.started_at or now
            order.status = ServiceOrderStatus.GENERATING
            order.error_code = None
            order.error_message = None
            if self._profile.service_type is ServiceType.SUMMARY:
                summary_request = SummaryGenerationRequest(
                    source=str(order.normalized_request["source"]),
                    detail=str(order.normalized_request["detail"]),  # type: ignore[arg-type]
                    language=str(order.normalized_request["language"]),  # type: ignore[arg-type]
                    additional_requirements=(
                        str(order.normalized_request["requirements"])
                        if order.normalized_request.get("requirements")
                        else None
                    ),
                )
                return token, order.user_id, summary_request, pending_keys
            if self._profile.service_type is ServiceType.QUIZ:
                quiz_request = QuizGenerationRequest(
                    topic=str(order.normalized_request["topic"]),
                    question_count=int(order.normalized_request["question_count"]),
                    difficulty=str(order.normalized_request["difficulty"]),  # type: ignore[arg-type]
                    option_count=int(order.normalized_request["option_count"]),
                    answer_key=bool(order.normalized_request["answer_key"]),
                    language=str(order.normalized_request["language"]),  # type: ignore[arg-type]
                    additional_requirements=(
                        str(order.normalized_request["requirements"])
                        if order.normalized_request.get("requirements")
                        else None
                    ),
                )
                return token, order.user_id, quiz_request, pending_keys
            if self._profile.service_type is ServiceType.WRITE_TEXT:
                write_request = WriteTextGenerationRequest(
                    topic=str(order.normalized_request["topic"]),
                    text_type=str(order.normalized_request["text_type"]),  # type: ignore[arg-type]
                    style=str(order.normalized_request["style"]),  # type: ignore[arg-type]
                    length=str(order.normalized_request["length"]),  # type: ignore[arg-type]
                    language=str(order.normalized_request["language"]),  # type: ignore[arg-type]
                    additional_requirements=(
                        str(order.normalized_request["requirements"])
                        if order.normalized_request.get("requirements")
                        else None
                    ),
                )
                return token, order.user_id, write_request, pending_keys
            if self._profile.service_type is ServiceType.REWRITE:
                rewrite_request = RewriteGenerationRequest(
                    text=str(order.normalized_request["text"]),
                    mode=str(order.normalized_request["mode"]),  # type: ignore[arg-type]
                )
                return token, order.user_id, rewrite_request, pending_keys
            if self._profile.service_type is ServiceType.TRANSLATE:
                translation_request = TranslationGenerationRequest(
                    text=str(order.normalized_request["text"]),
                    source_language=str(order.normalized_request["source_language"]),  # type: ignore[arg-type]
                    target_language=str(order.normalized_request["target_language"]),  # type: ignore[arg-type]
                )
                return token, order.user_id, translation_request, pending_keys
            common = {
                "topic": str(order.normalized_request["topic"]),
                "language": str(order.normalized_request["language"]),
                "length": str(order.normalized_request["length"]),
                "additional_requirements": (
                    str(order.normalized_request["requirements"])
                    if order.normalized_request.get("requirements")
                    else None
                ),
            }
            request: (
                EssayGenerationRequest
                | IndependentWorkGenerationRequest
                | CourseWorkGenerationRequest
            )
            if self._profile.service_type is ServiceType.ESSAY:
                request = EssayGenerationRequest(**common)  # type: ignore[arg-type]
            elif self._profile.service_type is ServiceType.INDEPENDENT_WORK:
                request = IndependentWorkGenerationRequest(
                    subject=str(order.normalized_request["subject"]),
                    **common,  # type: ignore[arg-type]
                )
            else:
                request = CourseWorkGenerationRequest(
                    subject=str(order.normalized_request["subject"]),
                    sections=str(order.normalized_request["sections"]),
                    **common,  # type: ignore[arg-type]
                )
            return token, order.user_id, request, pending_keys

    async def _mark_rendering(
        self,
        order_id: int,
        token: str,
        *,
        generation_metadata: dict[str, object],
    ) -> None:
        async with self._session_factory() as session, session.begin():
            order = await self._owned_lease(session, order_id, token)
            order.status = ServiceOrderStatus.DOCUMENT_RENDERING
            metadata = dict(order.generation_metadata)
            metadata.update(generation_metadata)
            order.generation_metadata = metadata

    async def _assert_owned_lease(self, order_id: int, token: str) -> None:
        async with self._session_factory() as session, session.begin():
            await self._owned_lease(session, order_id, token)

    async def _register_pending_artifacts(self, order_id: int, token: str, keys: list[str]) -> None:
        async with self._session_factory() as session, session.begin():
            order = await self._owned_lease(session, order_id, token)
            metadata = dict(order.generation_metadata)
            metadata["pending_artifact_keys"] = list(keys)
            order.generation_metadata = metadata

    async def _clear_pending_artifacts(self, order_id: int, token: str) -> None:
        async with self._session_factory() as session, session.begin():
            order = await self._owned_lease(session, order_id, token)
            metadata = dict(order.generation_metadata)
            pending = metadata.pop("pending_artifact_keys", [])
            candidates = [
                str(key)
                for key in metadata.get("orphan_candidate_keys", [])
                if isinstance(key, str)
            ]
            metadata["orphan_candidate_keys"] = list(
                dict.fromkeys([*candidates, *[str(key) for key in pending]])
            )
            order.generation_metadata = metadata

    async def _cleanup_attempt_artifacts(self, keys: list[str], *, user_id: int) -> None:
        for key in keys:
            await self._storage.delete_for_user(key, user_id=user_id)

    def _storage_profile_kwargs(self) -> dict[str, str]:
        if self._profile.service_type is ServiceType.ESSAY:
            return {}
        return {"service_type": self._profile.service_type.value}

    async def _mark_ready(
        self,
        order_id: int,
        token: str,
        *,
        docx_key: str,
        pdf_key: str | None,
        pdf_error: str | None,
        docx_size: int,
        pdf_size: int | None,
        generation_metadata_updates: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            order = await self._owned_lease(session, order_id, token)
            order.docx_storage_key = docx_key
            order.pdf_storage_key = pdf_key
            order.pdf_error = pdf_error
            generation_metadata = dict(order.generation_metadata)
            generation_metadata.pop("pending_artifact_keys", None)
            generation_metadata.update(generation_metadata_updates or {})
            orphan_candidates = [
                str(key)
                for key in generation_metadata.get("orphan_candidate_keys", [])
                if isinstance(key, str)
            ]
            order.generation_metadata = generation_metadata
            order.result_metadata = {
                "docx": {"available": True, "bytes": docx_size},
                "pdf": {"available": pdf_key is not None, "bytes": pdf_size},
            }
            order.completed_at = now
            order.status = ServiceOrderStatus.ARTIFACTS_READY
            order.generation_lease_token = None
            order.generation_lease_expires_at = None
            order.error_code = None
            order.error_message = None
            if orphan_candidates:
                await _schedule_orphan_cleanup(
                    session, order, available_at=now + timedelta(hours=24)
                )

    async def _release_retryable(
        self,
        order_id: int,
        token: str,
        *,
        error_code: str,
        user_message: str,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            order = await self._owned_lease(session, order_id, token)
            order.status = ServiceOrderStatus.QUEUED
            order.generation_lease_token = None
            order.generation_lease_expires_at = None
            order.retry_count += 1
            order.error_code = error_code
            order.error_message = user_message

    async def _mark_terminal(
        self,
        order_id: int,
        token: str,
        *,
        error_code: str,
        user_message: str,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            order = await self._owned_lease(session, order_id, token)
            order.status = ServiceOrderStatus.FAILED
            order.generation_lease_token = None
            order.generation_lease_expires_at = None
            order.retry_count += 1
            order.error_code = error_code
            order.error_message = user_message
            await _schedule_orphan_cleanup(
                session, order, available_at=datetime.now(UTC) + timedelta(hours=24)
            )

    @staticmethod
    async def _owned_lease(session: AsyncSession, order_id: int, token: str) -> ServiceOrder:
        order = await session.scalar(
            select(ServiceOrder).where(ServiceOrder.id == order_id).with_for_update()
        )
        if order is None or order.generation_lease_token != token:
            raise EssayLeaseBusy("generation lease was lost")
        return order


class IndependentWorkPipeline(EssayPipeline):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: AIProvider,
        storage: Storage,
        *,
        temp_root: Path,
        pdf_converter: PdfConverter,
        renderer: IndependentWorkDocumentRenderer | None = None,
        lease_seconds: int = 900,
        pdf_timeout_seconds: int = 120,
    ) -> None:
        super().__init__(
            session_factory,
            provider,
            storage,
            temp_root=temp_root,
            pdf_converter=pdf_converter,
            renderer=renderer,
            service_type=ServiceType.INDEPENDENT_WORK,
            lease_seconds=lease_seconds,
            pdf_timeout_seconds=pdf_timeout_seconds,
        )


class CourseWorkPipeline(EssayPipeline):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: AIProvider,
        storage: Storage,
        *,
        temp_root: Path,
        pdf_converter: PdfConverter,
        renderer: CourseWorkDocumentRenderer | None = None,
        lease_seconds: int = 900,
        pdf_timeout_seconds: int = 120,
    ) -> None:
        super().__init__(
            session_factory,
            provider,
            storage,
            temp_root=temp_root,
            pdf_converter=pdf_converter,
            renderer=renderer,
            service_type=ServiceType.COURSE_WORK,
            lease_seconds=lease_seconds,
            pdf_timeout_seconds=pdf_timeout_seconds,
        )


class SummaryPipeline(EssayPipeline):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: AIProvider,
        storage: Storage,
        *,
        temp_root: Path,
        pdf_converter: PdfConverter,
        renderer: SummaryDocumentRenderer | None = None,
        lease_seconds: int = 900,
        pdf_timeout_seconds: int = 120,
    ) -> None:
        super().__init__(
            session_factory,
            provider,
            storage,
            temp_root=temp_root,
            pdf_converter=pdf_converter,
            renderer=renderer,
            service_type=ServiceType.SUMMARY,
            lease_seconds=lease_seconds,
            pdf_timeout_seconds=pdf_timeout_seconds,
        )


class WriteTextPipeline(EssayPipeline):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: AIProvider,
        storage: Storage,
        *,
        temp_root: Path,
        pdf_converter: PdfConverter,
        renderer: WriteTextDocumentRenderer | None = None,
        lease_seconds: int = 900,
        pdf_timeout_seconds: int = 120,
    ) -> None:
        super().__init__(
            session_factory,
            provider,
            storage,
            temp_root=temp_root,
            pdf_converter=pdf_converter,
            renderer=renderer,
            service_type=ServiceType.WRITE_TEXT,
            lease_seconds=lease_seconds,
            pdf_timeout_seconds=pdf_timeout_seconds,
        )


class RewritePipeline(EssayPipeline):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: AIProvider,
        storage: Storage,
        *,
        temp_root: Path,
        pdf_converter: PdfConverter,
        renderer: RewriteDocumentRenderer | None = None,
        lease_seconds: int = 900,
        pdf_timeout_seconds: int = 120,
    ) -> None:
        super().__init__(
            session_factory,
            provider,
            storage,
            temp_root=temp_root,
            pdf_converter=pdf_converter,
            renderer=renderer,
            service_type=ServiceType.REWRITE,
            lease_seconds=lease_seconds,
            pdf_timeout_seconds=pdf_timeout_seconds,
        )


class TranslationPipeline(EssayPipeline):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: AIProvider,
        storage: Storage,
        *,
        temp_root: Path,
        pdf_converter: PdfConverter,
        renderer: TranslationDocumentRenderer | None = None,
        lease_seconds: int = 900,
        pdf_timeout_seconds: int = 120,
    ) -> None:
        super().__init__(
            session_factory,
            provider,
            storage,
            temp_root=temp_root,
            pdf_converter=pdf_converter,
            renderer=renderer,
            service_type=ServiceType.TRANSLATE,
            lease_seconds=lease_seconds,
            pdf_timeout_seconds=pdf_timeout_seconds,
        )


class QuizPipeline(EssayPipeline):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: AIProvider,
        storage: Storage,
        *,
        temp_root: Path,
        pdf_converter: PdfConverter,
        renderer: QuizDocumentRenderer | None = None,
        lease_seconds: int = 900,
        pdf_timeout_seconds: int = 120,
    ) -> None:
        super().__init__(
            session_factory,
            provider,
            storage,
            temp_root=temp_root,
            pdf_converter=pdf_converter,
            renderer=renderer,
            service_type=ServiceType.QUIZ,
            lease_seconds=lease_seconds,
            pdf_timeout_seconds=pdf_timeout_seconds,
        )


async def cleanup_document_orphan_artifacts(
    session_factory: async_sessionmaker[AsyncSession],
    storage: Storage,
    *,
    order_id: int,
    service_type: ServiceType,
) -> bool:
    async with session_factory() as session, session.begin():
        order = await session.get(ServiceOrder, order_id, with_for_update=True)
        if order is None or order.service_type != service_type.value:
            return True
        metadata = dict(order.generation_metadata)
        candidates = [
            str(key) for key in metadata.get("orphan_candidate_keys", []) if isinstance(key, str)
        ]
        canonical_keys = {order.docx_storage_key, order.pdf_storage_key, None}
        for key in candidates:
            if key not in canonical_keys:
                await storage.delete_for_user(key, user_id=order.user_id)
        metadata.pop("orphan_candidate_keys", None)
        order.generation_metadata = metadata
    return True


async def cleanup_essay_orphan_artifacts(
    session_factory: async_sessionmaker[AsyncSession],
    storage: Storage,
    *,
    order_id: int,
) -> bool:
    return await cleanup_document_orphan_artifacts(
        session_factory,
        storage,
        order_id=order_id,
        service_type=ServiceType.ESSAY,
    )


async def cleanup_independent_work_orphan_artifacts(
    session_factory: async_sessionmaker[AsyncSession],
    storage: Storage,
    *,
    order_id: int,
) -> bool:
    return await cleanup_document_orphan_artifacts(
        session_factory,
        storage,
        order_id=order_id,
        service_type=ServiceType.INDEPENDENT_WORK,
    )


async def cleanup_course_work_orphan_artifacts(
    session_factory: async_sessionmaker[AsyncSession],
    storage: Storage,
    *,
    order_id: int,
) -> bool:
    return await cleanup_document_orphan_artifacts(
        session_factory,
        storage,
        order_id=order_id,
        service_type=ServiceType.COURSE_WORK,
    )


async def cleanup_summary_orphan_artifacts(
    session_factory: async_sessionmaker[AsyncSession],
    storage: Storage,
    *,
    order_id: int,
) -> bool:
    return await cleanup_document_orphan_artifacts(
        session_factory,
        storage,
        order_id=order_id,
        service_type=ServiceType.SUMMARY,
    )


async def cleanup_quiz_orphan_artifacts(
    session_factory: async_sessionmaker[AsyncSession],
    storage: Storage,
    *,
    order_id: int,
) -> bool:
    return await cleanup_document_orphan_artifacts(
        session_factory,
        storage,
        order_id=order_id,
        service_type=ServiceType.QUIZ,
    )


async def cleanup_write_text_orphan_artifacts(
    session_factory: async_sessionmaker[AsyncSession],
    storage: Storage,
    *,
    order_id: int,
) -> bool:
    return await cleanup_document_orphan_artifacts(
        session_factory,
        storage,
        order_id=order_id,
        service_type=ServiceType.WRITE_TEXT,
    )


async def cleanup_rewrite_orphan_artifacts(
    session_factory: async_sessionmaker[AsyncSession],
    storage: Storage,
    *,
    order_id: int,
) -> bool:
    return await cleanup_document_orphan_artifacts(
        session_factory,
        storage,
        order_id=order_id,
        service_type=ServiceType.REWRITE,
    )


async def cleanup_translation_orphan_artifacts(
    session_factory: async_sessionmaker[AsyncSession],
    storage: Storage,
    *,
    order_id: int,
) -> bool:
    return await cleanup_document_orphan_artifacts(
        session_factory,
        storage,
        order_id=order_id,
        service_type=ServiceType.TRANSLATE,
    )


async def mark_terminal_document_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    order_id: int,
    service_type: ServiceType,
    error_code: str = "GENERATION_RETRIES_EXHAUSTED",
    user_message: str = "Akademik ishni tayyorlashda xatolik yuz berdi. Keyinroq qayta urinib ko‘ring.",
) -> bool:
    """Persist an exhausted retry without exposing provider or filesystem details."""

    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        order = await session.scalar(
            select(ServiceOrder).where(ServiceOrder.id == order_id).with_for_update()
        )
        if order is None or order.service_type != service_type.value:
            return False
        if order.status in {
            ServiceOrderStatus.ARTIFACTS_READY,
            ServiceOrderStatus.DELIVERED,
            ServiceOrderStatus.FAILED,
            ServiceOrderStatus.CANCELLED,
        }:
            return True
        expires_at = order.generation_lease_expires_at
        if expires_at is not None and _as_utc(expires_at) > now:
            return False
        order.status = ServiceOrderStatus.FAILED
        order.generation_lease_token = None
        order.generation_lease_expires_at = None
        order.error_code = error_code
        order.error_message = user_message
        await _schedule_orphan_cleanup(session, order, available_at=now + timedelta(hours=24))
        return True


async def mark_terminal_essay_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    order_id: int,
    error_code: str = "GENERATION_RETRIES_EXHAUSTED",
    user_message: str = "Referatni tayyorlashda xatolik yuz berdi. Keyinroq qayta urinib ko‘ring.",
) -> bool:
    return await mark_terminal_document_failure(
        session_factory,
        order_id=order_id,
        service_type=ServiceType.ESSAY,
        error_code=error_code,
        user_message=user_message,
    )


async def mark_terminal_independent_work_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    order_id: int,
    error_code: str = "GENERATION_RETRIES_EXHAUSTED",
    user_message: str = "Mustaqil ishni tayyorlashda xatolik yuz berdi. Keyinroq qayta urinib ko‘ring.",
) -> bool:
    return await mark_terminal_document_failure(
        session_factory,
        order_id=order_id,
        service_type=ServiceType.INDEPENDENT_WORK,
        error_code=error_code,
        user_message=user_message,
    )


async def mark_terminal_course_work_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    order_id: int,
    error_code: str = "GENERATION_RETRIES_EXHAUSTED",
    user_message: str = "Kurs ishini tayyorlashda xatolik yuz berdi. Keyinroq qayta urinib ko‘ring.",
) -> bool:
    return await mark_terminal_document_failure(
        session_factory,
        order_id=order_id,
        service_type=ServiceType.COURSE_WORK,
        error_code=error_code,
        user_message=user_message,
    )


async def mark_terminal_summary_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    order_id: int,
    error_code: str = "GENERATION_RETRIES_EXHAUSTED",
    user_message: str = "Konspektni tayyorlashda xatolik yuz berdi. Keyinroq qayta urinib ko‘ring.",
) -> bool:
    return await mark_terminal_document_failure(
        session_factory,
        order_id=order_id,
        service_type=ServiceType.SUMMARY,
        error_code=error_code,
        user_message=user_message,
    )


async def mark_terminal_quiz_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    order_id: int,
    error_code: str = "GENERATION_RETRIES_EXHAUSTED",
    user_message: str = "Quizni tayyorlashda xatolik yuz berdi. Keyinroq qayta urinib ko‘ring.",
) -> bool:
    return await mark_terminal_document_failure(
        session_factory,
        order_id=order_id,
        service_type=ServiceType.QUIZ,
        error_code=error_code,
        user_message=user_message,
    )


async def mark_terminal_write_text_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    order_id: int,
    error_code: str = "GENERATION_RETRIES_EXHAUSTED",
    user_message: str = "Matnni tayyorlashda xatolik yuz berdi. Keyinroq qayta urinib ko‘ring.",
) -> bool:
    return await mark_terminal_document_failure(
        session_factory,
        order_id=order_id,
        service_type=ServiceType.WRITE_TEXT,
        error_code=error_code,
        user_message=user_message,
    )


async def mark_terminal_rewrite_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    order_id: int,
    error_code: str = "GENERATION_RETRIES_EXHAUSTED",
    user_message: str = "Matnni qayta yozishda xatolik yuz berdi. Keyinroq qayta urinib ko‘ring.",
) -> bool:
    return await mark_terminal_document_failure(
        session_factory,
        order_id=order_id,
        service_type=ServiceType.REWRITE,
        error_code=error_code,
        user_message=user_message,
    )


async def mark_terminal_translation_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    order_id: int,
    error_code: str = "GENERATION_RETRIES_EXHAUSTED",
    user_message: str = "Tarjimani tayyorlashda xatolik yuz berdi. Keyinroq qayta urinib ko‘ring.",
) -> bool:
    return await mark_terminal_document_failure(
        session_factory,
        order_id=order_id,
        service_type=ServiceType.TRANSLATE,
        error_code=error_code,
        user_message=user_message,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
