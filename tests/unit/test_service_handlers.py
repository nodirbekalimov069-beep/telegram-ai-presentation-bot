from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from apps.bot.fsm import ServiceFlow
from apps.bot.handlers import services as service_handlers
from apps.bot.localization import Localizer
from core.config import Settings
from modules.service_orders.catalog import SERVICE_CATALOG, FieldKind, ServiceType


class FakeState:
    def __init__(self) -> None:
        self.state: object | None = None
        self.data: dict[str, object] = {}

    async def clear(self) -> None:
        self.state = None
        self.data = {}

    async def set_state(self, state: object) -> None:
        self.state = state

    async def get_state(self) -> object | None:
        return self.state

    async def set_data(self, data: dict[str, object]) -> None:
        self.data = dict(data)

    async def get_data(self) -> dict[str, object]:
        return dict(self.data)

    async def update_data(self, **values: object) -> None:
        self.data.update(values)


SAMPLES: dict[str, str] = {
    "topic": "Sun’iy intellekt",
    "language": "uz",
    "length": "medium",
    "requirements": "Manbalar ko‘rsatilsin",
    "subject": "Informatika",
    "sections": "Kirish, asosiy qism, xulosa",
    "source": "Sun’iy intellekt haqida konspekt uchun yetarli matn",
    "detail": "detailed",
    "question_count": "20",
    "difficulty": "medium",
    "option_count": "4",
    "answer_key": "yes",
    "text_type": "article",
    "style": "academic",
    "text": "Bu qayta ishlash yoki tarjima uchun yetarli sinov matni.",
    "mode": "formal",
    "target_language": "en",
    "document_type": "application",
    "details": "Universitet rektoriga ta’til so‘rab yoziladi",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("service_type", list(ServiceType))
async def test_every_service_fsm_reaches_expected_order_submission(
    monkeypatch: pytest.MonkeyPatch,
    service_type: ServiceType,
) -> None:
    monkeypatch.setattr(service_handlers, "Message", SimpleNamespace)
    monkeypatch.setattr(
        service_handlers,
        "current_user",
        AsyncMock(return_value=SimpleNamespace(selected_language="uz")),
    )
    preview_created = AsyncMock(return_value=SimpleNamespace(id=1))
    essay_created = AsyncMock(return_value=SimpleNamespace(id=1))
    independent_created = AsyncMock(return_value=SimpleNamespace(id=1))
    course_created = AsyncMock(return_value=SimpleNamespace(id=1))
    summary_created = AsyncMock(return_value=SimpleNamespace(id=1))
    quiz_created = AsyncMock(return_value=SimpleNamespace(id=1))
    write_text_created = AsyncMock(return_value=SimpleNamespace(id=1))
    rewrite_created = AsyncMock(return_value=SimpleNamespace(id=1))
    monkeypatch.setattr(
        service_handlers,
        "ServiceOrderService",
        Mock(
            return_value=SimpleNamespace(
                create_preview_order=preview_created,
                create_essay_order=essay_created,
                create_independent_work_order=independent_created,
                create_course_work_order=course_created,
                create_summary_order=summary_created,
                create_quiz_order=quiz_created,
                create_write_text_order=write_text_created,
                create_rewrite_order=rewrite_created,
            )
        ),
    )
    state = FakeState()
    message = SimpleNamespace(answer=AsyncMock(), from_user=SimpleNamespace(id=777))
    start_callback = SimpleNamespace(
        data=f"svc:{service_type.value}",
        message=message,
        from_user=SimpleNamespace(id=777),
        answer=AsyncMock(),
    )

    await service_handlers.start_service(
        start_callback,
        state,  # type: ignore[arg-type]
        AsyncMock(),
        Localizer.load_default(),
    )

    definition = SERVICE_CATALOG[service_type]
    if not definition.production_ready:
        assert state.state is None
        preview_created.assert_not_awaited()
        essay_created.assert_not_awaited()
        independent_created.assert_not_awaited()
        course_created.assert_not_awaited()
        summary_created.assert_not_awaited()
        quiz_created.assert_not_awaited()
        write_text_created.assert_not_awaited()
        rewrite_created.assert_not_awaited()
        return

    for index, field in enumerate(definition.fields):
        token = str(state.data["flow_token"])
        if field.kind in {FieldKind.TEXT, FieldKind.INTEGER}:
            input_message = SimpleNamespace(
                text=SAMPLES[field.name],
                from_user=SimpleNamespace(id=777),
                answer=AsyncMock(),
            )
            await service_handlers.collect_text_field(
                input_message,
                state,  # type: ignore[arg-type]
                Localizer.load_default(),
            )
        else:
            choice_callback = SimpleNamespace(
                data=f"svp:{token}:{index}:{SAMPLES[field.name]}",
                message=message,
                from_user=SimpleNamespace(id=777),
                answer=AsyncMock(),
            )
            await service_handlers.collect_choice_field(
                choice_callback,
                state,  # type: ignore[arg-type]
                Localizer.load_default(),
            )

    assert state.state is ServiceFlow.confirming
    preview_created.assert_not_awaited()
    essay_created.assert_not_awaited()
    independent_created.assert_not_awaited()
    course_created.assert_not_awaited()
    summary_created.assert_not_awaited()
    quiz_created.assert_not_awaited()
    write_text_created.assert_not_awaited()
    rewrite_created.assert_not_awaited()
    token = str(state.data["flow_token"])
    submit_callback = SimpleNamespace(
        data=f"svs:{token}",
        message=message,
        from_user=SimpleNamespace(id=777),
        answer=AsyncMock(),
    )
    await service_handlers.submit_service_order(
        submit_callback,
        state,  # type: ignore[arg-type]
        AsyncMock(),
        Localizer.load_default(),
    )

    if service_type is ServiceType.ESSAY:
        created = essay_created
    elif service_type is ServiceType.INDEPENDENT_WORK:
        created = independent_created
    elif service_type is ServiceType.COURSE_WORK:
        created = course_created
    elif service_type is ServiceType.SUMMARY:
        created = summary_created
    elif service_type is ServiceType.QUIZ:
        created = quiz_created
    elif service_type is ServiceType.WRITE_TEXT:
        created = write_text_created
    elif service_type is ServiceType.REWRITE:
        created = rewrite_created
    else:
        created = preview_created
    created.assert_awaited_once()
    call = created.await_args.kwargs
    assert call["telegram_user_id"] == 777
    if service_type not in {
        ServiceType.ESSAY,
        ServiceType.INDEPENDENT_WORK,
        ServiceType.COURSE_WORK,
        ServiceType.SUMMARY,
        ServiceType.QUIZ,
        ServiceType.WRITE_TEXT,
        ServiceType.REWRITE,
    }:
        assert call["service_type"] is service_type
    assert call["idempotency_key"].startswith("svc:777:")
    assert state.state is None


@pytest.mark.asyncio
async def test_oversized_service_text_is_rejected_without_advancing() -> None:
    state = FakeState()
    await state.set_state(ServiceFlow.collecting)
    await state.set_data(
        {
            "service_type": ServiceType.REWRITE.value,
            "flow_token": "12345678",
            "field_index": 0,
            "fields": {},
            "language": "uz",
        }
    )
    message = SimpleNamespace(
        text="x" * 4001,
        from_user=SimpleNamespace(id=777),
        answer=AsyncMock(),
    )

    await service_handlers.collect_text_field(
        message,
        state,  # type: ignore[arg-type]
        Localizer.load_default(),
    )

    assert state.data["field_index"] == 0
    message.answer.assert_awaited_once_with("Matn juda katta. Qisqartirib qayta yuboring.")


@pytest.mark.asyncio
async def test_stale_choice_callback_cannot_mutate_active_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_handlers, "Message", SimpleNamespace)
    state = FakeState()
    await state.set_state(ServiceFlow.collecting)
    await state.set_data(
        {
            "service_type": ServiceType.ESSAY.value,
            "flow_token": "active123",
            "field_index": 1,
            "fields": {"topic": "AI"},
            "language": "uz",
        }
    )
    callback = SimpleNamespace(
        data="svp:stale456:1:uz",
        message=SimpleNamespace(answer=AsyncMock()),
        answer=AsyncMock(),
    )

    await service_handlers.collect_choice_field(
        callback,
        state,  # type: ignore[arg-type]
        Localizer.load_default(),
    )

    assert state.data["fields"] == {"topic": "AI"}
    assert callback.answer.await_args.args


