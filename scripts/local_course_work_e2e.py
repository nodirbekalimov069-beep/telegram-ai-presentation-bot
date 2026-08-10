"""Run a real course-paper order through outbox, worker, storage and Telegram."""

import argparse
import asyncio
import json
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from docx import Document
from pypdf import PdfReader
from sqlalchemy import select

from core.config import Settings
from core.database.session import create_engine, create_session_factory
from core.outbox.models import OutboxEvent
from modules.files.factory import build_storage
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderService, ServiceOrderStatus
from modules.users.service import UserService

_CONCLUSION_HEADINGS = {"uz": "Xulosa", "ru": "Заключение", "en": "Conclusion"}
_REFERENCE_HEADINGS = {
    "uz": "Foydalanilgan adabiyotlar",
    "ru": "Список литературы",
    "en": "References",
}
_REQUIRED_HEADINGS = {
    "uz": {"Mundarija", "Maqsad", "Vazifalar", "Kirish", "Xulosa", "Foydalanilgan adabiyotlar"},
    "ru": {"Содержание", "Цель", "Задачи", "Введение", "Заключение", "Список литературы"},
    "en": {"Contents", "Objective", "Tasks", "Introduction", "Conclusion", "References"},
}


def _validate_docx(
    path: Path,
    *,
    language: str,
    subject: str,
    requested_sections: tuple[str, ...],
    required_phrase: str,
) -> dict[str, Any]:
    document = Document(str(path))
    paragraphs = [
        paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()
    ]
    headings = {
        paragraph.text.strip()
        for paragraph in document.paragraphs
        if paragraph.style is not None
        and paragraph.style.name.startswith("Heading")
        and paragraph.text.strip()
    }
    missing = sorted(_REQUIRED_HEADINGS[language] - headings)
    if missing:
        raise RuntimeError(f"DOCX headings missing: {missing}")
    for section in requested_sections:
        if section not in headings:
            raise RuntimeError(f"requested section missing from DOCX: {section}")
    content = "\n".join(paragraphs)
    if len(content) < 1_800:
        raise RuntimeError("DOCX contains too little course-paper text")
    if subject.casefold() not in content.casefold():
        raise RuntimeError("subject did not reach the artifact")
    conclusion_index = paragraphs.index(_CONCLUSION_HEADINGS[language])
    references_index = paragraphs.index(_REFERENCE_HEADINGS[language])
    conclusion_content = "\n".join(paragraphs[conclusion_index + 1 : references_index])
    if required_phrase.casefold() not in conclusion_content.casefold():
        raise RuntimeError("additional requirements did not materially affect the conclusion")
    references_content = "\n".join(paragraphs[references_index + 1 :])
    if any(marker in references_content.casefold() for marker in ("http://", "https://", "doi:")):
        raise RuntimeError("unverified citation escaped into references")
    return {
        "paragraphs": len(paragraphs),
        "headings": len(headings),
        "characters": len(content),
        "subject_present": True,
        "requested_sections_present": True,
        "required_phrase_in_conclusion": True,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings()
    if settings.owner_telegram_id is None:
        raise RuntimeError("OWNER_TELEGRAM_ID is required for owner-only Course Work E2E")
    telegram_user_id = settings.owner_telegram_id
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    storage = build_storage(settings)
    idempotency_key = f"course-e2e:{uuid.uuid4().hex}"
    requested_sections = tuple(item.strip() for item in args.sections.split(";") if item.strip())
    try:
        async with factory() as session:
            user = await UserService().register(
                session,
                telegram_user_id=telegram_user_id,
                username="course_e2e",
                first_name="Course E2E",
                last_name=None,
                language_code=args.language,
            )
            request_payload = {
                "subject": args.subject,
                "topic": args.topic,
                "sections": args.sections,
                "language": args.language,
                "length": args.length,
                "requirements": args.requirements,
            }
            order = await ServiceOrderService().create_course_work_order(
                session,
                telegram_user_id=telegram_user_id,
                request_payload=request_payload,
                idempotency_key=idempotency_key,
            )
            order_id = order.id
            user_id = user.id
        async with factory() as session:
            duplicate = await ServiceOrderService().create_course_work_order(
                session,
                telegram_user_id=telegram_user_id,
                request_payload=request_payload,
                idempotency_key=idempotency_key,
            )
        if duplicate.id != order_id:
            raise RuntimeError("idempotent submit created a duplicate order")

        deadline = time.monotonic() + args.timeout
        last_status = "QUEUED"
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            async with factory() as session:
                current = await session.scalar(
                    select(ServiceOrder).where(ServiceOrder.id == order_id)
                )
                if current is None:
                    raise RuntimeError("Course Work order disappeared")
                last_status = str(current.status)
                if current.status == ServiceOrderStatus.DELIVERED:
                    docx_key = current.docx_storage_key
                    pdf_key = current.pdf_storage_key
                    docx_message_id = current.docx_telegram_message_id
                    pdf_message_id = current.pdf_telegram_message_id
                    generation_attempts = current.generation_attempts
                    delivery_attempts = current.delivery_attempts
                    metadata = dict(current.result_metadata)
                    break
                if current.status == ServiceOrderStatus.FAILED:
                    raise RuntimeError(
                        f"Course Work failed safely: {current.error_code}: {current.error_message}"
                    )
        else:
            raise TimeoutError(f"Course Work E2E timed out in status {last_status}")

        if docx_key is None or docx_message_id is None:
            raise RuntimeError("DOCX was not persisted and delivered")
        if pdf_key is None or pdf_message_id is None:
            raise RuntimeError("validated Course Work PDF was not persisted and delivered")
        with tempfile.TemporaryDirectory(prefix=f"course-e2e-{order_id}-") as directory:
            root = Path(directory)
            docx_path = await storage.download_for_user(
                docx_key, root / f"kurs_ishi_{order_id}.docx", user_id=user_id
            )
            docx_result = _validate_docx(
                docx_path,
                language=args.language,
                subject=args.subject,
                requested_sections=requested_sections,
                required_phrase=args.required_phrase,
            )
            pdf_path = await storage.download_for_user(
                pdf_key, root / f"kurs_ishi_{order_id}.pdf", user_id=user_id
            )
            pdf = PdfReader(str(pdf_path), strict=True)
            if len(pdf.pages) < 4:
                raise RuntimeError("Course Work PDF has fewer than four pages")
            extracted_text = " ".join((page.extract_text() or "") for page in pdf.pages)
            normalized_text = " ".join(extracted_text.casefold().split())
            if (
                len(normalized_text) < 100
                or " ".join(args.topic.casefold().split()) not in normalized_text
            ):
                raise RuntimeError("PDF extracted text failed content parity")
            pdf_result = {
                "bytes": pdf_path.stat().st_size,
                "pages": len(pdf.pages),
                "characters": len(normalized_text),
                "topic_present": True,
            }
            docx_size = docx_path.stat().st_size

        async with factory() as session, session.begin():
            replay_event = await session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.event_type == "service.course.requested",
                    OutboxEvent.aggregate_id == str(order_id),
                )
                .with_for_update()
            )
            if replay_event is None:
                raise RuntimeError("Course Work transactional outbox event is missing")
            replay_event.status = "PENDING"
            replay_event.available_at = datetime.now(UTC)
            replay_event.published_at = None
        await asyncio.sleep(5)
        async with factory() as session:
            replayed = await session.get(ServiceOrder, order_id)
        if replayed is None:
            raise RuntimeError("Course Work order disappeared after replay")
        replay_unchanged = (
            replayed.status == ServiceOrderStatus.DELIVERED
            and replayed.generation_attempts == generation_attempts
            and replayed.delivery_attempts == delivery_attempts
            and replayed.docx_telegram_message_id == docx_message_id
            and replayed.pdf_telegram_message_id == pdf_message_id
            and replayed.docx_storage_key == docx_key
            and replayed.pdf_storage_key == pdf_key
        )
        if not replay_unchanged:
            raise RuntimeError("outbox replay changed the delivered Course Work result")
        return {
            "order_id": order_id,
            "status": "DELIVERED",
            "docx": {"bytes": docx_size, **docx_result},
            "pdf": pdf_result,
            "docx_message_id": docx_message_id,
            "pdf_message_id": pdf_message_id,
            "generation_attempts": generation_attempts,
            "delivery_attempts": delivery_attempts,
            "references": 0,
            "replay": "unchanged",
            "result_metadata": metadata,
        }
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", default="Informatika")
    parser.add_argument("--topic", default="Algoritmik fikrlashning amaliy ahamiyati")
    parser.add_argument(
        "--sections",
        default="Nazariy asoslar; Amaliy tahlil; Mas’uliyatli tavsiyalar",
    )
    parser.add_argument("--language", choices=("uz", "ru", "en"), default="uz")
    parser.add_argument("--length", choices=("short", "medium", "long"), default="short")
    parser.add_argument(
        "--requirements",
        default='Xulosada "mas’uliyatli avtomatlashtirish" iborasini aynan ishlat.',
    )
    parser.add_argument("--required-phrase", default="mas’uliyatli avtomatlashtirish")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
