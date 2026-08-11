"""Run a real quiz order through outbox, worker, storage and Telegram."""

import argparse
import asyncio
import hashlib
import json
import re
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
from modules.files.storage import StorageAccessDenied
from modules.service_orders.catalog import SERVICE_CATALOG, ServiceType
from modules.service_orders.document_services import PRODUCTION_DOCUMENT_SERVICE_TYPES
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderService, ServiceOrderStatus
from modules.users.service import UserService

_ANSWER_HEADINGS = {"uz": "JAVOBLAR KALITI", "ru": "КЛЮЧ ОТВЕТОВ", "en": "ANSWER KEY"}
_REFERENCES_HEADINGS = {
    "uz": "Manbalar holati",
    "ru": "Состояние источников",
    "en": "Source status",
}
_REQUIREMENTS_HEADINGS = {
    "uz": "Qo‘shimcha talablarning qo‘llanishi",
    "ru": "Применение дополнительных требований",
    "en": "Application of additional requirements",
}
_QUESTION_PATTERN = re.compile(r"^(\d+)\.\s+(.+)$")
_OPTION_PATTERN = re.compile(r"^(?:[•]\s*)?([A-E])\)\s+(.+)$")
_OPTION_LABEL_PATTERN = re.compile(r"^(?:[•]\s*)?([A-E])\)\s*$")
_EXPLANATION_HEADINGS = {
    "qisqa izohlar",
    "краткие пояснения",
    "short explanations",
    "brief explanations",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _parse_quiz_structure(
    lines: list[str],
    *,
    answer_heading: str | None,
    question_count: int,
    option_count: int,
    source: str,
) -> dict[int, dict[str, Any]]:
    if answer_heading is None:
        terminal_headings = {
            _normalized_text(value)
            for value in (*_REQUIREMENTS_HEADINGS.values(), *_REFERENCES_HEADINGS.values())
        }
        answer_index = next(
            (
                index
                for index, line in enumerate(lines)
                if _normalized_text(line) in terminal_headings
            ),
            len(lines),
        )
    else:
        try:
            answer_index = next(
                index
                for index, line in enumerate(lines)
                if _normalized_text(line) == _normalized_text(answer_heading)
            )
        except StopIteration as exc:
            raise RuntimeError(f"{source} answer-key heading is missing") from exc

    structure: dict[int, dict[str, Any]] = {}
    current_number: int | None = None
    detached_option = False
    for value in lines[:answer_index]:
        if value.strip().isdigit():
            continue
        question_match = _QUESTION_PATTERN.match(value)
        if question_match:
            number = int(question_match.group(1))
            if 1 <= number <= question_count:
                current_number = number
                structure[number] = {"text": question_match.group(2), "options": []}
                continue
        option_match = _OPTION_PATTERN.match(value)
        if option_match and current_number is not None:
            expected = chr(ord("A") + len(structure[current_number]["options"]))
            if option_match.group(1) != expected:
                raise RuntimeError(f"{source} option labels are not contiguous")
            structure[current_number]["options"].append(option_match.group(2))
            detached_option = False
            continue
        option_label = _OPTION_LABEL_PATTERN.match(value)
        if option_label and current_number is not None:
            expected = chr(ord("A") + len(structure[current_number]["options"]))
            if option_label.group(1) != expected:
                raise RuntimeError(f"{source} option labels are not contiguous")
            structure[current_number]["options"].append("")
            detached_option = True
            continue
        if current_number is not None:
            current = structure[current_number]
            if current["options"]:
                current["options"][-1] = (
                    value if detached_option else f"{current['options'][-1]} {value}".strip()
                )
                detached_option = False
            else:
                current["text"] = f"{current['text']} {value}".strip()
    if sorted(structure) != list(range(1, question_count + 1)):
        raise RuntimeError(f"{source} question count mismatch: {sorted(structure)}")
    if any(len(item["options"]) != option_count for item in structure.values()):
        raise RuntimeError(f"{source} exact option count mismatch")
    if any(
        not _normalized_text(option) for item in structure.values() for option in item["options"]
    ):
        raise RuntimeError(f"{source} contains an empty detached option")

    if answer_heading is None:
        return structure

    answers: dict[int, str] = {}
    answer_labels: dict[int, str] = {}
    current_answer: int | None = None
    for value in lines[answer_index + 1 :]:
        if _normalized_text(value) in _EXPLANATION_HEADINGS:
            break
        if value.strip().isdigit():
            continue
        match = _QUESTION_PATTERN.match(value)
        if match:
            option_match = _OPTION_PATTERN.match(match.group(2))
            option_label = _OPTION_LABEL_PATTERN.match(match.group(2))
            number = int(match.group(1))
            if option_match and 1 <= number <= question_count:
                answers[number] = option_match.group(2)
                answer_labels[number] = option_match.group(1)
                current_answer = number
                continue
            if option_label and 1 <= number <= question_count:
                answers[number] = ""
                answer_labels[number] = option_label.group(1)
                current_answer = number
                continue
        if current_answer is None:
            continue
        answers[current_answer] = f"{answers[current_answer]} {value}".strip()
    if sorted(answers) != list(range(1, question_count + 1)):
        raise RuntimeError(f"{source} answer-key count mismatch")
    for number, answer in answers.items():
        options = structure[number]["options"]
        if _normalized_text(answer) not in {_normalized_text(option) for option in options}:
            raise RuntimeError(f"{source} answer-key mismatch for question {number}")
        answer_index = next(
            index
            for index, option in enumerate(options)
            if _normalized_text(option) == _normalized_text(answer)
        )
        if answer_labels[number] != chr(ord("A") + answer_index):
            raise RuntimeError(f"{source} answer-key label mismatch for question {number}")
        structure[number]["answer"] = answer
    return structure


def _assert_structure_parity(
    docx_structure: dict[int, dict[str, Any]],
    pdf_structure: dict[int, dict[str, Any]],
) -> None:
    def normalized(value: object) -> str:
        return _normalized_text(str(value))

    for number in docx_structure:
        left = docx_structure[number]
        right = pdf_structure.get(number)
        if right is None or normalized(left["text"]) != normalized(right["text"]):
            raise RuntimeError(f"PDF question parity mismatch for question {number}")
        if [normalized(value) for value in left["options"]] != [
            normalized(value) for value in right["options"]
        ]:
            raise RuntimeError(f"PDF option parity mismatch for question {number}")
        if ("answer" in left) != ("answer" in right):
            raise RuntimeError(f"PDF answer-key visibility mismatch for question {number}")
        if "answer" in left and normalized(left["answer"]) != normalized(right["answer"]):
            raise RuntimeError(f"PDF answer-key parity mismatch for question {number}")


def _validate_docx(
    path: Path,
    *,
    language: str,
    topic: str,
    question_count: int,
    option_count: int,
    required_phrase: str,
    show_answer_key: bool = True,
) -> dict[str, Any]:
    if path.read_bytes()[:2] != b"PK":
        raise RuntimeError("DOCX has invalid ZIP magic bytes")
    document = Document(str(path))
    paragraphs = [
        paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()
    ]
    content = "\n".join(paragraphs)
    if topic.casefold() not in content.casefold():
        raise RuntimeError("quiz topic did not reach DOCX")
    if required_phrase.casefold() not in content.casefold():
        raise RuntimeError("additional requirement evidence is missing from DOCX")
    answer_heading = _ANSWER_HEADINGS[language]
    references_heading = _REFERENCES_HEADINGS[language]
    try:
        references_index = paragraphs.index(references_heading)
    except ValueError as exc:
        raise RuntimeError("DOCX references heading is missing") from exc
    if show_answer_key:
        try:
            answer_index = paragraphs.index(answer_heading)
        except ValueError as exc:
            raise RuntimeError("DOCX answer-key heading is missing") from exc
        if answer_index >= references_index:
            raise RuntimeError("DOCX answer key and references order is invalid")
    elif answer_heading in paragraphs:
        raise RuntimeError("DOCX answer key is visible although disabled")

    structure = _parse_quiz_structure(
        paragraphs,
        answer_heading=answer_heading if show_answer_key else None,
        question_count=question_count,
        option_count=option_count,
        source="DOCX",
    )
    if any(marker in content.casefold() for marker in ("http://", "https://", "doi:", "isbn")):
        raise RuntimeError("unverified citation escaped into QUIZ DOCX")
    if any(
        marker in content.casefold() for marker in ("system prompt", "service_order_id", "api_key")
    ):
        raise RuntimeError("internal data escaped into QUIZ DOCX")
    return {
        "bytes": path.stat().st_size,
        "characters": len(content),
        "question_count": len(structure),
        "answer_key_integrity": show_answer_key,
        "topic_present": True,
        "requirement_present": True,
        "sha256": _sha256(path),
        "_structure": structure,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings()
    if settings.owner_telegram_id is None:
        raise RuntimeError("OWNER_TELEGRAM_ID is required for owner-only QUIZ E2E")
    telegram_user_id = settings.owner_telegram_id
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    storage = build_storage(settings)
    idempotency_key = f"quiz-e2e:{uuid.uuid4().hex}"
    request_payload = {
        "topic": args.topic,
        "question_count": args.question_count,
        "difficulty": args.difficulty,
        "option_count": args.option_count,
        "answer_key": args.answer_key,
        "language": args.language,
        "requirements": args.requirements,
    }
    try:
        async with factory() as session:
            user = await UserService().register(
                session,
                telegram_user_id=telegram_user_id,
                username="quiz_e2e",
                first_name="Quiz E2E",
                last_name=None,
                language_code=args.language,
            )
            order = await ServiceOrderService().create_quiz_order(
                session,
                telegram_user_id=telegram_user_id,
                request_payload=request_payload,
                idempotency_key=idempotency_key,
            )
            order_id = order.id
            user_id = user.id
        async with factory() as session:
            duplicate = await ServiceOrderService().create_quiz_order(
                session,
                telegram_user_id=telegram_user_id,
                request_payload=request_payload,
                idempotency_key=idempotency_key,
            )
        if duplicate.id != order_id:
            raise RuntimeError("idempotent submit created a duplicate QUIZ order")

        deadline = time.monotonic() + args.timeout
        last_status = "QUEUED"
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            async with factory() as session:
                current = await session.scalar(
                    select(ServiceOrder).where(ServiceOrder.id == order_id)
                )
                if current is None:
                    raise RuntimeError("QUIZ order disappeared")
                last_status = str(current.status)
                if current.status == ServiceOrderStatus.DELIVERED:
                    docx_key = current.docx_storage_key
                    pdf_key = current.pdf_storage_key
                    docx_message_id = current.docx_telegram_message_id
                    pdf_message_id = current.pdf_telegram_message_id
                    generation_attempts = current.generation_attempts
                    delivery_attempts = current.delivery_attempts
                    generation_metadata = dict(current.generation_metadata)
                    result_metadata = dict(current.result_metadata)
                    break
                if current.status == ServiceOrderStatus.FAILED:
                    raise RuntimeError(
                        f"QUIZ failed safely: {current.error_code}: {current.error_message}"
                    )
        else:
            raise TimeoutError(f"QUIZ E2E timed out in status {last_status}")

        if docx_key is None or docx_message_id is None:
            raise RuntimeError("QUIZ DOCX was not persisted and delivered")
        if pdf_key is None or pdf_message_id is None:
            raise RuntimeError("validated QUIZ PDF was not persisted and delivered")
        if generation_attempts != 1 or delivery_attempts != 1:
            raise RuntimeError(
                f"unexpected attempts generation={generation_attempts} delivery={delivery_attempts}"
            )

        with tempfile.TemporaryDirectory(prefix=f"quiz-e2e-{order_id}-") as directory:
            root = Path(directory)
            docx_path = await storage.download_for_user(
                docx_key, root / f"quiz_{order_id}.docx", user_id=user_id
            )
            docx_result = _validate_docx(
                docx_path,
                language=args.language,
                topic=args.topic,
                question_count=args.question_count,
                option_count=args.option_count,
                required_phrase=args.required_phrase,
                show_answer_key=args.answer_key == "yes",
            )
            pdf_path = await storage.download_for_user(
                pdf_key, root / f"quiz_{order_id}.pdf", user_id=user_id
            )
            if pdf_path.read_bytes()[:5] != b"%PDF-":
                raise RuntimeError("QUIZ PDF has invalid magic bytes")
            pdf = PdfReader(str(pdf_path), strict=True)
            if len(pdf.pages) < 1:
                raise RuntimeError("QUIZ PDF has no pages")
            page_texts = [(page.extract_text() or "").strip() for page in pdf.pages]
            if any(len(value) < 20 for value in page_texts):
                raise RuntimeError("QUIZ PDF has a blank or nearly blank page")
            extracted_text = "\n".join(page_texts)
            if len(extracted_text) < 200 or args.topic.casefold() not in extracted_text.casefold():
                raise RuntimeError("QUIZ PDF content parity failed")
            show_answer_key = args.answer_key == "yes"
            answer_key_present = (
                _ANSWER_HEADINGS[args.language].casefold() in extracted_text.casefold()
            )
            if answer_key_present != show_answer_key:
                raise RuntimeError("QUIZ PDF answer-key visibility mismatch")
            pdf_structure = _parse_quiz_structure(
                [line.strip() for line in extracted_text.splitlines() if line.strip()],
                answer_heading=_ANSWER_HEADINGS[args.language] if show_answer_key else None,
                question_count=args.question_count,
                option_count=args.option_count,
                source="PDF",
            )
            _assert_structure_parity(docx_result["_structure"], pdf_structure)
            docx_result.pop("_structure")
            pdf_result = {
                "bytes": pdf_path.stat().st_size,
                "pages": len(pdf.pages),
                "characters": len(extracted_text),
                "topic_present": True,
                "answer_key_present": answer_key_present,
                "question_count": len(pdf_structure),
                "exact_option_count": args.option_count,
                "structure_parity": True,
                "sha256": _sha256(pdf_path),
            }
            try:
                await storage.download_for_user(
                    docx_key, root / "cross-user.docx", user_id=user_id + 1
                )
            except StorageAccessDenied:
                cross_user_denied = True
            else:
                cross_user_denied = False
            if not cross_user_denied:
                raise RuntimeError("cross-user QUIZ artifact access was not denied")

        async with factory() as session, session.begin():
            replay_event = await session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.event_type == "service.quiz.requested",
                    OutboxEvent.aggregate_id == str(order_id),
                )
                .with_for_update()
            )
            if replay_event is None:
                raise RuntimeError("QUIZ transactional outbox event is missing")
            replay_event.status = "PENDING"
            replay_event.available_at = datetime.now(UTC)
            replay_event.published_at = None
        await asyncio.sleep(5)
        async with factory() as session:
            replayed = await session.get(ServiceOrder, order_id)
        if replayed is None:
            raise RuntimeError("QUIZ order disappeared after replay")
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
            raise RuntimeError("outbox replay changed the delivered QUIZ result")

        next_preview_closed = (
            not SERVICE_CATALOG[ServiceType.WRITE_TEXT].production_ready
            and ServiceType.WRITE_TEXT.value not in PRODUCTION_DOCUMENT_SERVICE_TYPES
        )
        if not next_preview_closed:
            raise RuntimeError("next preview service was opened")
        if generation_metadata.get("question_count") != args.question_count:
            raise RuntimeError("generation metadata question count mismatch")
        if generation_metadata.get("reference_count") != 0:
            raise RuntimeError("unverified QUIZ references were persisted")
        return {
            "service_type": ServiceType.QUIZ.value,
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
    parser.add_argument("--topic", default="Algoritmik fikrlash")
    parser.add_argument("--language", choices=("uz", "ru", "en"), default="uz")
    parser.add_argument("--question-count", type=int, default=4)
    parser.add_argument("--difficulty", choices=("easy", "medium", "hard"), default="medium")
    parser.add_argument("--option-count", type=int, choices=(2, 3, 4, 5), default=4)
    parser.add_argument("--answer-key", choices=("yes", "no"), default="yes")
    parser.add_argument(
        "--requirements",
        default="Amaliy vaziyatlarni tahlil qilish orqali yechimni tekshirishga e'tibor bering.",
    )
    parser.add_argument("--required-phrase", default="amaliy vaziyat")
    parser.add_argument("--timeout", type=int, default=600)
    print(json.dumps(asyncio.run(_run(parser.parse_args())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
