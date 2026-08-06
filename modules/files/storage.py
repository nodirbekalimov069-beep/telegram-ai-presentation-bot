"""Local and abstract artifact storage."""

import asyncio
import shutil
from pathlib import Path
from typing import Protocol
from uuid import uuid4


class StorageAccessDenied(PermissionError):
    pass


class Storage(Protocol):
    async def put_file(
        self,
        source: Path,
        *,
        user_id: int,
        presentation_id: int,
        artifact_type: str,
    ) -> str: ...

    async def exists(self, key: str) -> bool: ...

    async def download_for_user(self, key: str, target: Path, *, user_id: int) -> Path: ...


class LocalStorage:
    _extensions = {"pptx": ".pptx", "pdf": ".pdf", "preview": ".png"}

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    async def put_file(
        self,
        source: Path,
        *,
        user_id: int,
        presentation_id: int,
        artifact_type: str,
    ) -> str:
        if artifact_type not in self._extensions:
            raise ValueError(f"unsupported artifact type: {artifact_type}")
        source_path = source.expanduser().resolve(strict=True)
        key = (
            f"users/{user_id}/presentations/{presentation_id}/"
            f"{uuid4().hex}{self._extensions[artifact_type]}"
        )
        target = self._safe_path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, source_path, target)
        return key

    async def exists(self, key: str) -> bool:
        return self._safe_path(key).is_file()

    async def path_for_user(self, key: str, *, user_id: int) -> Path:
        expected_prefix = f"users/{user_id}/"
        if not key.startswith(expected_prefix):
            raise StorageAccessDenied("artifact does not belong to this user")
        path = self._safe_path(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        return path

    async def download_for_user(self, key: str, target: Path, *, user_id: int) -> Path:
        del target
        return await self.path_for_user(key, user_id=user_id)

    def _safe_path(self, key: str) -> Path:
        if key.startswith("/") or ".." in Path(key).parts:
            raise StorageAccessDenied("unsafe storage key")
        candidate = (self._root / key).resolve()
        if self._root not in candidate.parents:
            raise StorageAccessDenied("storage key escapes root")
        return candidate
