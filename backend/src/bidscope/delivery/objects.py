import os
import tempfile
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ObjectStore(Protocol):
    def put_bytes(self, key: str, data: bytes) -> str: ...
    def get_bytes(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...


class LocalObjectStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        if ".." in key or key.startswith("/") or os.path.isabs(key):
            raise ValueError(f"object key escapes store root: {key!r}")
        resolved = (self.root / key).resolve()
        root_resolved = self.root.resolve()
        if root_resolved not in resolved.parents and resolved != root_resolved:
            raise ValueError(f"object key escapes store root: {key!r}")
        return resolved

    def put_bytes(self, key: str, data: bytes) -> str:
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=str(target.parent))
        try:
            os.write(fd, data)
            os.close(fd)
            os.replace(tmp_path, target)
        except BaseException:
            os.close(fd) if not os.get_inheritable(fd) else None
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
        return str(target)

    def get_bytes(self, key: str) -> bytes:
        target = self._resolve(key)
        if not target.exists():
            raise FileNotFoundError(key)
        return target.read_bytes()

    def exists(self, key: str) -> bool:
        try:
            return self._resolve(key).exists()
        except ValueError:
            return False


class S3ObjectStore:
    """S3-compatible object store backed by boto3.

    The bucket and an optional key prefix are supplied at construction so that
    every key passed to the store methods is a logical, bucket-relative path.
    """

    def __init__(self, bucket: str, prefix: str = "", endpoint_url: str | None = None) -> None:
        import boto3  # type: ignore[import-untyped]

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.client: Any = boto3.client("s3", endpoint_url=endpoint_url)

    def _full_key(self, key: str) -> str:
        if ".." in key or key.startswith("/"):
            raise ValueError(f"object key escapes prefix: {key!r}")
        return f"{self.prefix}/{key}" if self.prefix else key

    def put_bytes(self, key: str, data: bytes) -> str:
        full_key = self._full_key(key)
        self.client.put_object(Bucket=self.bucket, Key=full_key, Body=data)
        return f"s3://{self.bucket}/{full_key}"

    def get_bytes(self, key: str) -> bytes:
        full_key = self._full_key(key)
        response = self.client.get_object(Bucket=self.bucket, Key=full_key)
        body = response["Body"]
        return body.read() if hasattr(body, "read") else bytes(body)

    def exists(self, key: str) -> bool:
        full_key = self._full_key(key)
        try:
            self.client.head_object(Bucket=self.bucket, Key=full_key)
            return True
        except self.client.exceptions.NoSuchKey:
            return False
        except self.client.exceptions.ClientError as error:
            if error.response.get("Error", {}).get("Code") == "404":
                return False
            raise
