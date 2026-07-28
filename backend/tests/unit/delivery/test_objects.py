import os
from pathlib import Path
from typing import Any

import pytest
from bidscope.api import dependencies
from bidscope.api.dependencies import create_object_store
from bidscope.config import Settings
from bidscope.delivery.objects import LocalObjectStore, S3ObjectStore


def _make_store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(root=tmp_path / "objects")


def test_put_bytes_writes_exact_content(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    key = store.put_bytes("notice-1/payload.html", b"<html>hi</html>")

    assert store.exists("notice-1/payload.html")
    assert Path(key).read_bytes() == b"<html>hi</html>"


def test_get_bytes_round_trips(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.put_bytes("a/b.bin", bytes(range(256)))

    assert store.get_bytes("a/b.bin") == bytes(range(256))


def test_get_bytes_missing_raises(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    with pytest.raises(FileNotFoundError):
        store.get_bytes("does/not/exist.bin")


def test_exists_returns_false_for_missing(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    assert store.exists("nope") is False


def test_put_bytes_is_atomic_no_partial_file(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    data = b"x" * 10_000
    key = store.put_bytes("atomic.bin", data)

    assert Path(key).read_bytes() == data
    # No leftover temp files in the object root.
    staging = [p for p in (tmp_path / "objects").rglob("*") if p.is_file()]
    assert staging == [Path(key)]


def test_put_bytes_overwrites_existing(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.put_bytes("k.txt", b"v1")
    store.put_bytes("k.txt", b"v2")

    assert store.get_bytes("k.txt") == b"v2"


def test_put_bytes_rejects_path_traversal(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    with pytest.raises(ValueError):
        store.put_bytes("../escape.txt", b"bad")


def test_put_bytes_preserves_original_exception_on_failure(tmp_path: Path) -> None:
    """When the write fails, the original exception must surface unmasked."""
    store = _make_store(tmp_path)
    data = b"x" * 10_000

    # Force os.write to fail once the fd is open; the raised exception must be
    # the original error, not an OSError from a double fd close.
    original_write = os.write

    def failing_write(fd: int, payload: bytes) -> int:
        raise RuntimeError("write failed")

    os.write = failing_write
    try:
        with pytest.raises(RuntimeError, match="write failed"):
            store.put_bytes("boom.bin", data)
    finally:
        os.write = original_write

    # No leftover temp files should remain after a failed write.
    staging = list((tmp_path / "objects").rglob(".tmp-*"))
    assert staging == [], f"leftover temp files: {staging}"


def test_put_bytes_leaves_no_tmp_files_when_replace_fails(tmp_path: Path) -> None:
    """A failing os.replace must not leak the staging temp file."""
    store = _make_store(tmp_path)
    data = b"x" * 100

    original_replace = os.replace

    def failing_replace(*args: object, **kwargs: object) -> None:
        raise RuntimeError("replace failed")

    os.replace = failing_replace
    try:
        with pytest.raises(RuntimeError, match="replace failed"):
            store.put_bytes("atomic.bin", data)
    finally:
        os.replace = original_replace

    staging = list((tmp_path / "objects").rglob(".tmp-*"))
    assert staging == [], f"leftover temp files: {staging}"


def test_storage_factory_selects_s3_with_explicit_configuration() -> None:
    settings = Settings(
        object_store_type="s3",
        s3_endpoint="http://minio:9000",
        s3_bucket="bidscope",
        s3_access_key="minio",
        s3_secret_key="minioadmin",
    )
    assert isinstance(create_object_store(settings), S3ObjectStore)


def test_storage_factory_passes_configured_s3_region(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class CapturingS3ObjectStore(S3ObjectStore):
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(dependencies, "S3ObjectStore", CapturingS3ObjectStore)
    settings = Settings(
        object_store_type="s3",
        s3_endpoint="https://s3.example.test",
        s3_bucket="bidscope",
        s3_access_key="access",
        s3_secret_key="secret",
        s3_region="eu-west-2",
    )

    create_object_store(settings)

    assert captured["region_name"] == "eu-west-2"
    assert captured["connect_timeout"] == settings.s3_connect_timeout_seconds
    assert captured["read_timeout"] == settings.s3_read_timeout_seconds
    assert captured["max_attempts"] == settings.s3_max_attempts


def test_storage_factory_uses_local_root_in_demo(tmp_path: Path) -> None:
    assert isinstance(
        create_object_store(Settings(object_store_root=str(tmp_path))), LocalObjectStore
    )
