"""User-facing status labels and bounded pagination helpers."""

from dataclasses import dataclass

_STATUS_LABELS = {
    "uz": {
        "DRAFT": "🕓 Kutilmoqda",
        "CREATED": "🕓 Kutilmoqda",
        "WAITING_PAYMENT": "🕓 To‘lov kutilmoqda",
        "PAYMENT_PROCESSING": "⏳ To‘lov tekshirilmoqda",
        "PAID": "🕓 Navbatda",
        "OUTLINE_GENERATING": "⏳ Reja tayyorlanmoqda",
        "WAITING_OUTLINE_APPROVAL": "🕓 Tasdiqlash kutilmoqda",
        "QUEUED": "🕓 Navbatda",
        "SUBMITTED": "🕓 Navbatda",
        "GENERATING": "⏳ Tayyorlanmoqda",
        "PROCESSING": "⏳ Tayyorlanmoqda",
        "QUALITY_CHECK": "⏳ Tekshirilmoqda",
        "COMPLETED": "✅ Tayyor",
        "FAILED": "❌ Xatolik",
        "CANCELLED": "❌ Bekor qilingan",
        "REFUND_PENDING": "⏳ Qaytarish kutilmoqda",
        "REFUNDED": "✅ Mablag‘ qaytarilgan",
        "PREVIEW": "🧪 Xizmat tayyorlanmoqda",
    },
    "ru": {
        "DRAFT": "🕓 Ожидает",
        "CREATED": "🕓 Ожидает",
        "WAITING_PAYMENT": "🕓 Ожидает оплату",
        "PAYMENT_PROCESSING": "⏳ Оплата проверяется",
        "PAID": "🕓 В очереди",
        "OUTLINE_GENERATING": "⏳ План создаётся",
        "WAITING_OUTLINE_APPROVAL": "🕓 Ожидает подтверждения",
        "QUEUED": "🕓 В очереди",
        "SUBMITTED": "🕓 В очереди",
        "GENERATING": "⏳ Готовится",
        "PROCESSING": "⏳ Готовится",
        "QUALITY_CHECK": "⏳ Проверяется",
        "COMPLETED": "✅ Готово",
        "FAILED": "❌ Ошибка",
        "CANCELLED": "❌ Отменено",
        "REFUND_PENDING": "⏳ Возврат ожидается",
        "REFUNDED": "✅ Средства возвращены",
        "PREVIEW": "🧪 Сервис готовится",
    },
    "en": {
        "DRAFT": "🕓 Waiting",
        "CREATED": "🕓 Waiting",
        "WAITING_PAYMENT": "🕓 Awaiting payment",
        "PAYMENT_PROCESSING": "⏳ Checking payment",
        "PAID": "🕓 Queued",
        "OUTLINE_GENERATING": "⏳ Preparing outline",
        "WAITING_OUTLINE_APPROVAL": "🕓 Awaiting approval",
        "QUEUED": "🕓 Queued",
        "SUBMITTED": "🕓 Queued",
        "GENERATING": "⏳ Preparing",
        "PROCESSING": "⏳ Preparing",
        "QUALITY_CHECK": "⏳ Quality check",
        "COMPLETED": "✅ Ready",
        "FAILED": "❌ Error",
        "CANCELLED": "❌ Cancelled",
        "REFUND_PENDING": "⏳ Refund pending",
        "REFUNDED": "✅ Refunded",
        "PREVIEW": "🧪 Service in preparation",
    },
}

_DEFAULT_STATUS = {
    "uz": "🕓 Holat yangilanmoqda",
    "ru": "🕓 Статус обновляется",
    "en": "🕓 Status updating",
}


def friendly_order_status(status: str, language: str = "uz") -> str:
    selected = language if language in _STATUS_LABELS else "uz"
    return _STATUS_LABELS[selected].get(status, _DEFAULT_STATUS[selected])


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: tuple[T, ...]
    page: int
    total_pages: int
    total_items: int


def paginate_orders[T](items: list[T], *, page: int, per_page: int = 5) -> Page[T]:
    if not 1 <= per_page <= 10:
        raise ValueError("per_page must be between 1 and 10")
    total_items = len(items)
    total_pages = max(1, (total_items + per_page - 1) // per_page)
    bounded_page = min(max(page, 0), total_pages - 1)
    start = bounded_page * per_page
    return Page(
        items=tuple(items[start : start + per_page]),
        page=bounded_page,
        total_pages=total_pages,
        total_items=total_items,
    )
