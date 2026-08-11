"""Run a real WRITE_TEXT order through outbox, worker, storage and Telegram."""

import argparse
import asyncio
import hashlib
import json
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from docx import Document
from pypdf import PdfReader
from sqlalchemy import func, select

from core.config import Settings
from core.database.session import create_engine, create_session_factory
from core.outbox.models import OutboxEvent
from modules.files.factory import build_storage
from modules.files.storage import StorageAccessDenied
from modules.service_orders.catalog import SERVICE_CATALOG, ServiceType
from modules.service_orders.document_services import PRODUCTION_DOCUMENT_SERVICE_TYPES
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderService, ServiceOrderStatus
from modules.users.service import UserService


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _validate_docx(
    path: Path, *, topic: str, required_phrase: str, text_type: str, style: str
) -> dict[str, object]:
    if path.read_bytes()[:2] != b"PK":
        raise RuntimeError("WRITE_TEXT DOCX has invalid ZIP magic bytes")
    paragraphs = [
        paragraph.text.strip()
        for paragraph in Document(str(path)).paragraphs
        if paragraph.text.strip()
    ]
    content = "\n".join(paragraphs)
    lowered = _normalized(content)
    if _normalized(topic) not in lowered:
        raise RuntimeError("WRITE_TEXT topic did not reach DOCX")
    if _normalized(required_phrase) not in lowered:
        raise RuntimeError("WRITE_TEXT requirement evidence is missing from DOCX")
    if len(content) < 500:
        raise RuntimeError("WRITE_TEXT DOCX has insufficient meaningful content")
    type_labels = {
        "article": ("maqola", "статья", "article"),
        "post": ("post", "публикация"),
        "description": ("tavsif", "описание", "description"),
        "letter": ("xat", "письмо", "letter"),
    }
    style_labels = {
        "simple": ("sodda", "простой", "simple"),
        "formal": ("rasmiy", "официальный", "formal"),
        "academic": ("akademik", "академический", "academic"),
        "creative": ("ijodiy", "творческий", "creative"),
    }
    if not any(label in lowered for label in type_labels[text_type]):
        raise RuntimeError("WRITE_TEXT type evidence is missing from DOCX")
    if not any(label in lowered for label in style_labels[style]):
        raise RuntimeError("WRITE_TEXT style evidence is missing from DOCX")
    forbidden = (
        "http://",
        "https://",
        "doi:",
        "isbn",
        "system prompt",
        "api_key",
        "references",
        "foydalanilgan manbalar",
    )
    if any(marker in lowered for marker in forbidden):
        raise RuntimeError("unsafe or unverified content escaped into WRITE_TEXT DOCX")
    return {
        "bytes": path.stat().st_size,
        "characters": len(content),
        "paragraphs": len(paragraphs),
        "topic_present": True,
        "requirement_present": True,
        "type_present": True,
        "style_present": True,
        "sha256": _sha256(path),
        "_content": content,
    }


