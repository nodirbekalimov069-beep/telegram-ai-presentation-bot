"""Reply and inline keyboard factories."""

from collections.abc import Sequence

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from apps.bot.localization import Localizer


def main_menu_keyboard(
    localizer: Localizer,
    language: str,
    *,
    is_admin: bool,
) -> ReplyKeyboardMarkup:
    labels = [
        [localizer.text(language, "menu.new")],
        [localizer.text(language, "menu.mine"), localizer.text(language, "menu.plans")],
        [localizer.text(language, "menu.free_demo"), localizer.text(language, "menu.profile")],
        [localizer.text(language, "menu.help"), localizer.text(language, "menu.settings")],
    ]
    if is_admin:
        labels.append([localizer.text(language, "menu.admin")])
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=item) for item in row] for row in labels],
        resize_keyboard=True,
        input_field_placeholder=localizer.text(language, "menu.input_placeholder"),
    )


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="O'zbekcha", callback_data="lang:uz"),
                InlineKeyboardButton(text="Русский", callback_data="lang:ru"),
                InlineKeyboardButton(text="English", callback_data="lang:en"),
            ]
        ]
    )


def choices_keyboard(
    options: Sequence[str],
    localizer: Localizer,
    language: str,
    *,
    columns: int = 2,
) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=value) for value in options[index : index + columns]]
        for index in range(0, len(options), columns)
    ]
    rows.append(
        [
            KeyboardButton(text=localizer.text(language, "common.back")),
            KeyboardButton(text=localizer.text(language, "common.cancel")),
        ]
    )
    rows.append([KeyboardButton(text=localizer.text(language, "common.main_menu"))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def outline_actions(
    localizer: Localizer,
    language: str,
    presentation_id: int | None = None,
) -> InlineKeyboardMarkup:
    suffix = f":{presentation_id}" if presentation_id is not None else ""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=localizer.text(language, "create.approve"),
                    callback_data=f"outline:approve{suffix}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=localizer.text(language, "create.regenerate"),
                    callback_data=f"outline:regenerate{suffix}",
                ),
                InlineKeyboardButton(
                    text=localizer.text(language, "create.edit"),
                    callback_data=f"outline:edit{suffix}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=localizer.text(language, "common.cancel"),
                    callback_data=f"outline:cancel{suffix}",
                )
            ],
        ]
    )
