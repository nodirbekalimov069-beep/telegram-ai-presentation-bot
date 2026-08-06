"""Deterministic AI provider used by local development and tests."""

from modules.ai.contracts import Outline, OutlineSlide, PresentationRequest

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
