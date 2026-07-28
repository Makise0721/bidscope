import os
import tempfile
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ObjectStore(Protocol):
    def put_bytes(self, key: str, data: bytes) -> str: ...
    def get_bytes(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def list_keys(self, prefix: str = "") -> list[str]: ...
    def delete(self, key: str) -> None: ...


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
        fd_open = True
        try:
            os.write(fd, data)
            os.close(fd)
            fd_open = False
            os.replace(tmp_path, target)
        except BaseException:
            # Preserve the original exception: never let cleanup errors mask it.
            try:
                if fd_open:
                    os.close(fd)
            except OSError:
                pass
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
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

    def list_keys(self, prefix: str = "") -> list[str]:
        self._resolve(prefix or ".")
        base = self.root.resolve()
        keys = [
            path.relative_to(base).as_posix()
            for path in self.root.rglob("*")
            if path.is_file() and path.relative_to(base).as_posix().startswith(prefix)
        ]
        return sorted(keys)

    def delete(self, key: str) -> None:
        target = self._resolve(key)
        if target.exists():
            if not target.is_file():
                raise ValueError(f"object key is not a file: {key!r}")
            target.unlink()


class S3ObjectStore:
    """S3-compatible object store backed by boto3.

    The bucket and an optional key prefix are supplied at construction so that
    every key passed to the store methods is a logical, bucket-relative path.
    When ``client`` is omitted, an explicit boto3 client is built from the
    supplied credentials/endpoint — the store never silently relies on ambient
    (IAM/env) credentials. Inject ``client`` for tests or when you already hold
    a configured client.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        endpoint_url: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        region_name: str = "us-east-1",
        connect_timeout: int = 5,
        read_timeout: int = 60,
        max_attempts: int = 3,
        client: Any = None,
    ) -> None:
        if client is not None:
            self.client: Any = client
        else:
            import boto3  # type: ignore[import-untyped]
            from botocore.config import Config  # type: ignore[import-untyped]

            self.client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_secret_access_key,
                region_name=region_name,
                config=Config(
                    connect_timeout=connect_timeout,
                    read_timeout=read_timeout,
                    retries={"mode": "standard", "max_attempts": max_attempts},
                ),
            )
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def ensure_bucket(self) -> None:
        """Create the configured bucket if it does not already exist.

        Intentionally not part of the ``ObjectStore`` Protocol: only the S3
        backend needs bucket bootstrap, and local stores have nothing
        analogous. Callers that want startup-time bucket creation should guard
        on ``isinstance(store, S3ObjectStore)`` so ``LocalObjectStore`` stays a
        pure no-op-free implementation.

        Mirrors :meth:`exists`: the client's own ``exceptions.ClientError`` is
        caught from ``self.client.exceptions`` so this works against both real
        boto3 clients (where it aliases ``botocore.exceptions.ClientError``)
        and test doubles that stub the same namespace. Error codes are read
        from the structured response; ``BucketAlreadyOwnedByYou`` (raised when
        a bucket we already own was concurrently created) is treated as
        success. Per-service errors like that are only present on the
        instantiated client's ``exceptions`` namespace, not as importable
        module-level classes.
        """
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except self.client.exceptions.ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            # boto3 surfaces a missing bucket on head_bucket as a 404 or a
            # NoSuchBucket code depending on the backend (MinIO vs. AWS S3).
            if code not in ("404", "NoSuchBucket"):
                raise
            try:
                self.client.create_bucket(Bucket=self.bucket)
            except self.client.exceptions.ClientError as create_error:
                if create_error.response.get("Error", {}).get("Code") != "BucketAlreadyOwnedByYou":
                    raise

    def _full_key(self, key: str) -> str:
        if ".." in key or key.startswith("/"):
            raise ValueError(f"object key escapes prefix: {key!r}")
        return f"{self.prefix}/{key}" if self.prefix else key

    def _full_prefix(self, prefix: str) -> str:
        if ".." in prefix or prefix.startswith("/"):
            raise ValueError(f"object prefix escapes prefix: {prefix!r}")
        logical_prefix = prefix.strip("/")
        if not logical_prefix:
            return f"{self.prefix}/" if self.prefix else ""
        base = f"{self.prefix}/" if self.prefix else ""
        return f"{base}{logical_prefix}"

    def list_keys(self, prefix: str = "") -> list[str]:
        full_prefix = self._full_prefix(prefix)
        logical_base = f"{self.prefix}/" if self.prefix else ""
        keys: list[str] = []
        continuation_token: str | None = None
        while True:
            params: dict[str, str] = {
                "Bucket": self.bucket,
                "Prefix": full_prefix,
            }
            if continuation_token is not None:
                params["ContinuationToken"] = continuation_token
            response = self.client.list_objects_v2(**params)
            for item in response.get("Contents", ()):
                full_key = item.get("Key")
                if not isinstance(full_key, str) or not full_key.startswith(logical_base):
                    continue
                keys.append(full_key[len(logical_base) :])
            if not response.get("IsTruncated"):
                break
            continuation_token = response.get("NextContinuationToken")
            if not isinstance(continuation_token, str) or not continuation_token:
                break
        return sorted(keys)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._full_key(key))

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
