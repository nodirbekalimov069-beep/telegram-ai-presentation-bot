"""Catalog-driven FSM for preview AI services."""

import logging
import secrets
from html import escape
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from core.config import Settings
from modules.service_orders.catalog import (
    SERVICE_CATALOG,
    FieldKind,
    ServiceType,
    validate_field_value,
    validate_service_request,
)
from modules.service_orders.service import ServiceOrderService
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.fsm import ServiceFlow
from apps.bot.handlers.common import current_user, find_user, preferred_language, show_main_menu
from apps.bot.keyboards import (
    ai_services_keyboard,
    service_confirmation_keyboard,
    service_field_keyboard,
)
from apps.bot.localization import Localizer

router = Router(name="service-flow")
logger = logging.getLogger(__name__)
_DEFAULT_LOCALIZER = Localizer.load_default()
_BACK_LABELS = frozenset(
    _DEFAULT_LOCALIZER.text(language, "common.back") for language in _DEFAULT_LOCALIZER.languages
)
_CANCEL_LABELS = frozenset(
    _DEFAULT_LOCALIZER.text(language, "common.cancel") for language in _DEFAULT_LOCALIZER.languages
)


async def service_catalog(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
) -> None:
    if message.from_user is None:
        return
    await state.clear()
    user = await current_user(session, message.from_user.id)
    await message.answer(
        f"{localizer.text(user.selected_language, 'service.catalog.title')}\n\n"
        f"{localizer.text(user.selected_language, 'service.catalog.intro')}",
        reply_markup=ai_services_keyboard(user.selected_language, localizer),
    )


@router.callback_query(
    F.data.regexp(r"^svc:(essay|independent|course|summary|quiz|write|rewrite|translate|document)$")
)
async def start_service(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
) -> None:
    await callback.answer()
    if callback.data is None or not isinstance(callback.message, Message):
        return
    user = await current_user(session, callback.from_user.id)
    service_type = ServiceType(callback.data.split(":", 1)[1])
    token = secrets.token_hex(8)
    await state.set_state(ServiceFlow.collecting)
    await state.set_data(
        {
            "service_type": service_type.value,
            "flow_token": token,
            "field_index": 0,
            "fields": {},
            "language": user.selected_language,
        }
    )
    notice_keys = {
        ServiceType.ESSAY: "service.essay.notice",
        ServiceType.INDEPENDENT_WORK: "service.independent.notice",
        ServiceType.COURSE_WORK: "service.course.notice",
        ServiceType.SUMMARY: "service.summary.notice",
    }
    await callback.message.answer(
        localizer.text(
            user.selected_language,
            notice_keys[service_type]
            if SERVICE_CATALOG[service_type].production_ready
            else "service.preview_notice",
        )
    )
    await _prompt_current(callback.message, state, localizer)


async def _prompt_current(message: Message, state: FSMContext, localizer: Localizer) -> None:
    data = await state.get_data()
    definition = SERVICE_CATALOG[ServiceType(str(data["service_type"]))]
    index = int(data["field_index"])
    field = definition.fields[index]
    language = str(data["language"])
    await message.answer(
        localizer.text(language, field.prompt_key),
        reply_markup=service_field_keyboard(
            field,
            token=str(data["flow_token"]),
            field_index=index,
            language=language,
            localizer=localizer,
        ),
    )


@router.message(ServiceFlow.collecting, F.text.in_(_CANCEL_LABELS))
@router.message(ServiceFlow.confirming, F.text.in_(_CANCEL_LABELS))
async def service_cancel_text(
    message: Message,
    state: FSMContext,
    localizer: Localizer,
) -> None:
    data = await state.get_data()
    language = str(data.get("language", "uz"))
    await state.clear()
    await message.answer(localizer.text(language, "service.cancelled"))


@router.message(ServiceFlow.collecting, F.text.in_(_BACK_LABELS))
@router.message(ServiceFlow.confirming, F.text.in_(_BACK_LABELS))
async def service_back_text(
    message: Message,
    state: FSMContext,
    localizer: Localizer,
) -> None:
    data = await state.get_data()
    definition = SERVICE_CATALOG[ServiceType(str(data["service_type"]))]
    language = str(data.get("language", "uz"))
    index = int(data.get("field_index", 0))
    target = min(index - 1, len(definition.fields) - 1)
    if target < 0:
        await state.clear()
        await message.answer(
            localizer.text(language, "service.catalog.title"),
            reply_markup=ai_services_keyboard(language, localizer),
        )
        return
    allowed_names = {field.name for field in definition.fields[:target]}
    fields = {
        key: value for key, value in dict(data.get("fields", {})).items() if key in allowed_names
    }
    await state.set_state(ServiceFlow.collecting)
    await state.update_data(field_index=target, fields=fields)
    await _prompt_current(message, state, localizer)


