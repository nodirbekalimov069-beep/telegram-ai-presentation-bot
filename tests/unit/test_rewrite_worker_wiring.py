import inspect

from apps.worker import outbox_dispatcher, tasks


def test_rewrite_worker_and_outbox_wiring_is_bounded_and_complete() -> None:
    assert tasks.generate_service_rewrite.actor_name.endswith("generate_service_rewrite")
    assert tasks.cleanup_service_rewrite_artifacts.actor_name.endswith(
        "cleanup_service_rewrite_artifacts"
    )
    generate_options = tasks.generate_service_rewrite.options
    cleanup_options = tasks.cleanup_service_rewrite_artifacts.options
    assert generate_options["time_limit"] == 600_000
    assert cleanup_options["time_limit"] == 300_000
    assert cleanup_options["max_retries"] == 10

    source = inspect.getsource(outbox_dispatcher.run_dispatcher)
    assert '"service.rewrite.requested": publish_rewrite' in source
    assert '"service.rewrite.cleanup_requested": publish_rewrite_cleanup' in source
    assert "generate_service_rewrite.send" in source
    assert "cleanup_service_rewrite_artifacts.send" in source
