"""Probe the real DOCUMENT DB/outbox/worker/storage/Telegram pipeline."""

import argparse
import asyncio
import hashlib
import json
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path
from zipfile import ZipFile

from pypdf import PdfReader
from sqlalchemy import func, select

from core.config import Settings
from core.database.session import create_engine, create_session_factory
from core.outbox.models import OutboxEvent
from modules.files.factory import build_storage
from modules.files.storage import StorageAccessDenied
from modules.service_orders.document_documents import (
    extracted_document_docx_body,
    extracted_document_pdf_text,
)
from modules.service_orders.documents import validate_docx_security
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderService, ServiceOrderStatus
from modules.users.service import UserService


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _pdf_matches_docx(pdf_text: str, docx_body: str) -> bool:
    return _normalize(pdf_text) == _normalize(docx_body)


async def _run(args: argparse.Namespace) -> dict[str, object]:
    settings = Settings()
    if settings.owner_telegram_id is None:
        raise RuntimeError("OWNER_TELEGRAM_ID is required")
    if settings.ai_provider == "mock":
        raise RuntimeError("real DOCUMENT probe refuses MockAIProvider")
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    storage = build_storage(settings)
    key = f"document-e2e:{uuid.uuid4().hex}"
    payload = {
        "document_type": args.document_type,
        "details": args.details,
        "language": args.language,
    }
    try:
        async with factory() as session:
            user = await UserService().register(
                session,
                telegram_user_id=settings.owner_telegram_id,
                username="document_e2e",
                first_name="Document E2E",
                last_name=None,
                language_code=args.ui_locale,
            )
            user_id = user.id
            barrier_orders = int(
                await session.scalar(
                    select(func.count(ServiceOrder.id)).where(ServiceOrder.idempotency_key == key)
                )
                or 0
            )
            before_events = int(
                await session.scalar(
                    select(func.count(OutboxEvent.id)).where(
                        OutboxEvent.event_type == "service.document.requested"
                    )
                )
                or 0
            )
        if barrier_orders:
            raise RuntimeError("pre-confirmation barrier was not zero")
        async with factory() as session:
            order = await ServiceOrderService().create_document_order(
                session,
                telegram_user_id=settings.owner_telegram_id,
                request_payload=payload,
                idempotency_key=key,
            )
            duplicate = await ServiceOrderService().create_document_order(
                session,
                telegram_user_id=settings.owner_telegram_id,
                request_payload=payload,
                idempotency_key=key,
            )
        if duplicate.id != order.id:
            raise RuntimeError("duplicate logical order created")
        deadline = time.monotonic() + args.timeout
        current: ServiceOrder | None = None
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            async with factory() as session:
                current = await session.get(ServiceOrder, order.id)
            if current is None:
                raise RuntimeError("order disappeared")
            if current.status == ServiceOrderStatus.FAILED:
                raise RuntimeError(f"document failed safely: {current.error_code}")
            if current.status == ServiceOrderStatus.DELIVERED:
                break
        else:
            raise TimeoutError(f"document timed out in {current.status if current else 'UNKNOWN'}")
        assert current is not None
        if current.generation_attempts != 1 or current.delivery_attempts != 1:
            raise RuntimeError("generation/delivery attempt invariant failed")
        if not current.docx_storage_key or not current.pdf_storage_key:
            raise RuntimeError("canonical DOCX/PDF keys missing")
        with tempfile.TemporaryDirectory(prefix="document-e2e-") as temp:
            root = Path(temp)
            docx_target = root / f"hujjat_{order.id}.docx"
            pdf_target = root / f"hujjat_{order.id}.pdf"
            docx = await storage.download_for_user(
                current.docx_storage_key, docx_target, user_id=user_id
            )
            pdf = await storage.download_for_user(
                current.pdf_storage_key, pdf_target, user_id=user_id
            )
            validate_docx_security(docx)
            with ZipFile(docx) as archive:
                names = archive.namelist()
                relationships = b"".join(
                    archive.read(name) for name in names if name.endswith(".rels")
                )
                document_xml = archive.read("word/document.xml")
            if (
                b"<w:instrText" in document_xml
                or b'TargetMode="External"' in relationships
                or any(name.lower().endswith((".bin", ".docm", ".xlsm")) for name in names)
            ):
                raise RuntimeError("DOCX editability/security evidence failed")
            docx_body = extracted_document_docx_body(docx)
            reader = PdfReader(str(pdf))
            if not pdf.read_bytes().startswith(b"%PDF-") or not reader.pages:
                raise RuntimeError("PDF header/page evidence failed")
            pdf_text = extracted_document_pdf_text(pdf)
            expected_source = _normalize(args.details)
            if expected_source not in docx_body:
                raise RuntimeError("user-provided source is not preserved in canonical body")
            forbidden = ("signed by", "officially approved", "certified by", "seal affixed")
            if any(value in docx_body.lower() for value in forbidden):
                raise RuntimeError("unsupported authority claim was added")
            metadata = current.generation_metadata
            if len(docx_body) != metadata.get("canonical_body_characters"):
                raise RuntimeError("DOCX does not match persisted canonical character count")
            if not _pdf_matches_docx(pdf_text, docx_body):
                raise RuntimeError("PDF does not preserve canonical DOCX body")
            if metadata.get("canonical_parity_score") != 1.0:
                raise RuntimeError("canonical parity checkpoint is not exact")
            if metadata.get("source_bound") is not True or metadata.get("reference_count") != 0:
                raise RuntimeError("source-bound/reference checkpoint failed")
            docx_hash, pdf_hash = _hash(docx), _hash(pdf)
        try:
            with tempfile.TemporaryDirectory() as denied:
                await storage.download_for_user(
                    current.docx_storage_key, Path(denied) / "x.docx", user_id=user_id + 1
                )
        except StorageAccessDenied:
            cross_user = "denied"
        else:
            raise RuntimeError("cross-user artifact access was allowed")
        async with factory() as session:
            after_events = int(
                await session.scalar(
                    select(func.count(OutboxEvent.id)).where(
                        OutboxEvent.event_type == "service.document.requested"
                    )
                )
                or 0
            )
        if after_events != before_events + 1:
            raise RuntimeError("outbox increment invariant failed")
        before_replay = (current.generation_attempts, current.delivery_attempts)
        from apps.worker.tasks import _process_service_document_draft

        await _process_service_document_draft(order.id)
        async with factory() as session:
            replayed = await session.get(ServiceOrder, order.id)
        if (
            replayed is None
            or (
                replayed.generation_attempts,
                replayed.delivery_attempts,
            )
            != before_replay
        ):
            raise RuntimeError("worker replay changed generation/delivery checkpoints")
        if replayed.docx_telegram_message_id is None or replayed.pdf_telegram_message_id is None:
            raise RuntimeError("both document artifacts were not durably delivered")
        return {
            "order_id": order.id,
            "service_type": "document",
            "document_type": args.document_type,
            "ui_locale": args.ui_locale,
            "language": args.language,
            "barrier0": barrier_orders,
            "outbox_increment": 1,
            "status": "DELIVERED",
            "generation_attempts": 1,
            "delivery_attempts": 1,
            "editable_docx": "validated_ooxml",
            "canonical_pdf": {"header": True, "pages": len(reader.pages), "parity": 1.0},
            "source_bound": True,
            "references": 0,
            "docx_delivered": True,
            "pdf_delivered": True,
            "duplicate": "none",
            "cross_user": cross_user,
            "docx_sha256": docx_hash,
            "pdf_sha256": pdf_hash,
        }
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--document-type",
        choices=("application", "explanation", "autobiography", "official_letter", "other"),
        default="application",
    )
    parser.add_argument("--ui-locale", choices=("uz", "ru", "en"), default="uz")
    parser.add_argument("--language", choices=("uz", "ru", "en"), default="uz")
    parser.add_argument(
        "--details",
        default=(
            "Toshkent davlat universiteti rektoriga. Men, Ali Valiyev, "
            "2026-yil 15-avgustdan uch kunlik ta’til so‘rayman."
        ),
    )
    parser.add_argument("--timeout", type=int, default=600)
    print(json.dumps(asyncio.run(_run(parser.parse_args())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
