"""Tests for deterministic authorized-response snapshot materialization."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from bidscope.domain.snapshots import AuthorizedSourceContract
from bidscope.ingestion.materializer import (
    AuthorizedBundleMaterializer,
    BundleQuarantineError,
    MaterializedBundle,
)
from bidscope.ingestion.ports import AuthorizedSourcePage
from bidscope.snapshots.adapters import inspect_bundle
from bidscope.snapshots.ccgp import CcgpSnapshotAdapter

FIXED_NOW = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)
RESPONSE = b'{"items":[{"notice_id":"n-1"}],"next_cursor":null}'


def _contract() -> AuthorizedSourceContract:
    return AuthorizedSourceContract.model_validate(
        {
            "contract_version": "ccgp-authorized-v1",
            "authorization_ref": "pilot-ccgp-20260730",
            "data_owner": "authorized-operator",
            "regions": ["national"],
            "categories": ["government-procurement"],
            "review_status": "approved",
            "reviewed_at": "2026-07-30T00:00:00+00:00",
            "update_sla": "weekly",
            "retention_days": 365,
        }
    )


def _page(response_bytes: bytes = RESPONSE) -> AuthorizedSourcePage:
    return AuthorizedSourcePage(
        cursor_before="cursor-1",
        next_cursor="cursor-2",
        items=({"notice_id": "n-1"},),
        response_bytes=response_bytes,
        response_sha256=sha256(response_bytes).hexdigest(),
        retrieved_at=FIXED_NOW,
        status_code=200,
        source_url="https://www.ccgp.gov.cn/authorized/v1/notices",
        notice_field_map={"external_id": "notice_id"},
    )


def _materialize(
    root: Path,
    page: AuthorizedSourcePage | None = None,
    **kwargs: object,
) -> MaterializedBundle:
    return AuthorizedBundleMaterializer(root).materialize(
        page or _page(),
        batch_id="ccgp-batch-20260730",
        data_contract=_contract(),
        **kwargs,
    )


def test_identical_response_and_metadata_are_byte_identical(tmp_path: Path) -> None:
    first = _materialize(tmp_path / "first")
    second = _materialize(tmp_path / "second")

    assert first.bundle_id == second.bundle_id
    assert first.manifest.model_dump(mode="json") == second.manifest.model_dump(mode="json")
    assert (first.path / "manifest.json").read_bytes() == (
        second.path / "manifest.json"
    ).read_bytes()
    assert (first.path / "response.json").read_bytes() == (
        second.path / "response.json"
    ).read_bytes()
    inspection = inspect_bundle(first.path)
    assert inspection.valid is True
    assert inspection.manifest is not None
    assert inspection.manifest.capture_kind.value == "raw_response"
    assert inspection.manifest.acquisition_metadata is not None
    assert inspection.manifest.acquisition_metadata.notice_field_map == {
        "external_id": "notice_id"
    }
    notices = CcgpSnapshotAdapter().parse(first.path)
    assert [notice.external_id for notice in notices] == ["n-1"]


def test_changed_response_creates_a_new_bundle_id(tmp_path: Path) -> None:
    first = _materialize(tmp_path / "first")
    changed = _materialize(tmp_path / "second", _page(b'{"items":[],"next_cursor":null}'))

    assert first.bundle_id != changed.bundle_id
    assert first.response_sha256 != changed.response_sha256


def test_changed_safe_metadata_creates_a_new_bundle_id(tmp_path: Path) -> None:
    first = _materialize(tmp_path / "first", extra_metadata={"operator_note": "one"})
    changed = _materialize(tmp_path / "second", extra_metadata={"operator_note": "two"})

    assert first.bundle_id != changed.bundle_id
    assert first.manifest.acquisition_metadata is not None
    assert changed.manifest.acquisition_metadata is not None
    assert first.manifest.acquisition_metadata.extra != changed.manifest.acquisition_metadata.extra


def test_hash_case_is_normalized_for_deterministic_replay(tmp_path: Path) -> None:
    first = _materialize(tmp_path / "first")
    uppercase_page = replace(_page(), response_sha256=first.response_sha256.upper())
    uppercase = AuthorizedBundleMaterializer(tmp_path / "second").materialize(
        uppercase_page, batch_id="ccgp-batch-20260730", data_contract=_contract()
    )

    assert first.bundle_id == uppercase.bundle_id
    assert (first.path / "manifest.json").read_bytes() == (
        uppercase.path / "manifest.json"
    ).read_bytes()


def test_whole_materialized_bundle_is_bounded(tmp_path: Path) -> None:
    materializer = AuthorizedBundleMaterializer(tmp_path, max_bundle_bytes=len(RESPONSE) + 1)

    with pytest.raises(BundleQuarantineError) as error:
        materializer.materialize(
            _page(), batch_id="ccgp-batch-20260730", data_contract=_contract()
        )

    assert error.value.code == "bundle_too_large"


def test_existing_bundle_is_checked_against_new_size_limit(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _materialize(root)
    materializer = AuthorizedBundleMaterializer(root, max_bundle_bytes=len(RESPONSE) + 1)

    with pytest.raises(BundleQuarantineError) as error:
        materializer.materialize(
            _page(), batch_id="ccgp-batch-20260730", data_contract=_contract()
        )

    assert error.value.code == "bundle_too_large"


@pytest.mark.parametrize(
    ("page", "contract", "extra_metadata", "code"),
    [
        (_page(), None, {}, "missing_data_contract"),
        (
            replace(_page(), source_url="https://not-ccgp.example.test/authorized"),
            _contract(),
            {},
            "invalid_source_url",
        ),
        (
            replace(_page(), source_url="https://www.ccgp.gov.cn/authorized?token=secret"),
            _contract(),
            {},
            "invalid_source_url",
        ),
        (
            replace(_page(), source_url="https://[abc"),
            _contract(),
            {},
            "invalid_source_url",
        ),
        (replace(_page(), response_sha256=""), _contract(), {}, "missing_response_hash"),
        (_page(), _contract(), {"client_secret": "never-persist"}, "credential_metadata"),
        (_page(), _contract(), {"x-api-key": "never-persist"}, "credential_metadata"),
        (_page(), _contract(), {"operator_note": "Basic never-persist"}, "credential_metadata"),
        (
            _page(),
            _contract(),
            {"request": {"headers": {"Authorization": "Bearer never-persist"}}},
            "credential_metadata",
        ),
        (_page(), _contract(), {"operator_note": "x" * 257}, "invalid_metadata"),
    ],
)
def test_unsafe_authorized_batch_is_quarantined(
    tmp_path: Path,
    page: AuthorizedSourcePage,
    contract: AuthorizedSourceContract | None,
    extra_metadata: dict[str, object],
    code: str,
) -> None:
    materializer = AuthorizedBundleMaterializer(tmp_path)

    with pytest.raises(BundleQuarantineError) as error:
        materializer.materialize(
            page,
            batch_id="ccgp-batch-20260730",
            data_contract=contract,
            extra_metadata=extra_metadata,
        )

    assert error.value.code == code
    assert "never-persist" not in str(error.value)
