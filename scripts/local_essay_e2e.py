"""Run a real Referat order through outbox, worker, storage and Telegram delivery."""

import argparse
import asyncio
import json
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from docx import Document
from pypdf import PdfReader
from sqlalchemy import select

from core.config import Settings
from core.database.session import create_engine, create_session_factory
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
    "uz": {"Kirish", "Xulosa", "Foydalanilgan adabiyotlar"},
    "ru": {"Введение", "Заключение", "Список литературы"},
    "en": {"Introduction", "Conclusion", "References"},
}


def _validate_docx(
    path: Path,
    *,
    language: str,
    required_phrase: str | None = None,
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
    if len(content) < 1_000:
        raise RuntimeError("DOCX contains too little text")
    if required_phrase:
        conclusion_index = paragraphs.index(_CONCLUSION_HEADINGS[language])
        references_index = paragraphs.index(_REFERENCE_HEADINGS[language])
        conclusion_content = "\n".join(paragraphs[conclusion_index + 1 : references_index])
        if required_phrase.casefold() not in conclusion_content.casefold():
            raise RuntimeError("additional requirements did not materially affect the conclusion")
    return {
        "paragraphs": len(paragraphs),
        "headings": len(headings),
        "characters": len(content),
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings()
    if settings.owner_telegram_id is None:
        raise RuntimeError("OWNER_TELEGRAM_ID is required for owner-only Referat E2E")
    telegram_user_id = settings.owner_telegram_id
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    storage = build_storage(settings)
    try:
        async with factory() as session:
            user = await UserService().register(
                session,
                telegram_user_id=telegram_user_id,
                username="referat_e2e",
                first_name="Referat E2E",
                last_name=None,
                language_code=args.language,
            )
            order = await ServiceOrderService().create_essay_order(
                session,
                telegram_user_id=telegram_user_id,
                request_payload={
                    "topic": args.topic,
                    "language": args.language,
                    "length": args.length,
                    "requirements": args.requirements,
                },
                idempotency_key=f"referat-e2e:{uuid.uuid4().hex}",
            )
            order_id = order.id
            user_id = user.id

        deadline = time.monotonic() + args.timeout
        last_status = "QUEUED"
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            async with factory() as session:
                current = await session.scalar(
                    select(ServiceOrder).where(ServiceOrder.id == order_id)
                )
                if current is None:
                    raise RuntimeError("Referat order disappeared")
                last_status = str(current.status)
                if current.status == ServiceOrderStatus.DELIVERED:
                    docx_key = current.docx_storage_key
                    pdf_key = current.pdf_storage_key
                    docx_message_id = current.docx_telegram_message_id
                    pdf_message_id = current.pdf_telegram_message_id
                    metadata = dict(current.result_metadata)
                    break
                if current.status == ServiceOrderStatus.FAILED:
                    raise RuntimeError(
                        f"Referat failed safely: {current.error_code}: {current.error_message}"
                    )
        else:
            raise TimeoutError(f"Referat E2E timed out in status {last_status}")

        if docx_key is None or docx_message_id is None:
            raise RuntimeError("DOCX was not persisted and delivered")
        with tempfile.TemporaryDirectory(prefix=f"referat-e2e-{order_id}-") as directory:
            root = Path(directory)
            docx_path = await storage.download_for_user(
                docx_key, root / f"referat_{order_id}.docx", user_id=user_id
            )
            docx_result = _validate_docx(
                docx_path,
                language=args.language,
                required_phrase=args.required_phrase,
            )
            pdf_result: dict[str, Any] | None = None
            if pdf_key is not None:
                pdf_path = await storage.download_for_user(
                    pdf_key, root / f"referat_{order_id}.pdf", user_id=user_id
                )
                pdf = PdfReader(str(pdf_path))
                if len(pdf.pages) <= 0:
                    raise RuntimeError("PDF has no pages")
                pdf_result = {"bytes": pdf_path.stat().st_size, "pages": len(pdf.pages)}
            if pdf_key is not None and pdf_message_id is None:
                raise RuntimeError("PDF exists but was not delivered")
            return {
                "order_id": order_id,
                "status": "DELIVERED",
                "docx": {"bytes": docx_path.stat().st_size, **docx_result},
                "pdf": pdf_result,
                "docx_message_id": docx_message_id,
                "pdf_message_id": pdf_message_id,
                "result_metadata": metadata,
            }
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic", default="Sun’iy intellektning O‘zbekiston ta’limidagi o‘rni")
    parser.add_argument("--language", choices=("uz", "ru", "en"), default="uz")
    parser.add_argument("--length", choices=("short", "medium", "long"), default="short")
    parser.add_argument(
        "--requirements",
        default='Xulosada "inson nazorati" iborasini aynan ishlat.',
    )
    parser.add_argument("--required-phrase", default="inson nazorati")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
