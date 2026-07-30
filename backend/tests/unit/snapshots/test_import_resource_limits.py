"""Resource-admission tests for untrusted snapshot bundles."""

from __future__ import annotations

from pathlib import Path

import pytest
from bidscope.snapshots.importer import (
    SnapshotImporter,
    SnapshotImportError,
    SnapshotImportLimits,
)


class _RecordingObjectStore:
    def __init__(self) -> None:
        self.writes: list[tuple[str, bytes]] = []

    def put_bytes(self, key: str, value: bytes) -> None:
        self.writes.append((key, value))


@pytest.mark.parametrize(
    ("files", "limits", "expected_limit"),
    [
        (
            [b"a", b"b"],
            SnapshotImportLimits(
                max_files=1, max_file_bytes=10, max_bundle_bytes=10
            ),
            "max_files",
        ),
        (
            [b"012"],
            SnapshotImportLimits(
                max_files=2, max_file_bytes=2, max_bundle_bytes=10
            ),
            "max_file_bytes",
        ),
        (
            [b"012", b"345"],
            SnapshotImportLimits(
                max_files=2, max_file_bytes=5, max_bundle_bytes=5
            ),
            "max_bundle_bytes",
        ),
    ],
)
async def test_import_rejects_oversized_bundle_before_staging_or_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    files: list[bytes],
    limits: SnapshotImportLimits,
    expected_limit: str,
) -> None:
    bundle = tmp_path / "untrusted-bundle"
    bundle.mkdir()
    for index, contents in enumerate(files):
        (bundle / f"payload-{index}.json").write_bytes(contents)

    object_store = _RecordingObjectStore()

    def repository_factory(_: object) -> object:
        raise AssertionError("resource admission must happen before database access")

    importer = SnapshotImporter(
        session_factory=object,
        repository_factory=repository_factory,
        object_store=object_store,
        import_limits=limits,
    )

    def copytree_must_not_run(*_: object, **__: object) -> None:
        raise AssertionError("resource admission must happen before staging")

    monkeypatch.setattr(
        "bidscope.snapshots.importer.shutil.copytree", copytree_must_not_run
    )

    with pytest.raises(SnapshotImportError) as raised:
        await importer.import_bundle(bundle)

    assert raised.value.errors == [
        {
            "code": "bundle_resource_limit_exceeded",
            "message": f"{expected_limit} exceeded",
            "path": None,
        }
    ]
    assert object_store.writes == []
