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
