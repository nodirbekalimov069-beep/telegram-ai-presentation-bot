from pathlib import Path

import pytest

from modules.files.storage import LocalStorage, StorageAccessDenied


@pytest.mark.asyncio
async def test_local_storage_creates_scoped_unique_key(tmp_path: Path) -> None:
    source = tmp_path / "../../unsafe.pptx"
    source = tmp_path / "unsafe.pptx"
    source.write_bytes(b"pptx-data")
    storage = LocalStorage(tmp_path / "objects")

    key = await storage.put_file(
        source,
        user_id=12,
        presentation_id=34,
        artifact_type="pptx",
    )

    assert key.startswith("users/12/presentations/34/")
    assert key.endswith(".pptx")
    assert await storage.exists(key)
    assert (tmp_path / "objects" / key).read_bytes() == b"pptx-data"


@pytest.mark.asyncio
async def test_user_cannot_resolve_another_users_artifact(tmp_path: Path) -> None:
    source = tmp_path / "result.pdf"
    source.write_bytes(b"pdf")
    storage = LocalStorage(tmp_path / "objects")
    key = await storage.put_file(
        source,
        user_id=1,
        presentation_id=2,
        artifact_type="pdf",
    )

    with pytest.raises(StorageAccessDenied):
        await storage.path_for_user(key, user_id=999)