async def _run(args: argparse.Namespace) -> dict[str, object]:
    settings = Settings()
    if settings.owner_telegram_id is None:
        raise RuntimeError("OWNER_TELEGRAM_ID is required for owner-only WRITE_TEXT E2E")
    telegram_user_id = settings.owner_telegram_id
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    storage = build_storage(settings)
    idempotency_key = f"write-e2e:{uuid.uuid4().hex}"
    request_payload = {
        "topic": args.topic,
        "text_type": args.text_type,
        "style": args.style,
        "length": args.length,
        "language": args.language,
        "requirements": args.requirements,
    }
    try:
        async with factory() as session:
            user = await UserService().register(
                session,
                telegram_user_id=telegram_user_id,
                username="write_text_e2e",
                first_name="Write Text E2E",
                last_name=None,
                language_code=args.language,
            )
            user_id = user.id
            before_orders = int(
                await session.scalar(
                    select(func.count(ServiceOrder.id)).where(
                        ServiceOrder.idempotency_key == idempotency_key
                    )
                )
                or 0
            )
            before_events = int(
                await session.scalar(
                    select(func.count(OutboxEvent.id)).where(
                        OutboxEvent.event_type == "service.write.requested"
                    )
                )
                or 0
            )
        if before_orders:
            raise RuntimeError("pre-confirmation barrier was not clean")

        # This call represents the explicit confirmed submit. No order/outbox exists above it.
        async with factory() as session:
            order = await ServiceOrderService().create_write_text_order(
                session,
                telegram_user_id=telegram_user_id,
                request_payload=request_payload,
                idempotency_key=idempotency_key,
            )
            order_id = order.id
        async with factory() as session:
            after_events = int(
                await session.scalar(
                    select(func.count(OutboxEvent.id)).where(
                        OutboxEvent.event_type == "service.write.requested"
                    )
                )
                or 0
            )
        if after_events != before_events + 1:
            raise RuntimeError(
                "confirmed submit did not create exactly one WRITE_TEXT outbox event"
            )
        async with factory() as session:
            duplicate = await ServiceOrderService().create_write_text_order(
                session,
                telegram_user_id=telegram_user_id,
                request_payload=request_payload,
                idempotency_key=idempotency_key,
            )
        if duplicate.id != order_id:
            raise RuntimeError("idempotent submit created a duplicate WRITE_TEXT order")

        deadline = time.monotonic() + args.timeout
        last_status = "QUEUED"
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            async with factory() as session:
                current = await session.get(ServiceOrder, order_id)
                if current is None:
                    raise RuntimeError("WRITE_TEXT order disappeared")
                last_status = str(current.status)
                if current.status == ServiceOrderStatus.DELIVERED:
                    docx_key, pdf_key = current.docx_storage_key, current.pdf_storage_key
                    docx_message_id, pdf_message_id = (
                        current.docx_telegram_message_id,
                        current.pdf_telegram_message_id,
                    )
                    generation_attempts, delivery_attempts = (
                        current.generation_attempts,
                        current.delivery_attempts,
                    )
                    generation_metadata = dict(current.generation_metadata)
                    result_metadata = dict(current.result_metadata)
                    break
                if current.status == ServiceOrderStatus.FAILED:
                    raise RuntimeError(
                        f"WRITE_TEXT failed safely: {current.error_code}: {current.error_message}"
                    )
        else:
            raise TimeoutError(f"WRITE_TEXT E2E timed out in status {last_status}")
        if not all((docx_key, pdf_key, docx_message_id, pdf_message_id)):
            raise RuntimeError("WRITE_TEXT DOCX/PDF were not persisted and delivered")
        if generation_attempts != 1 or delivery_attempts != 1:
            raise RuntimeError(
                f"unexpected attempts generation={generation_attempts} delivery={delivery_attempts}"
            )

        with tempfile.TemporaryDirectory(prefix=f"write-e2e-{order_id}-") as directory:
            root = Path(directory)
            docx_path = await storage.download_for_user(
                docx_key, root / f"matn_{order_id}.docx", user_id=user_id
            )
            docx_result = _validate_docx(
                docx_path,
                topic=args.topic,
                required_phrase=args.required_phrase,
                text_type=args.text_type,
                style=args.style,
            )
            pdf_path = await storage.download_for_user(
                pdf_key, root / f"matn_{order_id}.pdf", user_id=user_id
            )
            if pdf_path.read_bytes()[:5] != b"%PDF-":
                raise RuntimeError("WRITE_TEXT PDF has invalid magic bytes")
            pdf = PdfReader(str(pdf_path), strict=True)
            if not pdf.pages:
                raise RuntimeError("WRITE_TEXT PDF has no pages")
            page_texts = [(page.extract_text() or "").strip() for page in pdf.pages]
            if any(len(value) < 20 for value in page_texts):
                raise RuntimeError("WRITE_TEXT PDF has a blank or nearly blank page")
            extracted = "\n".join(page_texts)
            if len(extracted) < 400 or _normalized(args.topic) not in _normalized(extracted):
                raise RuntimeError("WRITE_TEXT PDF content validation failed")
            if _normalized(args.required_phrase) not in _normalized(extracted):
                raise RuntimeError("WRITE_TEXT PDF requirement parity failed")
            docx_terms = set(_normalized(str(docx_result["_content"])).split())
            pdf_terms = set(_normalized(extracted).split())
            parity = len(docx_terms & pdf_terms) / max(1, len(docx_terms))
            if parity < 0.75:
                raise RuntimeError("WRITE_TEXT DOCX/PDF semantic parity failed")
            docx_result.pop("_content")
            pdf_result = {
                "bytes": pdf_path.stat().st_size,
                "pages": len(pdf.pages),
                "characters": len(extracted),
                "topic_present": True,
                "requirement_present": True,
                "semantic_parity": parity,
                "sha256": _sha256(pdf_path),
            }
            try:
                await storage.download_for_user(docx_key, root / "stolen.docx", user_id=user_id + 1)
            except StorageAccessDenied:
                cross_user_denied = True
            else:
                cross_user_denied = False
            if not cross_user_denied:
                raise RuntimeError("cross-user WRITE_TEXT artifact access was not denied")

        async with factory() as session, session.begin():
            replay_event = await session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.event_type == "service.write.requested",
                    OutboxEvent.aggregate_id == str(order_id),
                )
                .with_for_update()
            )
            if replay_event is None:
                raise RuntimeError("WRITE_TEXT transactional outbox event is missing")
            replay_event.status = "PENDING"
            replay_event.available_at = datetime.now(UTC)
            replay_event.published_at = None
        await asyncio.sleep(5)
        async with factory() as session:
            replayed = await session.get(ServiceOrder, order_id)
        unchanged = (
            replayed is not None
            and replayed.status == ServiceOrderStatus.DELIVERED
            and replayed.generation_attempts == generation_attempts
            and replayed.delivery_attempts == delivery_attempts
            and replayed.docx_storage_key == docx_key
            and replayed.pdf_storage_key == pdf_key
            and replayed.docx_telegram_message_id == docx_message_id
            and replayed.pdf_telegram_message_id == pdf_message_id
        )
        if not unchanged:
            raise RuntimeError("outbox replay changed delivered WRITE_TEXT result")
        closed = all(
            not SERVICE_CATALOG[item].production_ready
            and item.value not in PRODUCTION_DOCUMENT_SERVICE_TYPES
            for item in (ServiceType.REWRITE, ServiceType.TRANSLATE, ServiceType.DOCUMENT)
        )
        if not closed:
            raise RuntimeError("a next preview service was opened")
        if generation_metadata.get("reference_count") != 0:
            raise RuntimeError("unverified WRITE_TEXT references were persisted")
        return {
            "service_type": "write",
            "explicit_confirmation": True,
            "pre_confirmation_orders": before_orders,
            "pre_confirmation_generation_attempts": 0,
            "pre_confirmation_side_effects": 0,
            "confirmed_outbox_increment": after_events - before_events,
            "order_id": order_id,
            "status": "DELIVERED",
            "generation_attempts": generation_attempts,
            "delivery_attempts": delivery_attempts,
            "docx": docx_result,
            "pdf": pdf_result,
            "references": 0,
            "replay": "unchanged",
            "cross_user": "denied",
            "next_preview": "closed",
            "generation_metadata": generation_metadata,
            "result_metadata": result_metadata,
        }
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic", default="Mahalla hayotida hamjihatlik")
    parser.add_argument("--language", choices=("uz", "ru", "en"), default="uz")
    parser.add_argument(
        "--text-type", choices=("article", "post", "description", "letter"), default="article"
    )
    parser.add_argument(
        "--style", choices=("simple", "formal", "academic", "creative"), default="formal"
    )
    parser.add_argument("--length", choices=("short", "medium", "long"), default="short")
    parser.add_argument("--requirements", default="Amaliy hamkorlikni yoritish")
    parser.add_argument("--required-phrase", default="amaliy hamkorlik")
    parser.add_argument("--timeout", type=int, default=600)
    print(json.dumps(asyncio.run(_run(parser.parse_args())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
