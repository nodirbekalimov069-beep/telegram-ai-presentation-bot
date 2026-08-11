"""Reply and inline keyboard factories."""

from collections.abc import Sequence

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from apps.bot.localization import Localizer
from modules.service_orders.catalog import SERVICE_CATALOG, FieldSpec, ServiceType


def main_menu_keyboard(
    localizer: Localizer,
    language: str,
    *,
    is_admin: bool,
) -> ReplyKeyboardMarkup:
    labels = [
        [localizer.text(language, "menu.new")],
        [localizer.text(language, "menu.ai_services"), localizer.text(language, "menu.mine")],
        [localizer.text(language, "menu.plans"), localizer.text(language, "menu.free_demo")],
        [localizer.text(language, "menu.help"), localizer.text(language, "menu.settings")],
    ]
    if is_admin:
        labels.append([localizer.text(language, "menu.admin")])
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=item) for item in row] for row in labels],
        resize_keyboard=True,
        input_field_placeholder=localizer.text(language, "menu.input_placeholder"),
    )


def ai_services_keyboard(language: str, localizer: Localizer | None = None) -> InlineKeyboardMarkup:
    localizer = localizer or Localizer.load_default()
    buttons = [
        InlineKeyboardButton(
            text=f"{definition.icon} {localizer.text(language, definition.label_key)}",
            callback_data=f"svc:{service_type.value}",
        )
        for service_type, definition in SERVICE_CATALOG.items()
    ]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    rows.append(
        [
            InlineKeyboardButton(
                text=localizer.text(language, "common.main_menu"), callback_data="nav:home"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def service_field_keyboard(
    field: FieldSpec,
    *,
    token: str,
    field_index: int,
    language: str,
    localizer: Localizer,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=localizer.option_label(language, value),
                callback_data=f"svp:{token}:{field_index}:{value}",
            )
            for value in field.options[index : index + 2]
        ]
        for index in range(0, len(field.options), 2)
    ]
    if field.name == "requirements" and field.optional:
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text=localizer.text(language, "service.requirements.write"),
                        callback_data=f"svw:{token}:{field_index}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=localizer.text(language, "service.requirements.skip"),
                        callback_data=f"svk:{token}:{field_index}",
                    )
                ],
            ]
        )
    rows.extend(_service_token_navigation(token, language, localizer))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def service_confirmation_keyboard(
    *,
    token: str,
    language: str,
    localizer: Localizer,
    service_type: ServiceType | None = None,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=localizer.text(
                    language,
                    "service.essay.submit"
                    if service_type
                    in {
                        ServiceType.ESSAY,
                        ServiceType.INDEPENDENT_WORK,
                        ServiceType.COURSE_WORK,
                        ServiceType.SUMMARY,
                        ServiceType.QUIZ,
                    }
                    else "service.submit",
                ),
                callback_data=f"svs:{token}",
            )
        ],
        [
            InlineKeyboardButton(
                text=localizer.text(language, "service.edit"),
                callback_data=f"sve:{token}",
            )
        ],
    ]
    rows.extend(_service_token_navigation(token, language, localizer))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _service_token_navigation(
    token: str, language: str, localizer: Localizer
) -> list[list[InlineKeyboardButton]]:
    return [
        [
            InlineKeyboardButton(
                text=localizer.text(language, "common.back"), callback_data=f"svb:{token}"
            ),
            InlineKeyboardButton(
                text=localizer.text(language, "common.main_menu"), callback_data="nav:home"
            ),
        ],
        [
            InlineKeyboardButton(
                text=localizer.text(language, "common.cancel"), callback_data=f"svx:{token}"
            )
        ],
    ]


def language_keyboard(
    localizer: Localizer | None = None,
    language: str = "uz",
) -> InlineKeyboardMarkup:
    localizer = localizer or Localizer.load_default()
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="O'zbekcha", callback_data="lang:uz"),
                InlineKeyboardButton(text="Русский", callback_data="lang:ru"),
                InlineKeyboardButton(text="English", callback_data="lang:en"),
            ],
            [
                InlineKeyboardButton(
                    text=localizer.text(language, "common.main_menu"),
                    callback_data="nav:home",
                )
            ],
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
        [
            KeyboardButton(text=localizer.option_label(language, value))
            for value in options[index : index + columns]
        ]
        for index in range(0, len(options), columns)
    ]
    rows.append(
        [
            KeyboardButton(text=localizer.text(language, "common.back")),
            KeyboardButton(text=localizer.text(language, "common.cancel")),
        ]
    )
    rows.append([KeyboardButton(text=localizer.text(language, "common.main_menu"))])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def resolve_choice(
    value: str | None,
    options: Sequence[str],
    localizer: Localizer,
    language: str,
) -> str | None:
    text = (value or "").strip()
    for option in options:
        if text in {option, localizer.option_label(language, option)}:
            return option
    return None


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


def generation_cancel_action(
    localizer: Localizer,
    language: str,
    presentation_id: int,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=localizer.text(language, "common.cancel"),
                    callback_data=f"outline:cancel:{presentation_id}",
                )
            ]
        ]
    )
