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

from modules.ai.essay_contracts import (
    EssayDocument,
    EssayGenerationRequest,
    EssaySection,
    validate_essay_against_request,
)
from modules.ai.independent_work_contracts import (
    IndependentWorkDocument,
    IndependentWorkGenerationRequest,
    IndependentWorkSection,
    validate_independent_work_against_request,
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

    async def generate_essay(self, request: EssayGenerationRequest) -> EssayDocument:
        section_count = {"short": 2, "medium": 4, "long": 6}[request.length]
        requirement = request.additional_requirements or "Maxsus qo‘shimcha talab berilmagan"
        sections = [
            EssaySection(
                heading=f"Asosiy yo‘nalish {index}",
                paragraphs=[
                    (
                        f"{request.topic} mavzusining {index}-yo‘nalishi nazariy tushunchalar, "
                        "sabab-oqibat aloqalari va amaliy kontekst orqali yoritiladi. Ushbu tahlil "
                        f"foydalanuvchi talabini ham hisobga oladi: {requirement}."
                    ),
                    (
                        "Masalani baholashda umumiy da’volar tekshiriladigan dalillardan ajratiladi, "
                        "cheklovlar ochiq ko‘rsatiladi va xulosalar mavzu doirasida ehtiyotkor tarzda "
                        f"shakllantiriladi. Bu {index}-bo‘limning akademik izchilligini ta’minlaydi."
                    ),
                ],
            )
            for index in range(1, section_count + 1)
        ]
        document = EssayDocument(
            title=request.topic if len(request.topic) >= 3 else f"{request.topic} referati",
            language=request.language,
            introduction=(
                f"{request.topic} zamonaviy ilmiy va amaliy muhokamalarda dolzarb o‘rin tutadi. "
                "Referat mavzuning asosiy tushunchalarini, rivojlanish omillarini va qo‘llash "
                f"chegaralarini izchil tahlil qiladi. Qo‘shimcha yo‘nalish: {requirement}."
            ),
            sections=sections,
            conclusion=(
                f"{request.topic} bo‘yicha ko‘rib chiqilgan jihatlar nazariya va amaliyotning o‘zaro "
                "bog‘liqligini ko‘rsatadi. Yakuniy baho dalillarni tekshirish, cheklovlarni tan olish "
                "va mas’uliyatli qaror qabul qilish zarurligini ta’kidlaydi."
            ),
            references=[],
        )
        return validate_essay_against_request(document, request)

    async def generate_independent_work(
        self, request: IndependentWorkGenerationRequest
    ) -> IndependentWorkDocument:
        section_count = {"short": 2, "medium": 4, "long": 6}[request.length]
        requirement = request.additional_requirements or "Maxsus qo‘shimcha talab berilmagan"
        topic_reference = (
            request.topic if not any(char.isdigit() for char in request.topic) else "mavzu"
        )
        evidence = (
            f"{topic_reference} doirasida amaliy qarorlar xavflarni oldindan baholash, inson "
            f"nazoratini saqlash va natijani tekshirish bilan asoslanadi. {requirement}"
            if request.additional_requirements
            else None
        )
        section_labels = {
            "uz": ["Birinchi", "Ikkinchi", "Uchinchi", "To‘rtinchi", "Beshinchi", "Oltinchi"],
            "ru": ["Первое", "Второе", "Третье", "Четвёртое", "Пятое", "Шестое"],
            "en": ["First", "Second", "Third", "Fourth", "Fifth", "Sixth"],
        }[request.language]
        paragraph = (
            f"{topic_reference} {request.subject} fani doirasida nazariy tushunchalar, "
            "sabab-oqibat aloqalari va amaliy vazifalar orqali izchil yoritiladi. Tahlilda "
            "tekshirilmagan tashqi manbalar haqiqiy dalil sifatida ko‘rsatilmaydi, cheklovlar "
            f"ochiq qayd etiladi va foydalanuvchi talabi hisobga olinadi: {requirement}."
        )
        document = IndependentWorkDocument(
            title=request.topic,
            subject=request.subject,
            language=request.language,
            objective=(
                f"{topic_reference}ning {request.subject} fanidagi o‘rnini tushuntirish, "
                "asosiy tushunchalarni tizimlashtirish va amaliy qo‘llanish yo‘llarini "
                f"ko‘rsatish. Qo‘shimcha yo‘nalish: {requirement}."
            ),
            practical_tasks=[
                f"{topic_reference} bo‘yicha asosiy tushunchalarni tavsiflash",
                f"{request.subject} doirasidagi amaliy qo‘llanishlarni tahlil qilish",
            ],
            introduction=paragraph,
            sections=[
                IndependentWorkSection(
                    heading=f"{section_labels[index - 1]} asosiy yo‘nalish",
                    paragraphs=[paragraph, paragraph],
                )
                for index in range(1, section_count + 1)
            ],
            conclusion=(
                f"{topic_reference} bo‘yicha mustaqil tahlil {request.subject} fanining nazariy "
                "va amaliy jihatlarini bog‘lash zarurligini ko‘rsatadi. Natijalar ehtiyotkor "
                f"umumlashtirildi.{f' {evidence}' if evidence else ''}"
            ),
            requirement_evidence=[evidence] if evidence else [],
            references=[],
        )
        return validate_independent_work_against_request(document, request)