@pytest.mark.asyncio
async def test_old_choice_from_same_flow_cannot_fill_a_later_text_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_handlers, "Message", SimpleNamespace)
    state = FakeState()
    await state.set_state(ServiceFlow.collecting)
    await state.set_data(
        {
            "service_type": ServiceType.ESSAY.value,
            "flow_token": "active123",
            "field_index": 3,
            "fields": {"topic": "AI", "language": "uz", "length": "short"},
            "language": "uz",
        }
    )
    callback = SimpleNamespace(
        data="svp:active123:1:ru",
        message=SimpleNamespace(answer=AsyncMock()),
        answer=AsyncMock(),
    )

    await service_handlers.collect_choice_field(
        callback,
        state,  # type: ignore[arg-type]
        Localizer.load_default(),
    )

    assert state.data["field_index"] == 3
    fields = dict(state.data["fields"])  # type: ignore[arg-type]
    assert "requirements" not in fields
    callback.answer.assert_awaited_once_with("Bu tugma eskirgan. Xizmatni qayta tanlang.")


@pytest.mark.asyncio
async def test_service_back_removes_the_previous_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_handlers, "Message", SimpleNamespace)
    state = FakeState()
    await state.set_state(ServiceFlow.collecting)
    await state.set_data(
        {
            "service_type": ServiceType.ESSAY.value,
            "flow_token": "active123",
            "field_index": 1,
            "fields": {"topic": "AI"},
            "language": "uz",
        }
    )
    callback = SimpleNamespace(
        data="svb:active123",
        message=SimpleNamespace(answer=AsyncMock()),
        answer=AsyncMock(),
    )

    await service_handlers.service_back(
        callback,
        state,  # type: ignore[arg-type]
        AsyncMock(),
        Localizer.load_default(),
    )

    assert state.data["field_index"] == 0
    assert state.data["fields"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["back", "cancel"])
