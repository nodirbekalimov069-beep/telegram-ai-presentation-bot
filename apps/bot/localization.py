"""JSON-backed localization with Uzbek fallback."""

import json
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any


class LocalizationError(RuntimeError):
    pass


class Localizer:
    def __init__(self, catalogs: dict[str, dict[str, str]], *, fallback: str = "uz") -> None:
        if fallback not in catalogs:
            raise LocalizationError("fallback locale is missing")
        expected = set(catalogs[fallback])
        for language, catalog in catalogs.items():
            if set(catalog) != expected:
                missing = sorted(expected - set(catalog))
                extra = sorted(set(catalog) - expected)
                raise LocalizationError(f"locale {language}: missing={missing}, extra={extra}")
        self._catalogs = catalogs
        self._fallback = fallback

    @classmethod
    def load_default(cls) -> "Localizer":
        root = Path(__file__).with_name("locales")
        catalogs = {
            language: json.loads((root / f"{language}.json").read_text(encoding="utf-8"))
            for language in ("uz", "ru", "en")
        }
        return cls(catalogs)

    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(self._catalogs)

    def catalog(self, language: str) -> Mapping[str, str]:
        return MappingProxyType(self._catalogs.get(language, self._catalogs[self._fallback]))

    def text(self, language: str, key: str, **values: Any) -> str:
        catalog = self._catalogs.get(language, self._catalogs[self._fallback])
        try:
            template = catalog[key]
        except KeyError as exc:
            raise LocalizationError(f"unknown translation key: {key}") from exc
        return template.format(**values)

    def option_label(self, language: str, value: str) -> str:
        catalog = self._catalogs.get(language, self._catalogs[self._fallback])
        return catalog.get(f"option.{value}", value)
