"""Current and legacy reply-menu labels for safe keyboard upgrades."""

from apps.bot.localization import Localizer

_LEGACY_LABELS: dict[str, frozenset[str]] = {
    "menu.new": frozenset(
        {
            "🚀 Yangi prezentatsiya",
            "🚀 Новая презентация",
            "🚀 New presentation",
        }
    ),
    "menu.mine": frozenset(
        {
            "📂 Mening prezentatsiyalarim",
            "📂 Мои презентации",
            "📂 My presentations",
        }
    ),
    "menu.plans": frozenset({"💎 Tariflar", "💎 Тарифы", "💎 Plans"}),
    "menu.free_demo": frozenset({"🎁 Free Demo"}),
}


def menu_labels(localizer: Localizer, key: str) -> set[str]:
    current = {localizer.text(language, key) for language in localizer.languages}
    return current | set(_LEGACY_LABELS.get(key, ()))
