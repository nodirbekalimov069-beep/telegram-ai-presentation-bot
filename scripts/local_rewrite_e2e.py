"""Run a real REWRITE order through outbox, worker, storage and Telegram."""

import argparse
import asyncio
import hashlib
import json
import re
import tempfile
import time
import uuid
from collections import Counter
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

_TOKEN_RE = re.compile(r"[^\W_]+(?:[’'ʼ-][^\W_]+)*", re.UNICODE)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _tokens(value: str) -> Counter[str]:
    return Counter(match.group(0).casefold() for match in _TOKEN_RE.finditer(value))


def _validate_docx(path: Path, *, source: str, required_fragments: list[str]) -> dict[str, object]:
    if path.read_bytes()[:2] != b"PK":
        raise RuntimeError("REWRITE DOCX has invalid ZIP magic bytes")
    paragraphs = [
        paragraph.text.strip()
        for paragraph in Document(str(path)).paragraphs
        if paragraph.text.strip()
    ]
    if len(paragraphs) < 3:
        raise RuntimeError("REWRITE DOCX is empty, title-only, or missing authored content")
    rewritten = "\n".join(paragraphs[2:])
    if len(rewritten) < max(40, int(len(source) * 0.55)):
        raise RuntimeError("REWRITE DOCX has insufficient substantive content")
    if _normalized(rewritten) == _normalized(source):
        raise RuntimeError("REWRITE artifact did not materially transform the source")
    lowered = _normalized(rewritten)
    for fragment in required_fragments:
        if _normalized(fragment) not in lowered:
            raise RuntimeError(f"protected fragment was not preserved: {fragment}")
    return {
        "bytes": path.stat().st_size,
        "characters": len(rewritten),
        "paragraphs": len(paragraphs),
        "protected_fragments": len(required_fragments),
        "sha256": _sha256(path),
        "_all_content": "\n".join(paragraphs),
    }


