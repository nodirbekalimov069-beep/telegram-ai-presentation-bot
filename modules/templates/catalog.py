"""Direction-aware presentation design catalog."""

from dataclasses import dataclass


class TemplateAccessError(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class TemplateSelection:
    style: str
    palette: dict[str, str]
    heading_font: str
    body_font: str
    minimum_font_size: int
    image_style: str
    icon_style: str
    preferred_charts: tuple[str, ...]
    supports_speaker_notes: bool


_DIRECTION_STYLE: dict[str, str] = {
    alias: style
    for style, aliases in (
        ("academic", ("Ta’lim", "Talim", "Образование", "Education")),
        ("business", ("Biznes", "Бизнес", "Business")),
        ("modern", ("Marketing", "Маркетинг")),
        ("startup", ("Startap", "Стартап", "Startup")),
        (
            "startup",
            ("Investor Pitch Deck", "Инвесторский питч"),
        ),
        ("business", ("Savdo", "Продажи", "Sales")),
        ("technology", ("IT",)),
        ("technology", ("Dasturlash", "Программирование", "Programming")),
        (
            "technology",
            ("Sun’iy intellekt", "Искусственный интеллект", "Artificial intelligence"),
        ),
        ("dark", ("Kiberxavfsizlik", "Кибербезопасность", "Cybersecurity")),
        ("medical", ("Tibbiyot", "Медицина", "Medicine")),
        ("medical", ("Biologiya", "Биология", "Biology")),
        ("academic", ("Kimyo", "Химия", "Chemistry")),
        ("academic", ("Fizika", "Физика", "Physics")),
        ("academic", ("Matematika", "Математика", "Mathematics")),
        ("historical", ("Tarix", "История", "History")),
        ("academic", ("Geografiya", "География", "Geography")),
        ("corporate", ("Huquq", "Право", "Law")),
        ("business", ("Iqtisod", "Экономика", "Economics")),
        ("business", ("Moliya", "Финансы", "Finance")),
        ("light", ("Psixologiya", "Психология", "Psychology")),
        ("medical", ("Ekologiya", "Экология", "Ecology")),
        ("creative", ("Arxitektura", "Архитектура", "Architecture")),
        ("creative", ("Dizayn", "Дизайн", "Design")),
        ("light", ("Turizm", "Туризм", "Tourism")),
        ("modern", ("Sport", "Спорт")),
        (
            "modern",
            ("Mahsulot taqdimoti", "Презентация продукта", "Product presentation"),
        ),
        ("corporate", ("Hisobot", "Отчёт", "Report")),
        ("creative", ("Portfolio", "Портфолио")),
        ("modern", ("Boshqa yo‘nalish", "Другое", "Other")),
    )
    for alias in aliases
}

_PALETTES: dict[str, dict[str, str]] = {
    "minimal": {"background": "#FFFFFF", "text": "#172033", "accent": "#2563EB"},
    "academic": {"background": "#F8FAFC", "text": "#13213C", "accent": "#2563EB"},
    "business": {"background": "#FFFFFF", "text": "#172033", "accent": "#1E3A5F"},
    "modern": {"background": "#F7F7FF", "text": "#18182B", "accent": "#5B5BD6"},
    "creative": {"background": "#FFF9F3", "text": "#2F2038", "accent": "#E85D75"},
    "technology": {"background": "#101322", "text": "#F8FAFC", "accent": "#7C3AED"},
    "dark": {"background": "#0B1020", "text": "#F1F5F9", "accent": "#06B6D4"},
    "light": {"background": "#FFFFFF", "text": "#1F2937", "accent": "#0EA5E9"},
    "luxury": {"background": "#111111", "text": "#FFF7DF", "accent": "#D4AF37"},
    "corporate": {"background": "#F7F9FC", "text": "#132238", "accent": "#285A8C"},
    "startup": {"background": "#17152F", "text": "#FFFFFF", "accent": "#8B5CF6"},
    "children": {"background": "#FFF7D6", "text": "#24304A", "accent": "#FF6B6B"},
    "medical": {"background": "#F6FFFD", "text": "#153B45", "accent": "#0F9D8A"},
    "historical": {"background": "#F5EAD8", "text": "#3D2B1F", "accent": "#8B5E3C"},
}

_PLAN_RANK = {"free_demo": 0, "medium": 1, "premium": 2, "premium_plus": 3}
_STYLE_MINIMUM_PLAN = {"luxury": "premium_plus"}


def select_template(
    *,
    direction: str,
    requested_style: str,
    plan_code: str,
) -> TemplateSelection:
    if plan_code not in _PLAN_RANK:
        raise TemplateAccessError(f"unknown plan: {plan_code}")
    style = (
        _DIRECTION_STYLE.get(direction, "modern") if requested_style == "ai" else requested_style
    )
    if style not in _PALETTES:
        raise ValueError(f"unknown design style: {style}")
    minimum_plan = _STYLE_MINIMUM_PLAN.get(style, "free_demo")
    if _PLAN_RANK[plan_code] < _PLAN_RANK[minimum_plan]:
        raise TemplateAccessError(f"style {style!r} requires {minimum_plan}")
    dark_style = style in {"dark", "technology", "startup", "luxury"}
    return TemplateSelection(
        style=style,
        palette=dict(_PALETTES[style]),
        heading_font="Aptos Display",
        body_font="Aptos",
        minimum_font_size=18,
        image_style="editorial-dark" if dark_style else "clean-editorial",
        icon_style="outline",
        preferred_charts=("bar", "line", "donut"),
        supports_speaker_notes=plan_code == "premium_plus",
    )
