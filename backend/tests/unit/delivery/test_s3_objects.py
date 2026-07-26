from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
from bidscope.delivery.objects import S3ObjectStore


class _FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeS3Client:
    """Minimal boto3 S3 client stand-in capturing calls and serving canned data.

    The real boto3 client methods (put_object/get_object/head_object) are all we
    depend on, so the stub mimics just those. It also exposes a
    ``ClientError``/``NoSuchKey`` hierarchy that matches boto3's exception shape
    so the store's ``except`` branches run unmodified.
    """

    class _NotFound(Exception):
        pass

    class exceptions:
        class NoSuchKey(Exception):
            pass

        class ClientError(Exception):
            def __init__(self, error_response: dict[str, Any], operation: str) -> None:
                super().__init__(str(error_response))
                self.response = error_response
                self.operation_name = operation

    def __init__(self) -> None:
        self.buckets: dict[str, dict[str, bytes]] = {}
        self.calls: list[dict[str, Any]] = []
        # Buckets that already exist (for head_bucket / ensure_bucket modelling).
        self.known_buckets: set[str] = set()

    def _keys(self, bucket: str) -> dict[str, bytes]:
        return self.buckets.setdefault(bucket, {})

    def put_object(self, **kwargs: Any) -> None:
        self.calls.append({"method": "put_object", "kwargs": kwargs})
        body = kwargs["Body"]
        data = body.read() if hasattr(body, "read") else body
        self._keys(kwargs["Bucket"])[kwargs["Key"]] = data

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"method": "get_object", "kwargs": kwargs})
        bucket = self._keys(kwargs["Bucket"])
        if kwargs["Key"] not in bucket:
            raise self.exceptions.NoSuchKey("missing")
        return {"Body": _FakeBody(bucket[kwargs["Key"]])}

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"method": "head_object", "kwargs": kwargs})
        bucket = self._keys(kwargs["Bucket"])
        if kwargs["Key"] not in bucket:
            raise self.exceptions.NoSuchKey("missing")
        return {}

    def head_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"method": "head_bucket", "kwargs": kwargs})
        if kwargs["Bucket"] not in self.known_buckets:
            # Match the real botocore ClientError shape raised by boto3 when the
            # bucket does not exist (a 404 / NoSuchBucket response).
            raise self.exceptions.ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}},
                "HeadBucket",
            )
        return {}

    def create_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"method": "create_bucket", "kwargs": kwargs})
        self.known_buckets.add(kwargs["Bucket"])
        return {}


def _make_store(client: _FakeS3Client, prefix: str = "") -> S3ObjectStore:
    return S3ObjectStore(bucket="tender-docs", prefix=prefix, client=client)


def test_put_get_round_trip() -> None:
    client = _FakeS3Client()
    store = _make_store(client)
    url = store.put_bytes("ccgp/notice-1.html", b"<html>data</html>")

    assert url == "s3://tender-docs/ccgp/notice-1.html"
    assert store.get_bytes("ccgp/notice-1.html") == b"<html>data</html>"


def test_exists_respects_prefix() -> None:
    client = _FakeS3Client()
    store = _make_store(client, prefix="imports/2026")
    store.put_bytes("notice.bin", b"x")

    assert store.exists("notice.bin") is True
    assert store.exists("other.bin") is False
    # The stored key should carry the prefix.
    assert "imports/2026/notice.bin" in client.buckets["tender-docs"]


def test_get_missing_raises_from_no_such_key() -> None:
    client = _FakeS3Client()
    store = _make_store(client)

    with pytest.raises(client.exceptions.NoSuchKey):
        store.get_bytes("missing.html")


def test_put_rejects_path_traversal() -> None:
    client = _FakeS3Client()
    store = _make_store(client)

    with pytest.raises(ValueError):
        store.put_bytes("../escape.txt", b"bad")


def test_get_rejects_path_traversal() -> None:
    client = _FakeS3Client()
    store = _make_store(client)

    with pytest.raises(ValueError):
        store.get_bytes("../escape.txt")


def test_store_delegates_to_injected_client() -> None:
    """The store must not construct its own boto3 client when one is injected."""
    with mock.patch("boto3.client") as boto_client:
        store = S3ObjectStore(bucket="b", client=_FakeS3Client())
        store.put_bytes("k", b"v")
        boto_client.assert_not_called()


def test_store_builds_client_with_explicit_credentials_when_not_injected() -> None:
    """When no client is injected, boto3.client receives the explicit credentials.

    This guards against silently falling back to ambient (IAM/env) credentials:
    the configured access key, secret key, endpoint, and region must reach boto3.
    """
    with mock.patch("boto3.client") as boto_client:
        boto_client.return_value = _FakeS3Client()
        S3ObjectStore(
            bucket="bidscope",
            endpoint_url="http://minio:9000",
            aws_access_key_id="minio",
            aws_secret_access_key="minioadmin",
        )
        boto_client.assert_called_once()
        _, kwargs = boto_client.call_args
        assert kwargs.get("endpoint_url") == "http://minio:9000"
        assert kwargs.get("aws_access_key_id") == "minio"
        assert kwargs.get("aws_secret_access_key") == "minioadmin"
        assert kwargs.get("region_name") == "us-east-1"


def test_ensure_bucket_creates_missing_bucket_then_succeeds() -> None:
    """ensure_bucket creates a missing bucket and is a no-op once it exists."""
    client = _FakeS3Client()
    store = S3ObjectStore(bucket="bidscope", client=client)

    # First call: bucket is absent, so create_bucket must run.
    store.ensure_bucket()
    head_calls = [c for c in client.calls if c["method"] == "head_bucket"]
    create_calls = [c for c in client.calls if c["method"] == "create_bucket"]
    assert len(head_calls) == 1
    assert len(create_calls) == 1
    assert create_calls[0]["kwargs"]["Bucket"] == "bidscope"

    # Second call: bucket now exists, create_bucket must NOT run again.
    store.ensure_bucket()
    create_calls = [c for c in client.calls if c["method"] == "create_bucket"]
    assert len(create_calls) == 1
