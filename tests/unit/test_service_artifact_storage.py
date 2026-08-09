from pathlib import Path

import pytest

from modules.files.s3_storage import S3Storage
from modules.files.storage import LocalStorage, StorageAccessDenied


async def test_service_artifact_key_is_deterministic_scoped_and_download_authorized(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.docx"
    source.write_bytes(b"PK\x03\x04-safe-docx")
    storage = LocalStorage(tmp_path / "storage")

    key = await storage.put_service_file(
        source,
        user_id=17,
        service_order_id=42,
        artifact_type="docx",
        attempt_token="a" * 32,
    )

    assert key == f"users/17/service-orders/42/attempts/{'a' * 32}/referat_42.docx"
    target = tmp_path / "download.docx"
    downloaded = await storage.download_for_user(key, target, user_id=17)
    assert downloaded.read_bytes() == source.read_bytes()
    with pytest.raises(StorageAccessDenied):
        await storage.download_for_user(key, tmp_path / "stolen.docx", user_id=18)


async def test_service_artifact_storage_rejects_type_and_path_confusion(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("unsafe")
    storage = LocalStorage(tmp_path / "storage")

    with pytest.raises(ValueError):
        await storage.put_service_file(
            source,
            user_id=1,
            service_order_id=1,
            artifact_type="../../secret",
            attempt_token="a" * 32,
        )
    with pytest.raises(ValueError):
        await storage.put_service_file(
            source,
            user_id=0,
            service_order_id=1,
            artifact_type="docx",
            attempt_token="a" * 32,
        )


async def test_independent_work_artifact_key_uses_safe_service_filename(tmp_path: Path) -> None:
    source = tmp_path / "work.docx"
    source.write_bytes(b"PK\x03\x04-safe-docx")
    storage = LocalStorage(tmp_path / "storage")

    key = await storage.put_service_file(
        source,
        user_id=17,
        service_order_id=43,
        artifact_type="docx",
        attempt_token="c" * 32,
        service_type="independent",
    )

    assert key == f"users/17/service-orders/43/attempts/{'c' * 32}/mustaqil_ish_43.docx"


async def test_s3_service_artifact_attempt_namespace_and_owner_delete(tmp_path: Path) -> None:
    class FakeS3Client:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}

        def upload_file(self, path: str, bucket: str, key: str, **_: object) -> None:
            assert bucket == "artifacts"
            self.objects[key] = Path(path).read_bytes()

        def delete_object(self, *, Bucket: str, Key: str) -> None:
            assert Bucket == "artifacts"
            self.objects.pop(Key, None)

    source = tmp_path / "referat.docx"
    source.write_bytes(b"safe")
    client = FakeS3Client()
    storage = S3Storage(
        bucket="artifacts",
        endpoint_url=None,
        access_key="synthetic",
        secret_key="synthetic",
        region="test",
        client=client,  # type: ignore[arg-type]
    )
    key = await storage.put_service_file(
        source,
        user_id=7,
        service_order_id=9,
        artifact_type="docx",
        attempt_token="b" * 32,
    )
    assert key == f"users/7/service-orders/9/attempts/{'b' * 32}/referat_9.docx"
    with pytest.raises(StorageAccessDenied):
        await storage.delete_for_user(key, user_id=8)
    await storage.delete_for_user(key, user_id=7)
    assert key not in client.objects
