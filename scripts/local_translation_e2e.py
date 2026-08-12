"""Probe the real TRANSLATE DB/outbox/worker/storage/Telegram pipeline."""

import argparse
import asyncio
import hashlib
import json
import re
import tempfile
import time
import uuid
import unicodedata
from pathlib import Path
from zipfile import ZipFile

from docx import Document
from pypdf import PdfReader
from sqlalchemy import func, select

from core.config import Settings
from core.database.session import create_engine, create_session_factory
from core.outbox.models import OutboxEvent
from modules.files.factory import build_storage
from modules.files.storage import StorageAccessDenied
from modules.service_orders.catalog import SERVICE_CATALOG, ServiceType
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderService, ServiceOrderStatus
from modules.service_orders.documents import validate_docx_security
from modules.users.service import UserService


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _docx_body(path: Path) -> str:
    document = Document(str(path))
    parts: list[str] = []
    for block in document.iter_inner_content():
        if hasattr(block, "rows"):
            parts.extend(" ".join(cell.text for cell in row.cells) for row in block.rows)
        elif block.text.strip():
            parts.append(block.text)
    if len(parts) < 3:
        raise RuntimeError("DOCX has no canonical translation body")
    return _normalize(" ".join(parts[2:]))


def _pdf_body_text(reader: PdfReader) -> str:
    raw = "\n".join(page.extract_text() or "" for page in reader.pages)
    without_rendered_list_markers = "\n".join(
        re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", line) for line in raw.splitlines()
    )
    return _normalize(without_rendered_list_markers)


def _artifact_bodies_are_nonempty(docx_body: str, pdf_text: str) -> bool:
    """Short translations are valid; digest and inclusion checks prove parity."""
    return bool(docx_body.strip() and pdf_text.strip())


