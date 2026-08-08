"""Fast free-form presentation parsing and local PDF rendering."""

import asyncio
import hashlib
import hmac
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from core.config import Settings
from modules.ai.factory import build_ai_provider, close_ai_provider
from modules.files.factory import build_storage
from modules.files.storage import LocalStorage
from modules.orders.models import Order
from modules.orders.service import OrderService
from modules.payments.providers.mock import MockPaymentProvider
from modules.payments.service import PaymentService
from modules.presentations.engine import NodePresentationRenderer
from modules.presentations.generation import GenerationPipeline
from modules.presentations.service import PresentationService
from modules.users.models import User
from modules.users.service import FreeDemoService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_SLIDE_COUNT_RE = re.compile(
    r"(?<![0-9])(?P<count>[0-9]+)(?![0-9])\s*(?:[-–—]\s*)?(?:ta\s*)?"
    r"(?:slayd(?:lar|li)?|sahifa(?:li)?|bet|pages?|slides?|слайд(?:ов|а|ы)?)\b",
    re.IGNORECASE,
)
_IMAGE_WORDS = ("rasm", "image", "photo", "foto", "картин", "изображ")
_COLOR_WORDS = ("rangli", "colorful", "colourful", "ярк", "цветн")


@dataclass(frozen=True, slots=True)
class QuickPresentationRequest:
    topic: str
    language: str
    slide_count: int
    include_images: bool
    colorful: bool


@dataclass(frozen=True, slots=True)
class QuickPresentationArtifacts:
    order_id: int
    presentation_id: int
    title: str
    pptx_path: Path
    pdf_path: Path


class QuickPresentationRequestError(ValueError):
    """Raised when a free-form request cannot map to a supported plan."""


class QuickPresentationUnavailable(RuntimeError):
    """Raised when the secure quick-generation path is unavailable."""