@router.message(ServiceFlow.collecting)
async def collect_text_field(
    message: Message,
    state: FSMContext,
    localizer: Localizer,
) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    definition = SERVICE_CATALOG[ServiceType(str(data["service_type"]))]
    field = definition.fields[int(data["field_index"])]
    language = str(data["language"])
    if field.kind is not FieldKind.TEXT and field.kind is not FieldKind.INTEGER:
        await message.answer(localizer.text(language, "service.invalid"))
        return
    if message.text is None:
        await message.answer(localizer.text(language, "service.invalid"))
        return
    try:
        value = validate_field_value(definition.service_type, field.name, message.text)
    except ValueError:
        key = "service.too_large" if len(message.text) > field.maximum else "service.invalid"
        await message.answer(localizer.text(language, key))
        return
    await _store_and_advance(message, state, localizer, field.name, value)


@router.callback_query(ServiceFlow.collecting, F.data.startswith("svp:"))
async def collect_choice_field(
    callback: CallbackQuery,
    state: FSMContext,
    localizer: Localizer,
) -> None:
    if callback.data is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    data = await state.get_data()
    parts = callback.data.split(":", 3)
    current_index = int(data["field_index"])
    if (
        len(parts) != 4
        or parts[1] != data.get("flow_token")
        or not parts[2].isdigit()
        or int(parts[2]) != current_index
    ):
        await callback.answer(localizer.text(str(data.get("language", "uz")), "service.expired"))
        return
    definition = SERVICE_CATALOG[ServiceType(str(data["service_type"]))]
    field = definition.fields[current_index]
    if field.kind not in {FieldKind.CHOICE, FieldKind.BOOLEAN}:
        await callback.answer(localizer.text(str(data["language"]), "service.expired"))
        return
    try:
        value = validate_field_value(definition.service_type, field.name, parts[3])
    except ValueError:
        await callback.answer(localizer.text(str(data["language"]), "service.invalid"))
        return
    await callback.answer()
    await _store_and_advance(callback.message, state, localizer, field.name, value)


@router.callback_query(ServiceFlow.collecting, F.data.regexp(r"^sv[kw]:"))
async def optional_requirements_action(
    callback: CallbackQuery,
    state: FSMContext,
    localizer: Localizer,
) -> None:
    if callback.data is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    data = await state.get_data()
    parts = callback.data.split(":", 2)
    current_index = int(data["field_index"])
    language = str(data["language"])
    definition = SERVICE_CATALOG[ServiceType(str(data["service_type"]))]
    field = definition.fields[current_index]
    if (
        len(parts) != 3
        or parts[1] != data.get("flow_token")
        or not parts[2].isdigit()
        or int(parts[2]) != current_index
        or field.name != "requirements"
        or not field.optional
    ):
        await callback.answer(localizer.text(language, "service.expired"))
        return
    await callback.answer()
    if parts[0] == "svw":
        await callback.message.answer(localizer.text(language, "service.requirements.prompt"))
        return
    await _store_and_advance(callback.message, state, localizer, field.name, "")


async def _store_and_advance(
    message: Message,
    state: FSMContext,
    localizer: Localizer,
    field_name: str,
    value: Any,
) -> None:
    data = await state.get_data()
    fields = dict(data.get("fields", {}))
    fields[field_name] = value
    index = int(data["field_index"]) + 1
    definition = SERVICE_CATALOG[ServiceType(str(data["service_type"]))]
    await state.update_data(fields=fields, field_index=index)
    if index < len(definition.fields):
        await _prompt_current(message, state, localizer)
        return
    payload = validate_service_request(definition.service_type, fields)
    await state.update_data(fields=payload)
    await state.set_state(ServiceFlow.confirming)
    language = str(data["language"])
    summary = _safe_summary(definition.service_type, payload, localizer, language)
    await message.answer(
        f"{localizer.text(language, 'service.confirm')}\n\n{summary}",
        reply_markup=service_confirmation_keyboard(
            token=str(data["flow_token"]),
            language=language,
            localizer=localizer,
            service_type=definition.service_type,
        ),
        parse_mode="HTML",
    )