async def _run(args: argparse.Namespace) -> dict[str, object]:
    settings = Settings()
    if settings.owner_telegram_id is None:
        raise RuntimeError("OWNER_TELEGRAM_ID is required")
    if settings.ai_provider == "mock":
        raise RuntimeError("real TRANSLATE probe refuses MockAIProvider")
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    storage = build_storage(settings)
    key = f"translation-e2e:{uuid.uuid4().hex}"
    payload = {
        "text": args.source,
        "source_language": args.source_language,
        "target_language": args.target_language,
    }
    try:
        async with factory() as session:
            user = await UserService().register(
                session,
                telegram_user_id=settings.owner_telegram_id,
                username="translation_e2e",
                first_name="Translation E2E",
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
                        OutboxEvent.event_type == "service.translate.requested"
                    )
                )
                or 0
            )
        if barrier_orders:
            raise RuntimeError("pre-confirmation barrier was not zero")
        async with factory() as session:
            order = await ServiceOrderService().create_translation_order(
                session,
                telegram_user_id=settings.owner_telegram_id,
                request_payload=payload,
                idempotency_key=key,
            )
            duplicate = await ServiceOrderService().create_translation_order(
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
                raise RuntimeError(f"translation failed safely: {current.error_code}")
            if current.status == ServiceOrderStatus.DELIVERED:
                break
        else:
            raise TimeoutError(
                f"translation timed out in {current.status if current else 'UNKNOWN'}"
            )
        assert current is not None
        if current.generation_attempts != 1 or current.delivery_attempts != 1:
            raise RuntimeError("generation/delivery attempt invariant failed")
        if not current.docx_storage_key or not current.pdf_storage_key:
            raise RuntimeError("canonical DOCX/PDF keys missing")
        with tempfile.TemporaryDirectory(prefix="translation-e2e-") as temp:
            root = Path(temp)
            docx_target = root / f"tarjima_{order.id}.docx"
            pdf_target = root / f"tarjima_{order.id}.pdf"
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
            docx_body = _docx_body(docx)
            reader = PdfReader(str(pdf))
            if not pdf.read_bytes().startswith(b"%PDF-") or not reader.pages:
                raise RuntimeError("PDF header/page evidence failed")
            pdf_text = _pdf_body_text(reader)
            if not _artifact_bodies_are_nonempty(docx_body, pdf_text):
                raise RuntimeError("DOCX/PDF is not meaningful")
            metadata = current.generation_metadata
            canonical_digest = hashlib.sha256(docx_body.encode()).hexdigest()
            if canonical_digest != metadata.get("canonical_body_sha256"):
                raise RuntimeError("DOCX does not match persisted canonical translation digest")
            if docx_body not in pdf_text:
                raise RuntimeError("PDF text does not preserve canonical target content")
            risk_gate_passed = metadata.get("translation_risk_gate_passed")
            protected_association = metadata.get("protected_association_score")
            canonical_parity = metadata.get("canonical_parity_score")
            reference_count = metadata.get("reference_count")
            if risk_gate_passed is not True:
                raise RuntimeError("translation multi-signal risk gate checkpoint is missing")
            if protected_association != 1.0:
                raise RuntimeError("protected atom/segment association checkpoint is not exact")
            if canonical_parity != 1.0:
                raise RuntimeError("computed DOCX/PDF canonical parity checkpoint is not exact")
            if metadata.get("target_language") != args.target_language:
                raise RuntimeError("target-language checkpoint mismatch")
            if reference_count != 0:
                raise RuntimeError("source-absent service references were added")
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
                        OutboxEvent.event_type == "service.translate.requested"
                    )
                )
                or 0
            )
        if (
            after_events != before_events + 1
            or SERVICE_CATALOG[ServiceType.DOCUMENT].production_ready
        ):
            raise RuntimeError("outbox increment or DOCUMENT closure invariant failed")
        async with factory() as session:
            try:
                await ServiceOrderService().create_preview_order(
                    session,
                    telegram_user_id=settings.owner_telegram_id,
                    service_type="document",
                    request_payload={"document_type": "other", "details": "probe"},
                    idempotency_key=f"p7-doc:{uuid.uuid4().hex}",
                )
            except PermissionError:
                pass
            else:
                raise RuntimeError("direct DOCUMENT order creation was not closed")
        before_replay = (current.generation_attempts, current.delivery_attempts)
        from apps.worker.tasks import _process_service_translation

        await _process_service_translation(order.id)
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
            raise RuntimeError("both translated artifacts were not durably delivered")
        return {
            "order_id": order.id,
            "service_type": "translate",
            "ui_locale": args.ui_locale,
            "direction": f"{args.source_language}->{args.target_language}",
            "barrier0": barrier_orders,
            "outbox_increment": 1,
            "status": "DELIVERED",
            "generation_attempts": 1,
            "delivery_attempts": 1,
            "editable_docx": "validated_ooxml",
            "meaningful_pdf": {"header": True, "pages": len(reader.pages), "text": True},
            "protected_association": protected_association,
            "canonical_parity": canonical_parity,
            "translation_risk_gate_passed": risk_gate_passed,
            "references": reference_count,
            "docx_delivered": replayed.docx_telegram_message_id is not None,
            "pdf_delivered": replayed.pdf_telegram_message_id is not None,
            "replay": {
                "generation_attempts": replayed.generation_attempts,
                "delivery_attempts": replayed.delivery_attempts,
            },
            "duplicate": "none",
            "cross_user": cross_user,
            "document": "closed",
            "docx_sha256": docx_hash,
            "pdf_sha256": pdf_hash,
        }
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ui-locale", choices=("uz", "ru", "en"), default="uz")
    parser.add_argument("--source-language", choices=("uz", "ru", "en"), default="uz")
    parser.add_argument("--target-language", choices=("uz", "ru", "en"), default="en")
    parser.add_argument(
        "--source",
        default="Ali 2026-08-12 gacha 25 USD to‘lashi shart. https://example.com ga qarang.",
    )
    parser.add_argument("--timeout", type=int, default=600)
    print(json.dumps(asyncio.run(_run(parser.parse_args())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