def _extract_topic(text: str, *, language: str) -> str:
    if language == "en":
        match = re.search(r"\babout\s+(.+)$", text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip(" ,.-:")[:80]
    if language == "ru":
        match = re.search(r"\b(?:о|об)\s+(.+)$", text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip(" ,.-:")[:80]
    topic = re.sub(
        r"^(?:menga|iltimos|please|пожалуйста|мне)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    topic = _SLIDE_COUNT_RE.sub(" ", topic, count=1)
    topic = re.split(
        r"\s+(?:haqida|to['\u2018\u2019]?g['\u2018\u2019]?risida|rasmlar?\s+bilan|"
        r"slaydga|slayd\s+qilib|презентац|слайд|presentation|slides?)\b",
        topic,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    topic = topic.strip(" ,.-:")
    return (topic or text)[:80]


def parse_quick_request(text: str, *, language: str) -> QuickPresentationRequest:
    """Extract safe deterministic defaults from a free-form user request."""
    normalized = " ".join(text.split()).strip()
    if len(normalized) < 3:
        raise QuickPresentationRequestError("presentation request is too short")
    match = _SLIDE_COUNT_RE.search(normalized)
    raw_count = match.group("count") if match else "8"
    if len(raw_count) > 2:
        raise QuickPresentationRequestError("slide count must be between 1 and 30")
    slide_count = int(raw_count)
    if not 1 <= slide_count <= 30:
        raise QuickPresentationRequestError("slide count must be between 1 and 30")
    if slide_count == 9:
        raise QuickPresentationRequestError("slide count must be 1–8 or 10–30")
    lowered = normalized.casefold()
    return QuickPresentationRequest(
        topic=_extract_topic(normalized, language=language),
        language=language if language in {"uz", "ru", "en"} else "uz",
        slide_count=slide_count,
        include_images=any(word in lowered for word in _IMAGE_WORDS),
        colorful=any(word in lowered for word in _COLOR_WORDS),
    )


def select_plan_code(slide_count: int) -> str:
    """Map a requested count to the authoritative seeded plan ranges."""
    if 1 <= slide_count <= 8:
        return "free_demo"
    if 10 <= slide_count <= 15:
        return "medium"
    if 16 <= slide_count <= 20:
        return "premium"
    if 21 <= slide_count <= 30:
        return "premium_plus"
    raise QuickPresentationRequestError("no active plan supports this slide count")


def _slide_sort_key(name: str) -> int:
    match = re.search(r"slide(\d+)\.xml$", name)
    return int(match.group(1)) if match else 0


def _extract_slide_texts(pptx_path: Path) -> list[list[str]]:
    with zipfile.ZipFile(pptx_path) as archive:
        names = sorted(
            (
                name
                for name in archive.namelist()
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
            ),
            key=_slide_sort_key,
        )
        slides: list[list[str]] = []
        for name in names:
            # Input is a PPTX produced by our own local renderer, not uploaded XML.
            root = ElementTree.fromstring(archive.read(name))
            texts = [
                str(node.text).strip()
                for node in root.iter()
                if node.tag.endswith("}t") and node.text and str(node.text).strip()
            ]
            slides.append(texts or ["Presentation"])
    if not slides:
        raise ValueError("PPTX contains no slides")
    return slides


def _wrap_text(text: str, *, max_chars: int) -> list[str]:
    words = text.replace("\u2019", "'").split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join([*current, word])
        if current and len(candidate) > max_chars:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines or [""]


class PptxTextPdfConverter:
    """Render a portable 16:9 PDF from text contained in a generated PPTX."""

    async def convert(self, pptx_path: Path, output_pdf: Path) -> Path:
        return await asyncio.to_thread(self._convert_sync, pptx_path, output_pdf)

    def _convert_sync(self, pptx_path: Path, output_pdf: Path) -> Path:
        from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

        source = pptx_path.expanduser().resolve(strict=True)
        output = output_pdf.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        slides = _extract_slide_texts(source)
        width, height = 960.0, 540.0
        document = canvas.Canvas(str(output), pagesize=(width, height))
        palette = ("#071426", "#0B203A", "#14B8A6", "#F8FAFC")
        for index, texts in enumerate(slides, start=1):
            document.setFillColor(palette[(index - 1) % 2])
            document.rect(0, 0, width, height, fill=1, stroke=0)
            document.setFillColor(palette[2])
            document.roundRect(52, 455, 92, 8, 4, fill=1, stroke=0)
            document.setFillColor(palette[3])
            document.setFont("Helvetica-Bold", 28)
            title_lines = _wrap_text(texts[0], max_chars=48)
            y = 410
            for line in title_lines[:2]:
                document.drawString(54, y, line)
                y -= 38
            document.setFont("Helvetica", 18)
            y -= 12
            body = texts[1:] or [" "]
            for item in body[:7]:
                for line_index, line in enumerate(_wrap_text(item, max_chars=72)):
                    prefix = "• " if line_index == 0 else "  "
                    document.drawString(70, y, f"{prefix}{line}")
                    y -= 27
                    if y < 70:
                        break
                if y < 70:
                    break
            document.setFillColor("#94A3B8")
            document.setFont("Helvetica", 10)
            document.drawRightString(910, 35, f"{index} / {len(slides)}")
            document.showPage()
        document.save()
        return output


def _request_settings(request: QuickPresentationRequest) -> dict[str, object]:
    directions = {
        "uz": "Dasturlash",
        "ru": "Программирование",
        "en": "Programming",
    }
    audiences = {
        "uz": "Umumiy auditoriya",
        "ru": "Широкая аудитория",
        "en": "General audience",
    }
    purposes = {
        "uz": (
            "Bosqichma-bosqich tushuntirish",
            "Bosqichma-bosqich tushuntirish; vizual misollar va diagrammalardan foydalanish",
        ),
        "ru": (
            "Пошаговое объяснение",
            "Пошаговое объяснение с визуальными примерами и диаграммами",
        ),
        "en": (
            "Step-by-step explanation",
            "Step-by-step explanation with visual examples and diagrams",
        ),
    }
    standard_purpose, visual_purpose = purposes[request.language]
    return {
        "language": request.language,
        "direction": directions[request.language],
        "purpose": visual_purpose if request.include_images else standard_purpose,
        "audience": audiences[request.language],
        "knowledge_level": "beginner",
        "slide_count": request.slide_count,
        "design_style": "creative" if request.colorful else "modern",
    }


async def _apply_mock_payment(
    session: AsyncSession,
    *,
    order: Order,
    secret: str,
) -> None:
    payload = json.dumps(
        {
            "event_id": f"quick-{order.id}",
            "transaction_id": f"quick-mock-{order.id}",
            "order_public_id": order.public_id,
            "amount": order.amount,
            "currency": order.currency,
            "status": "paid",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    await PaymentService().handle_webhook(
        session,
        MockPaymentProvider(secret=secret),
        payload,
        {"X-Mock-Signature": signature},
    )


class QuickPresentationService:
    """Create real local artifacts from one free-form command in test mode."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def run(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        telegram_user_id: int,
        text: str,
    ) -> QuickPresentationArtifacts:
        async with session_factory() as session:
            user = await session.scalar(
                select(User).where(User.telegram_user_id == telegram_user_id)
            )
            if user is None or user.is_blocked:
                raise QuickPresentationUnavailable("user is missing or blocked")
            language = user.selected_language
            user_id = user.id

        request = parse_quick_request(text, language=language)
        plan_code = select_plan_code(request.slide_count)
        order_settings = _request_settings(request)
        async with session_factory() as session:
            if plan_code == "free_demo":
                order = await FreeDemoService().claim(
                    session,
                    telegram_user_id=telegram_user_id,
                    topic=request.topic,
                    settings=order_settings,
                )
            else:
                order = await OrderService().create_paid_order(
                    session,
                    user_id=user_id,
                    plan_code=plan_code,
                    topic=request.topic,
                    settings=order_settings,
                )
                if not (
                    self._settings.app_env == "test" and self._settings.payment_provider == "mock"
                ):
                    raise QuickPresentationUnavailable(
                        "quick paid generation requires a verified payment"
                    )
                await _apply_mock_payment(
                    session,
                    order=order,
                    secret=self._settings.mock_payment_secret.get_secret_value(),
                )
            order_id = order.id

        provider = build_ai_provider(self._settings)
        try:
            async with session_factory() as session:
                presentation = await PresentationService(ai_provider=provider).generate_outline(
                    session,
                    order_id=order_id,
                )
                presentation_id = presentation.id
        finally:
            await close_ai_provider(provider)

        async with session_factory() as session:
            await PresentationService().approve_outline(session, presentation_id=presentation_id)

        storage = build_storage(self._settings)
        if not isinstance(storage, LocalStorage):
            raise QuickPresentationUnavailable("quick delivery requires local storage")
        renderer_path = self._settings.presentation_renderer.expanduser().resolve()
        pipeline = GenerationPipeline(
            renderer=NodePresentationRenderer(
                renderer_path.parent,
                node_binary=self._settings.node_binary,
            ),
            pdf_converter=PptxTextPdfConverter(),
            storage=storage,
            work_root=self._settings.generation_work_root,
        )
        result = await pipeline.run(
            session_factory,
            presentation_id=presentation_id,
        )
        if not result.completed:
            raise QuickPresentationUnavailable("generation did not complete")
        pptx_path = await storage.path_for_user(
            result.pptx_storage_key,
            user_id=user_id,
        )
        pdf_path = await storage.path_for_user(
            result.pdf_storage_key,
            user_id=user_id,
        )
        return QuickPresentationArtifacts(
            order_id=order_id,
            presentation_id=presentation_id,
            title=request.topic,
            pptx_path=pptx_path,
            pdf_path=pdf_path,
        )