async def _run(args: argparse.Namespace) -> dict[str, object]:
    settings = Settings()
    if settings.owner_telegram_id is None:
        raise RuntimeError("OWNER_TELEGRAM_ID is required for owner-only REWRITE E2E")
    telegram_user_id = settings.owner_telegram_id
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    storage = build_storage(settings)
    idempotency_key = f"rewrite-e2e:{uuid.uuid4().hex}"
    request_payload = {"text": args.source, "mode": args.mode}
    try:
        async with factory() as session:
            user = await UserService().register(
                session,
                telegram_user_id=telegram_user_id,
                username="rewrite_e2e",
                first_name="Rewrite E2E",
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
                        OutboxEvent.event_type == "service.rewrite.requested"
                    )
                )
                or 0
            )
        if before_orders != 0:
            raise RuntimeError("pre-confirmation barrier was not clean")

        async with factory() as session:
            order = await ServiceOrderService().create_rewrite_order(
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
                        OutboxEvent.event_type == "service.rewrite.requested"
                    )
                )
                or 0
            )
        if after_events != before_events + 1:
            raise RuntimeError("confirmed submit did not create exactly one REWRITE outbox event")
        async with factory() as session:
            duplicate = await ServiceOrderService().create_rewrite_order(
                session,
                telegram_user_id=telegram_user_id,
                request_payload=request_payload,
                idempotency_key=idempotency_key,
            )
        if duplicate.id != order_id:
            raise RuntimeError("idempotent submit created a duplicate REWRITE order")

        deadline = time.monotonic() + args.timeout
        last_status = "QUEUED"
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            async with factory() as session:
                current = await session.get(ServiceOrder, order_id)
                if current is None:
                    raise RuntimeError("REWRITE order disappeared")
                last_status = str(current.status)
                if current.status == ServiceOrderStatus.DELIVERED:
                    docx_key, pdf_key = current.docx_storage_key, current.pdf_storage_key
                    docx_message_id = current.docx_telegram_message_id
                    pdf_message_id = current.pdf_telegram_message_id
                    generation_attempts = current.generation_attempts
                    delivery_attempts = current.delivery_attempts
                    generation_metadata = dict(current.generation_metadata)
                    result_metadata = dict(current.result_metadata)
                    break
                if current.status == ServiceOrderStatus.FAILED:
                    raise RuntimeError(
                        f"REWRITE failed safely: {current.error_code}: {current.error_message}"
                    )
        else:
            raise TimeoutError(f"REWRITE E2E timed out in status {last_status}")

        if not all((docx_key, pdf_key, docx_message_id, pdf_message_id)):
            raise RuntimeError("REWRITE DOCX/PDF were not persisted and delivered")
        if generation_attempts != 1 or delivery_attempts != 1:
            raise RuntimeError(
                f"unexpected attempts generation={generation_attempts} delivery={delivery_attempts}"
            )
        if generation_metadata.get("reference_count") != 0:
            raise RuntimeError("unverified REWRITE references were persisted")

        with tempfile.TemporaryDirectory(prefix=f"rewrite-e2e-{order_id}-") as directory:
            root = Path(directory)
            docx_path = await storage.download_for_user(
                docx_key, root / f"qayta_yozish_{order_id}.docx", user_id=user_id
            )
            docx_result = _validate_docx(
                docx_path,
                source=args.source,
                required_fragments=args.required_fragment,
            )
            pdf_path = await storage.download_for_user(
                pdf_key, root / f"qayta_yozish_{order_id}.pdf", user_id=user_id
            )
            if pdf_path.read_bytes()[:5] != b"%PDF-":
                raise RuntimeError("REWRITE PDF has invalid magic bytes")
            pdf = PdfReader(str(pdf_path), strict=True)
            if not pdf.pages:
                raise RuntimeError("REWRITE PDF has no pages")
            page_texts = [(page.extract_text() or "").strip() for page in pdf.pages]
            if any(len(value) < 20 for value in page_texts):
                raise RuntimeError("REWRITE PDF has a blank or nearly blank page")
            extracted = "\n".join(page_texts)
            docx_tokens = _tokens(str(docx_result["_all_content"]))
            pdf_tokens = _tokens(extracted)
            overlap = sum((docx_tokens & pdf_tokens).values())
            parity = overlap / max(1, sum(docx_tokens.values()), sum(pdf_tokens.values()))
            if parity != 1.0:
                raise RuntimeError(f"REWRITE DOCX/PDF normalized semantic parity was {parity}")
            docx_result.pop("_all_content")
            pdf_result = {
                "bytes": pdf_path.stat().st_size,
                "pages": len(pdf.pages),
                "characters": len(extracted),
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
                raise RuntimeError("cross-user REWRITE artifact access was not denied")

        async with factory() as session, session.begin():
            replay_event = await session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.event_type == "service.rewrite.requested",
                    OutboxEvent.aggregate_id == str(order_id),
                )
                .with_for_update()
            )
            if replay_event is None:
                raise RuntimeError("REWRITE transactional outbox event is missing")
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
            raise RuntimeError("outbox replay changed delivered REWRITE result")

        successors = (ServiceType.TRANSLATE, ServiceType.DOCUMENT)
        closed = all(
            not SERVICE_CATALOG[item].production_ready
            and item.value not in PRODUCTION_DOCUMENT_SERVICE_TYPES
            for item in successors
        )
        if not closed:
            raise RuntimeError("a next preview service was opened")
        async with factory() as session:
            before_closed = int(await session.scalar(select(func.count(ServiceOrder.id))) or 0)
            for successor in successors:
                try:
                    await ServiceOrderService().create_preview_order(
                        session,
                        telegram_user_id=telegram_user_id,
                        service_type=successor,
                        request_payload=(
                            {
                                "text": "Safe source text for a closed service.",
                                "target_language": "uz",
                            }
                            if successor is ServiceType.TRANSLATE
                            else {"document_type": "application", "details": "Closed service"}
                        ),
                        idempotency_key=f"closed-e2e:{successor.value}:{uuid.uuid4().hex}",
                    )
                except PermissionError:
                    pass
                else:
                    raise RuntimeError(f"closed successor accepted direct order: {successor.value}")
            after_closed = int(await session.scalar(select(func.count(ServiceOrder.id))) or 0)
        if before_closed != after_closed:
            raise RuntimeError("closed successor created a service order")

        return {
            "service_type": "rewrite",
            "language": args.language,
            "mode": args.mode,
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
    parser.add_argument(
        "--source",
        default=(
            "Nodir AI-2026 loyihasini 12-avgust kuni https://example.com sahifasida e’lon qildi. "
            "Bu loyiha jamoa uchun umumiy reja bilan aniq natijalarni ta’minlaydi va har bir vazifani "
            "yetkazishdan oldin sinchiklab tekshiradi."
        ),
    )
    parser.add_argument("--language", choices=("uz", "ru", "en"), default="uz")
    parser.add_argument(
        "--mode", choices=("simple", "formal", "academic", "shorten", "clarify"), default="formal"
    )
    parser.add_argument(
        "--required-fragment",
        action="append",
        default=None,
    )
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if args.required_fragment is None:
        args.required_fragment = ["Nodir", "AI-2026", "12-avgust", "https://example.com"]
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