async def test_stale_navigation_callbacks_use_saved_language(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    monkeypatch.setattr(service_handlers, "Message", SimpleNamespace)
    monkeypatch.setattr(
        service_handlers,
        "find_user",
        AsyncMock(return_value=SimpleNamespace(selected_language="ru")),
    )
    state = FakeState()
    callback = SimpleNamespace(
        data="svb:expired" if action == "back" else "svx:expired",
        message=SimpleNamespace(answer=AsyncMock()),
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=777, language_code="en"),
    )
    if action == "back":
        await service_handlers.service_back(
            callback,
            state,  # type: ignore[arg-type]
            AsyncMock(),
            Localizer.load_default(),
        )
    else:
        await service_handlers.service_cancel(
            callback,
            state,  # type: ignore[arg-type]
            AsyncMock(),
            Localizer.load_default(),
            Settings(app_env="test"),
        )

    callback.answer.assert_awaited_once_with("Эта кнопка устарела. Выберите сервис заново.")


@pytest.mark.asyncio
async def test_unknown_service_callback_uses_telegram_language_for_unregistered_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_handlers, "find_user", AsyncMock(return_value=None))
    callback = SimpleNamespace(
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=888, language_code="en-US"),
    )
    await service_handlers.unknown_service_callback(
        callback,
        AsyncMock(),
        Localizer.load_default(),
    )
    callback.answer.assert_awaited_once_with(
        "This button has expired. Please choose the service again."
    )


