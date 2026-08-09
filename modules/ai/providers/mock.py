"""Deterministic AI provider used by local development and tests."""

from modules.ai.contracts import (
    ContentGenerationRequest,
    GeneratedContent,
    GeneratedContentBlock,
    GeneratedContentSlide,
    Outline,
    OutlineSlide,
    PresentationRequest,
)

_SECTION_TITLES = {
    "uz": (
        "Kirish",
        "Asosiy tushuncha",
        "Muammo va kontekst",
        "Dalillar",
        "Tahlil",
        "Misol",
        "Yechim",
        "Tavsiyalar",
        "Xulosa",
    ),
    "ru": (
        "Введение",
        "Основная идея",
        "Проблема и контекст",
        "Факты",
        "Анализ",
        "Пример",
        "Решение",
        "Рекомендации",
        "Вывод",
    ),
    "en": (
        "Introduction",
        "Core concept",
        "Problem and context",
        "Evidence",
        "Analysis",
        "Example",
        "Solution",
        "Recommendations",
        "Conclusion",
    ),
}


class MockAIProvider:
    async def generate_outline(self, request: PresentationRequest) -> Outline:
        titles = _SECTION_TITLES[request.language]
        slides: list[OutlineSlide] = []
        for index in range(1, request.slide_count + 1):
            if index == 1:
                title = f"{request.topic}: {titles[0]}"
                layout = "title"
            elif index == request.slide_count:
                title = titles[-1]
                layout = "summary"
            else:
                title = f"{titles[(index - 1) % (len(titles) - 1)]} {index}"
                layout = "title_and_content"
            slides.append(
                OutlineSlide(
                    number=index,
                    title=title,
                    purpose=f"{request.audience} uchun bitta asosiy fikr",
                    key_points=[
                        f"{request.topic} bo‘yicha aniq fikr {index}",
                        f"Maqsad: {request.purpose}",
                    ],
                    visual_type="icon" if index % 3 else "bar_chart",
                    layout=layout,
                    sources=[],
                )
            )
        return Outline(title=request.topic, slides=slides)

    async def generate_content(self, request: ContentGenerationRequest) -> GeneratedContent:
        slides: list[GeneratedContentSlide] = []
        raw_slides = request.outline.get("slides", [])
        if not isinstance(raw_slides, list):
            raw_slides = []
        for index, raw in enumerate(raw_slides, start=1):
            item = raw if isinstance(raw, dict) else {}
            title = str(item.get("title") or f"Slayd {index}")
            raw_points = item.get("key_points", [])
            points = raw_points if isinstance(raw_points, list) else []
            blocks = [
                GeneratedContentBlock(
                    heading=str(point)[:80],
                    body=(
                        f"{point} mavzuning muhim tarkibiy qismi hisoblanadi. "
                        f"Bu jihat {request.audience} auditoriyasi uchun sabab, kontekst va "
                        "amaliy ahamiyat nuqtayi nazaridan izohlanadi."
                    )[:300],
                )
                for point in points[:4]
                if str(point).strip()
            ]
            while len(blocks) < 2:
                blocks.append(
                    GeneratedContentBlock(
                        heading="Asosiy mazmun",
                        body=(
                            f"{title} mavzusi {request.direction} yo‘nalishi doirasida "
                            "tushuntiriladi. Uning nazariy mazmuni, konteksti va amaliy "
                            "ahamiyati o‘zaro bog‘liq holda yoritiladi."
                        ),
                    )
                )
            slides.append(
                GeneratedContentSlide(
                    number=index,
                    title=title,
                    body_blocks=blocks,
                    speaker_notes=(
                        f"Ushbu slaydda {title} mavzusini {request.knowledge_level} darajada "
                        f"tushuntiring. Asosiy tushunchalarni {request.audience} ehtiyojlari "
                        "bilan bog‘lang, dalil chegarasini ko‘rsating va keyingi slaydga "
                        "mantiqiy o‘tish yarating. Muhokama uchun bitta tekshiriladigan savol bering."
                    ),
                )
            )
        return GeneratedContent(title=request.title, slides=slides)
