"""S3-compatible production artifact storage."""

import asyncio
from pathlib import Path
from uuid import uuid4

import boto3
from botocore.client import BaseClient
from botocore.exceptions import ClientError

from modules.files.storage import StorageAccessDenied, _valid_attempt_token


class S3Storage:
    _extensions = {"pptx": ".pptx", "pdf": ".pdf", "preview": ".png"}

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None,
        access_key: str,
        secret_key: str,
        region: str,
        client: BaseClient | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("S3 bucket is required")
        self._bucket = bucket
        self._client = client or boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )

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
        path = source.expanduser().resolve(strict=True)
        key = (
            f"users/{user_id}/presentations/{presentation_id}/"
            f"{uuid4().hex}{self._extensions[artifact_type]}"
        )
        await asyncio.to_thread(
            self._client.upload_file,
            str(path),
            self._bucket,
            key,
            ExtraArgs={
                "ContentType": {
                    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    "pdf": "application/pdf",
                    "preview": "image/png",
                }[artifact_type],
                "ServerSideEncryption": "AES256",
            },
        )
        return key

    async def put_service_file(
        self,
        source: Path,
        *,
        user_id: int,
        service_order_id: int,
        artifact_type: str,
        attempt_token: str,
    ) -> str:
        path = source.expanduser().resolve(strict=True)
        key = self.service_file_key(
            user_id=user_id,
            service_order_id=service_order_id,
            artifact_type=artifact_type,
            attempt_token=attempt_token,
        )
        await asyncio.to_thread(
            self._client.upload_file,
            str(path),
            self._bucket,
            key,
            ExtraArgs={
                "ContentType": {
                    "docx": (
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    ),
                    "pdf": "application/pdf",
                }[artifact_type],
                "ServerSideEncryption": "AES256",
            },
        )
        return key

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
        self._validate_key(key)
        try:
            await asyncio.to_thread(self._client.head_object, Bucket=self._bucket, Key=key)
            return True
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 404:
                return False
            raise

    async def signed_url(self, key: str, *, user_id: int, expires_seconds: int = 300) -> str:
        self._validate_owner(key, user_id)
        return await asyncio.to_thread(
            self._client.generate_presigned_url,
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=min(max(expires_seconds, 30), 3600),
        )

    async def download_for_user(self, key: str, target: Path, *, user_id: int) -> Path:
        self._validate_owner(key, user_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            self._client.download_file,
            self._bucket,
            key,
            str(target),
        )
        return target

    async def delete_for_user(self, key: str, *, user_id: int) -> None:
        self._validate_owner(key, user_id)
        await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=key)

    @staticmethod
    def _validate_key(key: str) -> None:
        if key.startswith("/") or ".." in Path(key).parts:
            raise StorageAccessDenied("unsafe storage key")

    def _validate_owner(self, key: str, user_id: int) -> None:
        self._validate_key(key)
        if not key.startswith(f"users/{user_id}/"):
            raise StorageAccessDenied("artifact does not belong to this user")
