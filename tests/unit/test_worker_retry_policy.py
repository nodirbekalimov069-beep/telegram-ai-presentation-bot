import dramatiq
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest
from dramatiq.middleware import CurrentMessage

from apps.worker import tasks as worker_tasks
from apps.worker.tasks import (
    GenerationAttemptError,
    _broker,
    _coordination_retry,
    _current_failure_progress_step,
    _generation_retry_when,
    _record_runtime_failure,
)
from modules.presentations.delivery import DeliveryLeaseLost
from modules.presentations.generation import GenerationLeaseLost


def test_active_lease_retry_is_not_exhausted_by_normal_failure_limit() -> None:
    assert _generation_retry_when(999, dramatiq.Retry("lease", delay=1_000)) is True
    assert _generation_retry_when(999, GenerationAttemptError(failures=1)) is True
    assert _generation_retry_when(999, GenerationAttemptError(failures=3)) is True
    assert _generation_retry_when(0, GenerationAttemptError(failures=4)) is False
    assert _generation_retry_when(0, RuntimeError("unclassified")) is False


def test_lease_retry_count_does_not_poison_runtime_failure_budget(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    message = SimpleNamespace(options={"retries": 999})
    monkeypatch.setattr(
        CurrentMessage,
        "get_current_message",
        staticmethod(lambda: message),
    )

    assert _current_failure_progress_step() == "retrying"
    assert _record_runtime_failure() == 1
    assert message.options["generation_runtime_failures"] == 1

    message.options["generation_runtime_failures"] = 3
    assert _current_failure_progress_step() == "failed"


def test_worker_broker_exposes_current_message_retry_count() -> None:
    assert any(isinstance(middleware, CurrentMessage) for middleware in _broker.middleware)


def test_actor_wrapper_counts_runtime_failure_but_not_lease_retry(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    message = SimpleNamespace(options={"retries": 50})
    monkeypatch.setattr(
        CurrentMessage,
        "get_current_message",
        staticmethod(lambda: message),
    )

    async def fail_runtime(presentation_id: int, *, terminal_failure: bool) -> None:
        del presentation_id, terminal_failure
        raise RuntimeError("temporary")

    monkeypatch.setattr(worker_tasks, "_generate", fail_runtime)
    with pytest.raises(GenerationAttemptError) as caught:
        worker_tasks._run_generation_actor(7)
    assert caught.value.failures == 1
    assert message.options["generation_runtime_failures"] == 1

    async def wait_for_lease(presentation_id: int, *, terminal_failure: bool) -> None:
        del presentation_id, terminal_failure
        raise dramatiq.Retry("lease", delay=1_000)

    message.options.pop("generation_runtime_failures")
    monkeypatch.setattr(worker_tasks, "_generate", wait_for_lease)
    with pytest.raises(dramatiq.Retry):
        worker_tasks._run_generation_actor(7)
    assert "generation_runtime_failures" not in message.options


@pytest.mark.parametrize("error", [GenerationLeaseLost(), DeliveryLeaseLost()])
def test_lease_ownership_loss_is_a_coordination_retry(error: Exception) -> None:
    retry = _coordination_retry(error)

    assert isinstance(retry, dramatiq.Retry)
    assert retry.delay == 1_000


@pytest.mark.asyncio
async def test_terminal_setup_failure_is_persisted_before_retry_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = SimpleNamespace(dispose=AsyncMock())
    factory = object()
    marker = AsyncMock(return_value=True)
    monkeypatch.setattr(worker_tasks, "create_engine", lambda settings: engine)
    monkeypatch.setattr(worker_tasks, "create_session_factory", lambda current: factory)
    monkeypatch.setattr(
        worker_tasks,
        "build_storage",
        lambda settings: (_ for _ in ()).throw(RuntimeError("storage setup failed")),
    )
    monkeypatch.setattr(worker_tasks, "mark_terminal_generation_failure", marker)

    with pytest.raises(RuntimeError, match="storage setup failed"):
        await worker_tasks._generate(7, terminal_failure=True)

    marker.assert_awaited_once_with(
        factory,
        presentation_id=7,
        error=ANY,
    )
    engine.dispose.assert_awaited_once_with()