def _safe_summary(
    service_type: ServiceType,
    payload: dict[str, Any],
    localizer: Localizer,
    language: str,
) -> str:
    definition = SERVICE_CATALOG[service_type]
    if service_type is ServiceType.ESSAY:
        requirements = str(payload.get("requirements") or localizer.text(language, "service.none"))
        return "\n".join(
            (
                f"<b>📚 {escape(localizer.text(language, 'service.essay'))}</b>",
                f"<b>{escape(localizer.text(language, 'service.summary.topic'))}</b>: "
                f"{escape(str(payload['topic']))}",
                f"<b>{escape(localizer.text(language, 'service.summary.language'))}</b>: "
                f"{escape(localizer.option_label(language, str(payload['language'])))}",
                f"<b>{escape(localizer.text(language, 'service.summary.length'))}</b>: "
                f"{escape(localizer.option_label(language, str(payload['length'])))}",
                f"<b>{escape(localizer.text(language, 'service.summary.requirements'))}</b>: "
                f"{escape(requirements)}",
            )
        )
    if service_type is ServiceType.INDEPENDENT_WORK:
        requirements = str(payload.get("requirements") or localizer.text(language, "service.none"))
        return "\n".join(
            (
                f"<b>📝 {escape(localizer.text(language, 'service.independent'))}</b>",
                f"<b>{escape(localizer.text(language, 'service.summary.subject'))}</b>: "
                f"{escape(str(payload['subject']))}",
                f"<b>{escape(localizer.text(language, 'service.summary.topic'))}</b>: "
                f"{escape(str(payload['topic']))}",
                f"<b>{escape(localizer.text(language, 'service.summary.length'))}</b>: "
                f"{escape(localizer.option_label(language, str(payload['length'])))}",
                f"<b>{escape(localizer.text(language, 'service.summary.language'))}</b>: "
                f"{escape(localizer.option_label(language, str(payload['language'])))}",
                f"<b>{escape(localizer.text(language, 'service.summary.requirements'))}</b>: "
                f"{escape(requirements)}",
            )
        )
    if service_type is ServiceType.COURSE_WORK:
        requirements = str(payload.get("requirements") or localizer.text(language, "service.none"))
        return "\n".join(
            (
                f"<b>📚 {escape(localizer.text(language, 'service.course'))}</b>",
                f"<b>{escape(localizer.text(language, 'service.summary.subject'))}</b>: "
                f"{escape(str(payload['subject']))}",
                f"<b>{escape(localizer.text(language, 'service.summary.topic'))}</b>: "
                f"{escape(str(payload['topic']))}",
                f"<b>{escape(localizer.text(language, 'service.summary.sections'))}</b>: "
                f"{escape(str(payload['sections']))}",
                f"<b>{escape(localizer.text(language, 'service.summary.length'))}</b>: "
                f"{escape(localizer.option_label(language, str(payload['length'])))}",
                f"<b>{escape(localizer.text(language, 'service.summary.language'))}</b>: "
                f"{escape(localizer.option_label(language, str(payload['language'])))}",
                f"<b>{escape(localizer.text(language, 'service.summary.requirements'))}</b>: "
                f"{escape(requirements)}",
            )
        )
    lines: list[str] = []
    for field in definition.fields:
        value = payload[field.name]
        if field.kind is FieldKind.BOOLEAN:
            shown = localizer.option_label(language, "yes" if bool(value) else "no")
        elif field.options:
            shown = localizer.option_label(language, str(value).lower())
        else:
            shown = str(value)
        compact = " ".join(shown.split())
        if len(compact) > 120:
            compact = f"{compact[:117]}…"
        prompt = localizer.text(language, field.prompt_key).rstrip(":")
        lines.append(f"• <b>{escape(prompt)}</b>: {escape(compact)}")
    return "\n".join(lines)


async def _callback_language(
    data: dict[str, Any],
    callback: CallbackQuery,
    session: AsyncSession,
    localizer: Localizer,
) -> str:
    state_language = str(data.get("language", ""))
    if state_language in localizer.languages:
        return state_language
    user = await find_user(session, callback.from_user.id)
    return preferred_language(user, callback.from_user.language_code, localizer)


@router.callback_query(F.data.startswith("svb:"))
async def service_back(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
) -> None:
    if callback.data is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    data = await state.get_data()
    language = await _callback_language(data, callback, session, localizer)
    if callback.data.split(":", 1)[1] != data.get("flow_token"):
        await callback.answer(localizer.text(language, "service.expired"))
        return
    definition = SERVICE_CATALOG[ServiceType(str(data["service_type"]))]
    index = int(data.get("field_index", 0))
    target = min(index - 1, len(definition.fields) - 1)
    if target < 0:
        await state.clear()
        await callback.answer()
        await callback.message.answer(
            localizer.text(language, "service.catalog.title"),
            reply_markup=ai_services_keyboard(language, localizer),
        )
        return
    allowed_names = {field.name for field in definition.fields[:target]}
    fields = {
        key: value for key, value in dict(data.get("fields", {})).items() if key in allowed_names
    }
    await state.set_state(ServiceFlow.collecting)
    await state.update_data(field_index=target, fields=fields)
    await callback.answer()
    await _prompt_current(callback.message, state, localizer)