@pytest.mark.asyncio
async def test_essay_requirements_can_be_skipped_with_scoped_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_handlers, "Message", SimpleNamespace)
    state = FakeState()
    await state.set_state(ServiceFlow.collecting)
    await state.set_data(
        {
            "service_type": ServiceType.ESSAY.value,
            "flow_token": "active123",
            "field_index": 3,
            "fields": {"topic": "AI", "language": "uz", "length": "short"},
            "language": "uz",
        }
    )
    callback = SimpleNamespace(
        data="svk:active123:3",
        message=SimpleNamespace(answer=AsyncMock()),
        answer=AsyncMock(),
    )
    await service_handlers.optional_requirements_action(
        callback,
        state,
        Localizer.load_default(),  # type: ignore[arg-type]
    )
    assert state.state is ServiceFlow.confirming
    assert state.data["fields"]["requirements"] == ""  # type: ignore[index]


@pytest.mark.asyncio
async def test_confirmation_edit_restarts_same_scoped_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_handlers, "Message", SimpleNamespace)
    state = FakeState()
    await state.set_state(ServiceFlow.confirming)
    await state.set_data(
        {
            "service_type": ServiceType.ESSAY.value,
            "flow_token": "active123",
            "field_index": 4,
            "fields": {"topic": "AI", "language": "uz", "length": "short"},
            "language": "uz",
        }
    )
    callback = SimpleNamespace(
        data="sve:active123",
        message=SimpleNamespace(answer=AsyncMock()),
        from_user=SimpleNamespace(id=777, language_code="uz"),
        answer=AsyncMock(),
    )
    await service_handlers.edit_service_order(
        callback,
        state,
        AsyncMock(),
        Localizer.load_default(),  # type: ignore[arg-type]
    )
    assert state.state is ServiceFlow.collecting
    assert state.data["field_index"] == 0
    assert state.data["fields"] == {}
    assert state.data["flow_token"] != "active123"


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["uz", "ru", "en"])
async def test_persistent_cancel_label_never_becomes_service_payload(language: str) -> None:
    localizer = Localizer.load_default()
    state = FakeState()
    await state.set_state(ServiceFlow.collecting)
    await state.set_data(
        {
            "service_type": ServiceType.ESSAY.value,
            "flow_token": "active123",
            "field_index": 0,
            "fields": {},
            "language": language,
        }
    )
    message = SimpleNamespace(
        text=localizer.text(language, "common.cancel"),
        answer=AsyncMock(),
    )

    await service_handlers.service_cancel_text(message, state, localizer)  # type: ignore[arg-type]

    assert state.state is None
    assert state.data == {}
    message.answer.assert_awaited_once_with(localizer.text(language, "service.cancelled"))


@pytest.mark.asyncio
async def test_persistent_back_label_uses_service_navigation_not_payload() -> None:
    localizer = Localizer.load_default()
    state = FakeState()
    await state.set_state(ServiceFlow.collecting)
    await state.set_data(
        {
            "service_type": ServiceType.ESSAY.value,
            "flow_token": "active123",
            "field_index": 1,
            "fields": {"topic": "AI"},
            "language": "uz",
        }
    )
    message = SimpleNamespace(
        text=localizer.text("uz", "common.back"),
        answer=AsyncMock(),
    )

    await service_handlers.service_back_text(message, state, localizer)  # type: ignore[arg-type]

    assert state.data["field_index"] == 0
    assert state.data["fields"] == {}


def test_boolean_confirmation_uses_localized_yes_no_not_raw_true() -> None:
    localizer = Localizer.load_default()
    payload = {
        "topic": "AI testi",
        "language": "uz",
        "difficulty": "medium",
        "question_count": 10,
        "option_count": 4,
        "answer_key": True,
        "requirements": "",
    }

    for language, expected in (("uz", "Ha"), ("ru", "Да"), ("en", "Yes")):
        summary = service_handlers._safe_summary(ServiceType.QUIZ, payload, localizer, language)
        assert expected in summary
        assert "true" not in summary.lower()
