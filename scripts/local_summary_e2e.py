"""Run a real summary-note order through outbox, worker, storage and Telegram."""

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

_REQUIRED_HEADINGS = {
    "uz": {"Qisqacha mazmun", "Asosiy fikrlar", "Xulosa", "Manbalar holati"},
    "ru": {"Краткое содержание", "Основные положения", "Вывод", "Состояние источников"},
    "en": {"Overview", "Key points", "Conclusion", "Source status"},
}
_CONCLUSION_HEADINGS = {"uz": "Xulosa", "ru": "Вывод", "en": "Conclusion"}
_SOURCES_HEADINGS = {"uz": "Manbalar holati", "ru": "Состояние источников", "en": "Source status"}


def _validate_docx(
    path: Path,
    *,
    language: str,
    source_phrase: str,
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
    content = "\n".join(paragraphs)
    if len(content) < 1_200:
        raise RuntimeError("DOCX contains too little summary text")
    if source_phrase.casefold() not in content.casefold():
        raise RuntimeError("source topic did not reach the artifact")
    conclusion_index = paragraphs.index(_CONCLUSION_HEADINGS[language])
    sources_index = paragraphs.index(_SOURCES_HEADINGS[language])
    conclusion_content = "\n".join(paragraphs[conclusion_index + 1 : sources_index])
    if required_phrase.casefold() not in conclusion_content.casefold():
        raise RuntimeError("additional requirements did not materially affect the conclusion")
    if any(marker in content.casefold() for marker in ("http://", "https://", "doi:", "isbn")):
        raise RuntimeError("unverified citation escaped into the summary")
    if any(
        marker in content.casefold() for marker in ("system prompt", "service_order_id", "api_key")
    ):
        raise RuntimeError("internal data escaped into the summary")
    return {
        "paragraphs": len(paragraphs),
        "headings": len(headings),
        "characters": len(content),
        "source_present": True,
        "required_phrase_in_conclusion": True,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings()
    if settings.owner_telegram_id is None:
        raise RuntimeError("OWNER_TELEGRAM_ID is required for owner-only Summary E2E")
    telegram_user_id = settings.owner_telegram_id
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    storage = build_storage(settings)
    idempotency_key = f"summary-e2e:{uuid.uuid4().hex}"
    try:
        async with factory() as session:
            user = await UserService().register(
                session,
                telegram_user_id=telegram_user_id,
                username="summary_e2e",
                first_name="Summary E2E",
                last_name=None,
                language_code=args.language,
            )
            request_payload = {
                "source": args.source,
                "detail": args.detail,
                "language": args.language,
                "requirements": args.requirements,
            }
            order = await ServiceOrderService().create_summary_order(
                session,
                telegram_user_id=telegram_user_id,
                request_payload=request_payload,
                idempotency_key=idempotency_key,
            )
            order_id = order.id
            user_id = user.id
        async with factory() as session:
            duplicate = await ServiceOrderService().create_summary_order(
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
                    raise RuntimeError("Summary order disappeared")
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
                        f"Summary failed safely: {current.error_code}: {current.error_message}"
                    )
        else:
            raise TimeoutError(f"Summary E2E timed out in status {last_status}")

        if docx_key is None or docx_message_id is None:
            raise RuntimeError("DOCX was not persisted and delivered")
        if pdf_key is None or pdf_message_id is None:
            raise RuntimeError("validated Summary PDF was not persisted and delivered")
        with tempfile.TemporaryDirectory(prefix=f"summary-e2e-{order_id}-") as directory:
            root = Path(directory)
            docx_path = await storage.download_for_user(
                docx_key, root / f"konspekt_{order_id}.docx", user_id=user_id
            )
            docx_result = _validate_docx(
                docx_path,
                language=args.language,
                source_phrase=args.source_phrase,
                required_phrase=args.required_phrase,
            )
            pdf_path = await storage.download_for_user(
                pdf_key, root / f"konspekt_{order_id}.pdf", user_id=user_id
            )
            pdf = PdfReader(str(pdf_path), strict=True)
            if len(pdf.pages) < 2:
                raise RuntimeError("Summary PDF has fewer than two pages")
            extracted_text = " ".join((page.extract_text() or "") for page in pdf.pages)
            normalized_text = " ".join(extracted_text.casefold().split())
            if len(normalized_text) < 100 or args.source_phrase.casefold() not in normalized_text:
                raise RuntimeError("PDF extracted text failed content parity")
            pdf_result = {
                "bytes": pdf_path.stat().st_size,
                "pages": len(pdf.pages),
                "characters": len(normalized_text),
                "source_present": True,
            }
            docx_size = docx_path.stat().st_size

        async with factory() as session, session.begin():
            replay_event = await session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.event_type == "service.summary.requested",
                    OutboxEvent.aggregate_id == str(order_id),
                )
                .with_for_update()
            )
            if replay_event is None:
                raise RuntimeError("Summary transactional outbox event is missing")
            replay_event.status = "PENDING"
            replay_event.available_at = datetime.now(UTC)
            replay_event.published_at = None
        await asyncio.sleep(5)
        async with factory() as session:
            replayed = await session.get(ServiceOrder, order_id)
        if replayed is None:
            raise RuntimeError("Summary order disappeared after replay")
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
            raise RuntimeError("outbox replay changed the delivered Summary result")
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
    parser.add_argument(
        "--source",
        default=(
            "Algoritmik fikrlash murakkab vazifani boshqariladigan qismlarga ajratadi. "
            "Inson nazorati amaliy yechimni tekshiradi va aniqlangan cheklovlar natijadagi "
            "xavfni kamaytirishga yordam beradi."
        ),
    )
    parser.add_argument("--source-phrase", default="Algoritmik fikrlash")
    parser.add_argument("--language", choices=("uz", "ru", "en"), default="uz")
    parser.add_argument("--detail", choices=("short", "medium", "detailed"), default="short")
    parser.add_argument(
        "--requirements",
        default="Inson nazorati orqali amaliy xavfni kamaytirish mexanizmini alohida tushuntiring.",
    )
    parser.add_argument("--required-phrase", default="inson nazorati")
    parser.add_argument("--timeout", type=int, default=600)
    print(json.dumps(asyncio.run(_run(parser.parse_args())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
