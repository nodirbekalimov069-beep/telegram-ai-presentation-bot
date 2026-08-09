"""Local and abstract artifact storage."""

import asyncio
import shutil
from pathlib import Path
from typing import Protocol
from uuid import uuid4


class StorageAccessDenied(PermissionError):
    pass


class Storage(Protocol):
    def service_file_key(
        self,
        *,
        user_id: int,
        service_order_id: int,
        artifact_type: str,
        attempt_token: str,
    ) -> str: ...

    async def put_file(
        self,
        source: Path,
        *,
        user_id: int,
        presentation_id: int,
        artifact_type: str,
    ) -> str: ...

    async def put_service_file(
        self,
        source: Path,
        *,
        user_id: int,
        service_order_id: int,
        artifact_type: str,
        attempt_token: str,
    ) -> str: ...

    async def exists(self, key: str) -> bool: ...

    async def download_for_user(self, key: str, target: Path, *, user_id: int) -> Path: ...

    async def delete_for_user(self, key: str, *, user_id: int) -> None: ...


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
        return str(target.relative_to(self._root))

    async def put_service_file(
        self,
        source: Path,
        *,
        user_id: int,
        service_order_id: int,
        artifact_type: str,
        attempt_token: str,
    ) -> str:
        source_path = source.expanduser().resolve(strict=True)
        relative = Path(
            self.service_file_key(
                user_id=user_id,
                service_order_id=service_order_id,
                artifact_type=artifact_type,
                attempt_token=attempt_token,
            )
        )
        target = self._safe_path(str(relative))
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, source_path, target)
        return str(relative)

    def service_file_key(
        self,
        *,
        user_id: int,
        service_order_id: int,
        artifact_type: str,
        attempt_token: str,
    ) -> str:
        if artifact_type not in {"docx", "pdf"}:
            raise ValueError(f"unsupported service artifact type: {artifact_type}")
        if user_id <= 0 or service_order_id <= 0 or not _valid_attempt_token(attempt_token):
            raise ValueError("invalid service artifact coordinates")
        return (
            f"users/{user_id}/service-orders/{service_order_id}/attempts/{attempt_token}/"
            f"referat_{service_order_id}.{artifact_type}"
        )

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

    async def delete_for_user(self, key: str, *, user_id: int) -> None:
        expected_prefix = f"users/{user_id}/"
        if not key.startswith(expected_prefix):
            raise StorageAccessDenied("artifact does not belong to this user")
        path = self._safe_path(key)
        await asyncio.to_thread(path.unlink, missing_ok=True)

    def _safe_path(self, key: str) -> Path:
        if key.startswith("/") or ".." in Path(key).parts:
            raise StorageAccessDenied("unsafe storage key")
        candidate = (self._root / key).resolve()
        if self._root not in candidate.parents:
            raise StorageAccessDenied("storage key escapes root")
        return candidate


def _valid_attempt_token(value: str) -> bool:
    return len(value) == 32 and value.isascii() and value.isalnum()