@router.callback_query(ServiceFlow.confirming, F.data.startswith("sve:"))
async def edit_service_order(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
) -> None:
    if callback.data is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    data = await state.get_data()
    language = await _callback_language(data, callback, session, localizer)
    if callback.data.split(":", 1)[1] != data.get("flow_token"):
        await callback.answer(localizer.text(language, "service.expired"))
        return
    await state.set_state(ServiceFlow.collecting)
    await state.update_data(field_index=0, fields={}, flow_token=secrets.token_hex(8))
    await callback.answer()
    await _prompt_current(callback.message, state, localizer)


@router.callback_query(F.data.startswith("svx:"))
async def service_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
    settings: Settings,
) -> None:
    data = await state.get_data()
    language = await _callback_language(data, callback, session, localizer)
    if callback.data is None or callback.data.split(":", 1)[1] != data.get("flow_token"):
        await callback.answer(localizer.text(language, "service.expired"))
        return
    await callback.answer()
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer(localizer.text(language, "service.cancelled"))
        user = await current_user(session, callback.from_user.id)
        await show_main_menu(callback.message, user=user, localizer=localizer, settings=settings)


@router.callback_query(ServiceFlow.confirming, F.data.startswith("svs:"))
async def submit_service_order(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
) -> None:
    data = await state.get_data()
    language = str(data.get("language", "uz"))
    if callback.data is None or callback.data.split(":", 1)[1] != data.get("flow_token"):
        await callback.answer(localizer.text(language, "service.expired"))
        return
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    token = str(data["flow_token"])
    try:
        service_type = ServiceType(str(data["service_type"]))
        service = ServiceOrderService()
        if service_type is ServiceType.ESSAY:
            await service.create_essay_order(
                session,
                telegram_user_id=callback.from_user.id,
                request_payload=dict(data["fields"]),
                idempotency_key=f"svc:{callback.from_user.id}:{token}",
            )
        elif service_type is ServiceType.INDEPENDENT_WORK:
            await service.create_independent_work_order(
                session,
                telegram_user_id=callback.from_user.id,
                request_payload=dict(data["fields"]),
                idempotency_key=f"svc:{callback.from_user.id}:{token}",
            )
        elif service_type is ServiceType.COURSE_WORK:
            await service.create_course_work_order(
                session,
                telegram_user_id=callback.from_user.id,
                request_payload=dict(data["fields"]),
                idempotency_key=f"svc:{callback.from_user.id}:{token}",
            )
        elif service_type is ServiceType.SUMMARY:
            await service.create_summary_order(
                session,
                telegram_user_id=callback.from_user.id,
                request_payload=dict(data["fields"]),
                idempotency_key=f"svc:{callback.from_user.id}:{token}",
            )
        else:
            await service.create_preview_order(
                session,
                telegram_user_id=callback.from_user.id,
                service_type=service_type,
                request_payload=dict(data["fields"]),
                idempotency_key=f"svc:{callback.from_user.id}:{token}",
            )
    except (ValueError, PermissionError, LookupError):
        logger.exception(
            "service order submission rejected telegram_user_id=%s", callback.from_user.id
        )
        await callback.message.answer(localizer.text(language, "service.invalid"))
        return
    await state.clear()
    if service_type is ServiceType.ESSAY:
        created_key = "service.essay.created"
    elif service_type is ServiceType.INDEPENDENT_WORK:
        created_key = "service.independent.created"
    elif service_type is ServiceType.COURSE_WORK:
        created_key = "service.course.created"
    elif service_type is ServiceType.SUMMARY:
        created_key = "service.summary.created"
    else:
        created_key = "service.created"
    await callback.message.answer(localizer.text(language, created_key))


@router.callback_query(F.data.regexp(r"^(svc|svp|svw|svk|svb|sve|svx|svs):"))
async def unknown_service_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    localizer: Localizer,
) -> None:
    user = await find_user(session, callback.from_user.id)
    language = preferred_language(user, callback.from_user.language_code, localizer)
    await callback.answer(localizer.text(language, "service.expired"))
