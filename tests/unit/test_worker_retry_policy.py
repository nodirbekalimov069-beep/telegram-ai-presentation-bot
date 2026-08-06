import dramatiq

from apps.worker.tasks import _generation_retry_when


def test_active_lease_retry_is_not_exhausted_by_normal_failure_limit() -> None:
    assert _generation_retry_when(999, dramatiq.Retry("lease", delay=1_000)) is True
    assert _generation_retry_when(0, RuntimeError("temporary")) is True
    assert _generation_retry_when(3, RuntimeError("still failing")) is False
