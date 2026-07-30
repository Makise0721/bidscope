"""Configuration boundary tests for authorized live ingestion."""

from __future__ import annotations

import pytest
from bidscope.config import Settings
from pydantic import ValidationError


def _valid_production_settings() -> dict[str, object]:
    return {
        "_env_file": None,
        "app_mode": "production",
        "admin_token": "a" * 32,
        "object_store_type": "s3",
        "s3_endpoint": "https://s3.example.test",
        "s3_bucket": "bidscope-prod",
        "s3_access_key": "access-key",
        "s3_secret_key": "secret-key",
        "allowed_origins": ["https://bidscope.example.test"],
        "trusted_hosts": ["bidscope.example.test"],
        "external_scheme": "https",
        "database_url": "postgresql+asyncpg://bidscope:password@database.example.test/bidscope",
        "checkpoint_database_url": (
            "postgresql+psycopg://bidscope:password@database.example.test/bidscope"
        ),
    }


def _complete_live_settings() -> dict[str, object]:
    return {
        **_valid_production_settings(),
        "process_role": "ingestion",
        "live_ingestion_enabled": True,
        "ccgp_api_base_url": "https://www.ccgp.gov.cn",
        "ccgp_client_id": "client-id",
        "ccgp_signing_key": "signing-key",
        "ccgp_authorization_ref": "approval-2026-001",
        "ccgp_data_contract_version": "ccgp-authorized-v1",
        "ccgp_data_owner": "authorized-operator",
        "ccgp_data_regions": ["全国"],
        "ccgp_data_categories": ["government-procurement"],
        "ccgp_data_review_status": "approved",
        "ccgp_data_reviewed_at": "2026-07-30T00:00:00Z",
        "ccgp_data_update_sla": "weekly",
        "ccgp_data_retention_days": 365,
    }


def test_live_ingestion_is_disabled_and_credentials_are_absent_by_default() -> None:
    settings = Settings(_env_file=None)

    assert settings.live_ingestion_enabled is False
    assert settings.process_role == "api"
    assert settings.ccgp_api_base_url is None
    assert settings.ccgp_client_id is None
    assert settings.ccgp_signing_key is None


@pytest.mark.parametrize(
    "base_url",
    (
        "http://www.ccgp.gov.cn",
        "https://not-ccgp.example.test",
        "https://www.ccgp.gov.cn?token=secret",
        "https://user:password@www.ccgp.gov.cn",
    ),
)
def test_ccgp_base_url_requires_https_and_an_approved_origin(base_url: str) -> None:
    with pytest.raises(ValidationError, match="ccgp_api_base_url"):
        Settings(_env_file=None, ccgp_api_base_url=base_url)


def test_production_allows_missing_live_credentials_when_ingestion_is_disabled() -> None:
    settings = Settings(**_valid_production_settings())

    assert settings.app_mode == "production"
    assert settings.live_ingestion_enabled is False


def test_enabled_production_ingestion_requires_complete_authorization_contract() -> None:
    values = _valid_production_settings()
    values.update(
        {
            "process_role": "ingestion",
            "live_ingestion_enabled": True,
            "ccgp_api_base_url": "https://www.ccgp.gov.cn",
        }
    )

    with pytest.raises(ValidationError, match="ccgp_client_id"):
        Settings(**values)


def test_api_and_scheduler_reject_ccgp_credentials() -> None:
    for process_role in ("api", "scheduler"):
        with pytest.raises(ValidationError, match="process_role='ingestion'"):
            Settings(_env_file=None, process_role=process_role, ccgp_signing_key="secret-key")


def test_complete_live_ingestion_configuration_is_accepted() -> None:
    settings = Settings(**_complete_live_settings())

    assert settings.live_ingestion_enabled is True
    assert settings.process_role == "ingestion"
    assert settings.ccgp_signing_key is not None
