from unittest.mock import AsyncMock

import pytest

from apps.worker import tasks


def test_service_essay_actor_finalizes_after_timeout_budget_exhaustion(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    process = AsyncMock()
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(
        tasks,
        "_current_timeout_failures",
        lambda: tasks._settings.generation_max_retries + 1,
    )
    monkeypatch.setattr(tasks, "_process_service_essay", process)
    monkeypatch.setattr(tasks, "_finalize_service_essay", finalize)

    tasks._run_service_essay_actor(42)

    finalize.assert_awaited_once_with(42)
    process.assert_not_awaited()


def test_service_essay_actor_retries_terminal_persistence_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(tasks, "_current_timeout_failures", lambda: 0)
    monkeypatch.setattr(
        tasks,
        "_record_runtime_failure",
        lambda: tasks._settings.generation_max_retries + 1,
    )
    monkeypatch.setattr(
        tasks,
        "_process_service_essay",
        AsyncMock(side_effect=RuntimeError("transient database outage")),
    )
    finalize = AsyncMock(side_effect=RuntimeError("terminal write unavailable"))
    monkeypatch.setattr(tasks, "_finalize_service_essay", finalize)

    with pytest.raises(tasks.dramatiq.Retry):
        tasks._run_service_essay_actor(43)

    finalize.assert_awaited_once_with(43)


def test_service_essay_actor_stops_after_terminal_state_is_persisted(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(tasks, "_current_timeout_failures", lambda: 0)
    monkeypatch.setattr(
        tasks,
        "_record_runtime_failure",
        lambda: tasks._settings.generation_max_retries + 1,
    )
    monkeypatch.setattr(
        tasks,
        "_process_service_essay",
        AsyncMock(side_effect=RuntimeError("terminal generation failure")),
    )
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(tasks, "_finalize_service_essay", finalize)

    tasks._run_service_essay_actor(44)

    finalize.assert_awaited_once_with(44)


def test_cleanup_failure_is_requeued_through_database_outbox(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    cleanup = AsyncMock(side_effect=ConnectionError("object store unavailable"))
    requeue = AsyncMock()
    monkeypatch.setattr(tasks, "_cleanup_service_essay_artifacts", cleanup)
    monkeypatch.setattr(tasks, "_requeue_service_essay_cleanup", requeue)

    tasks._run_service_essay_cleanup_actor(51, 61)

    cleanup.assert_awaited_once_with(51)
    requeue.assert_awaited_once()
    assert requeue.await_args_list[0].args[0] == 61


def test_cleanup_requeue_persistence_failure_uses_coordination_retry(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        tasks,
        "_cleanup_service_essay_artifacts",
        AsyncMock(side_effect=ConnectionError("object store unavailable")),
    )
    monkeypatch.setattr(
        tasks,
        "_requeue_service_essay_cleanup",
        AsyncMock(side_effect=ConnectionError("database unavailable")),
    )

    with pytest.raises(tasks.dramatiq.Retry) as retry:
        tasks._run_service_essay_cleanup_actor(52, 62)

    assert tasks._cleanup_persistence_retry_when(10_000, retry.value)


def test_service_independent_actor_finalizes_after_timeout_budget_exhaustion(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    process = AsyncMock()
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(
        tasks,
        "_current_timeout_failures",
        lambda: tasks._settings.generation_max_retries + 1,
    )
    monkeypatch.setattr(tasks, "_process_service_independent", process)
    monkeypatch.setattr(tasks, "_finalize_service_independent", finalize)

    tasks._run_service_independent_actor(72)

    finalize.assert_awaited_once_with(72)
    process.assert_not_awaited()


def test_independent_cleanup_failure_requeues_exact_outbox_event(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    cleanup = AsyncMock(side_effect=ConnectionError("object store unavailable"))
    requeue = AsyncMock()
    monkeypatch.setattr(tasks, "_cleanup_service_independent_artifacts", cleanup)
    monkeypatch.setattr(tasks, "_requeue_service_independent_cleanup", requeue)

    tasks._run_service_independent_cleanup_actor(73, 83)

    cleanup.assert_awaited_once_with(73)
    assert requeue.await_args.args[0] == 83


def test_service_course_actor_finalizes_after_timeout_budget_exhaustion(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    process = AsyncMock()
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(
        tasks,
        "_current_timeout_failures",
        lambda: tasks._settings.generation_max_retries + 1,
    )
    monkeypatch.setattr(tasks, "_process_service_course", process)
    monkeypatch.setattr(tasks, "_finalize_service_course", finalize)

    tasks._run_service_course_actor(92)

    finalize.assert_awaited_once_with(92)
    process.assert_not_awaited()


def test_summary_actor_finalizes_after_timeout_budget_exhaustion(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    process = AsyncMock()
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(
        tasks,
        "_current_timeout_failures",
        lambda: tasks._settings.generation_max_retries + 1,
    )
    monkeypatch.setattr(tasks, "_process_service_summary", process)
    monkeypatch.setattr(tasks, "_finalize_service_summary", finalize)

    tasks._run_service_summary_actor(112)

    finalize.assert_awaited_once_with(112)
    process.assert_not_awaited()


def test_summary_cleanup_failure_requeues_exact_outbox_event(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    cleanup = AsyncMock(side_effect=ConnectionError("object store unavailable"))
    requeue = AsyncMock()
    monkeypatch.setattr(tasks, "_cleanup_service_summary_artifacts", cleanup)
    monkeypatch.setattr(tasks, "_requeue_service_summary_cleanup", requeue)

    tasks._run_service_summary_cleanup_actor(113, 123)

    cleanup.assert_awaited_once_with(113)
    assert requeue.await_args.args[0] == 123


def test_quiz_actor_finalizes_after_timeout_budget_exhaustion(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    process = AsyncMock()
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(
        tasks,
        "_current_timeout_failures",
        lambda: tasks._settings.generation_max_retries + 1,
    )
    monkeypatch.setattr(tasks, "_process_service_quiz", process)
    monkeypatch.setattr(tasks, "_finalize_service_quiz", finalize)

    tasks._run_service_quiz_actor(132)

    finalize.assert_awaited_once_with(132)
    process.assert_not_awaited()


def test_quiz_cleanup_failure_requeues_exact_outbox_event(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    cleanup = AsyncMock(side_effect=ConnectionError("object store unavailable"))
    requeue = AsyncMock()
    monkeypatch.setattr(tasks, "_cleanup_service_quiz_artifacts", cleanup)
    monkeypatch.setattr(tasks, "_requeue_service_quiz_cleanup", requeue)

    tasks._run_service_quiz_cleanup_actor(133, 143)

    cleanup.assert_awaited_once_with(133)
    assert requeue.await_args.args[0] == 143


def test_course_cleanup_failure_requeues_exact_outbox_event(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    cleanup = AsyncMock(side_effect=ConnectionError("object store unavailable"))
    requeue = AsyncMock()
    monkeypatch.setattr(tasks, "_cleanup_service_course_artifacts", cleanup)
    monkeypatch.setattr(tasks, "_requeue_service_course_cleanup", requeue)

    tasks._run_service_course_cleanup_actor(93, 103)

    cleanup.assert_awaited_once_with(93)
    assert requeue.await_args.args[0] == 103
