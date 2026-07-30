from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from functools import lru_cache
from typing import Any, ClassVar, Literal, cast
from urllib.parse import parse_qsl, unquote, urlsplit

from pydantic import AnyHttpUrl, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from bidscope.domain.enums import SourceName
from bidscope.domain.provenance import OFFICIAL_HOSTS_BY_SOURCE

MAX_ADMIN_TOKEN_HEADER_LENGTH = 4096


class Settings(BaseSettings):
    _secret_field_names: ClassVar[frozenset[str]] = frozenset(
        {
            "admin_token",
            "model_api_key",
            "s3_access_key",
            "s3_secret_key",
            "backup_s3_access_key",
            "backup_s3_secret_key",
            "ccgp_client_id",
            "ccgp_signing_key",
        }
    )
    _dsn_field_names: ClassVar[frozenset[str]] = frozenset(
        {"database_url", "checkpoint_database_url"}
    )
    _redacted_input_field_names: ClassVar[frozenset[str]] = frozenset(
        {
            *_secret_field_names,
            *_dsn_field_names,
            "ccgp_api_base_url",
        }
    )
    _database_dsn_defaults: ClassVar[dict[str, str]] = {
        "database_url": "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope",
        "checkpoint_database_url": "postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope",
    }
    _production_dsn_schemes: ClassVar[dict[str, str]] = {
        "database_url": "postgresql+asyncpg",
        "checkpoint_database_url": "postgresql+psycopg",
    }
    _production_dsn_tls_queries: ClassVar[dict[str, tuple[str, frozenset[str]]]] = {
        "database_url": ("ssl", frozenset({"require"})),
        "checkpoint_database_url": ("sslmode", frozenset({"require"})),
    }

    @classmethod
    def _build_sanitized_validation_error(
        cls, data: Mapping[str, Any], error: ValidationError
    ) -> ValidationError:
        raw_errors = error.errors(include_url=True, include_context=True)
        secret_values: set[str] = set()
        for field_name in cls._secret_field_names:
            cls._collect_secret_value(data.get(field_name), secret_values)
        for item in raw_errors:
            cls._collect_error_input_values(item.get("input"), secret_values, root=True)
        sanitized_errors = [
            cls._sanitize_error_item(cast(Mapping[str, Any], item), secret_values)
            for item in raw_errors
        ]
        return ValidationError.from_exception_data(cls.__name__, cast(Any, sanitized_errors))

    def __init__(self, **data: Any) -> None:
        sanitized_error: ValidationError | None = None
        try:
            super().__init__(**data)
        except ValidationError as error:
            sanitized_error = self._build_sanitized_validation_error(data, error)
            del data
            del error

        if sanitized_error is not None:
            del self
            raise sanitized_error

    @staticmethod
    def _secret_text(value: Any) -> str | None:
        """Normalize secret input only for transient settings validation/redaction.

        The public getter is never logged, persisted, returned, or included in
        validation errors; runtime secret use remains at its authentication,
        storage, and model-provider boundaries.
        """
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        return value if isinstance(value, str) else None

    @classmethod
    def _collect_secret_value(cls, value: Any, secret_values: set[str]) -> None:
        secret_text = cls._secret_text(value)
        if secret_text:
            secret_values.add(secret_text)

    @classmethod
    def _is_blank(cls, value: Any) -> bool:
        if isinstance(value, SecretStr):
            text = cls._secret_text(value)
            return text is None or not text.strip()
        return value is None or (isinstance(value, str) and not value.strip())

    @classmethod
    def _collect_error_input_values(
        cls, value: Any, secret_values: set[str], *, root: bool = False
    ) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in cls._redacted_input_field_names:
                    cls._collect_secret_value(item, secret_values)
                elif isinstance(item, (Mapping, list, tuple)):
                    cls._collect_error_input_values(item, secret_values)
        elif isinstance(value, (list, tuple)):
            for item in value:
                cls._collect_error_input_values(item, secret_values)
        elif root and isinstance(value, str):
            cls._collect_secret_value(value, secret_values)

    @classmethod
    def _sanitize_error_item(
        cls, item: Mapping[str, Any], secret_values: set[str]
    ) -> dict[str, Any]:
        sanitized = dict(item)
        location = item.get("loc", ())
        if "input" in item:
            if isinstance(location, tuple) and any(
                part in cls._redacted_input_field_names
                for part in location
                if isinstance(part, str)
            ):
                sanitized["input"] = "**********"
            else:
                sanitized["input"] = cls._sanitize_error_value(item["input"], secret_values)
        if "ctx" in item:
            sanitized["ctx"] = cls._sanitize_error_context(item["ctx"], secret_values)
        return sanitized

    @staticmethod
    def _is_secret_like(value: Any) -> bool:
        return isinstance(value, SecretStr) or getattr(value, "get_secret_value", None) is not None

    @classmethod
    def _sanitize_error_context(cls, value: Any, secret_values: set[str]) -> Any:
        if cls._is_secret_like(value):
            return "**********"
        if isinstance(value, Mapping):
            return {
                key: cls._sanitize_error_context(item, secret_values) for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [cls._sanitize_error_context(item, secret_values) for item in value]
        if isinstance(value, BaseException):
            sanitized_args = tuple(
                cls._sanitize_error_context(item, secret_values) for item in value.args
            )
            # Pydantic exposes this object through ``ctx['error']``. Rebuild a
            # plain ValueError so the original validator traceback and locals
            # cannot remain reachable from the sanitized ValidationError.
            return ValueError(*sanitized_args)
        if isinstance(value, str):
            return cls._redact_secret_string(value, secret_values)
        return value

    @staticmethod
    def _redact_dsn_password(value: str) -> str:
        """Mask a PostgreSQL password without changing the connection target."""
        scheme, separator, remainder = value.partition("://")
        if not separator or not scheme.startswith("postgresql"):
            return value

        userinfo, separator, location = remainder.rpartition("@")
        if separator:
            username, password_separator, _ = userinfo.partition(":")
            if not password_separator:
                return value
            return f"{scheme}://{username}:**********@{location}"

        username, password_separator, _ = remainder.partition(":")
        if not password_separator:
            return value
        return f"{scheme}://{username}:**********"

    @staticmethod
    def _redact_secret_string(value: str, secret_values: set[str]) -> str:
        return "**********" if value in secret_values else value

    def database_dsn(self) -> str:
        """Return the primary DSN only at the database driver boundary."""
        return self.database_url.get_secret_value()

    def checkpoint_database_dsn(self) -> str:
        """Return the checkpoint DSN only at the migration/checkpoint boundary."""
        return self.checkpoint_database_url.get_secret_value()

    @classmethod
    def _sanitize_error_value(cls, value: Any, secret_values: set[str]) -> Any:
        if cls._is_secret_like(value):
            return "**********"
        if isinstance(value, Mapping):
            return {
                key: (
                    "**********"
                    if key in cls._redacted_input_field_names
                    and value[key] is not None
                    else cls._sanitize_error_value(value[key], secret_values)
                )
                for key in value
            }
        if isinstance(value, (list, tuple)):
            return [cls._sanitize_error_value(item, secret_values) for item in value]
        if isinstance(value, str):
            return cls._redact_secret_string(value, secret_values)
        return value

    model_config = SettingsConfigDict(
        env_prefix="BIDSCOPE_",
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_mode: Literal["demo", "development", "production", "test"] = "demo"
    #: Selects the process role. Only the isolated ingestion role may hold
    #: CCGP signing credentials or enable live source acquisition.
    process_role: Literal["api", "scheduler", "ingestion"] = "api"
    database_url: SecretStr = SecretStr(_database_dsn_defaults["database_url"])
    checkpoint_database_url: SecretStr = SecretStr(
        _database_dsn_defaults["checkpoint_database_url"]
    )
    real_model_enabled: bool = False
    #: Base64-encoded Ed25519 public key used to verify restricted evaluation
    #: snapshot-admission catalog signatures. The matching private key is never
    #: configured by or stored in this application.
    real_evaluation_catalog_public_key: str | None = None
    admin_token: SecretStr | None = None
    admin_token_min_length: int = Field(default=32, gt=0)
    allowed_origins: list[AnyHttpUrl] = Field(default_factory=list)
    trusted_hosts: list[str] = Field(default_factory=list)
    external_scheme: Literal["http", "https"] = "http"
    #: Values that are unsafe to use as production admin credentials.
    production_placeholder_tokens: ClassVar[frozenset[str]] = frozenset(
        {"change-me", "changeme", "replace-me", "your-admin-token", "minioadmin"}
    )
    #: OpenAI-compatible base URL for the real-model provider (e.g. DeepSeek).
    #: Used by :class:`~bidscope.llm.deepseek.DeepSeekReportModel` and its
    #: siblings — only consulted when ``real_model_enabled`` is true.
    model_base_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-chat"
    model_api_key: SecretStr | None = None
    #: Live acquisition is an explicit opt-in and is disabled in every mode by
    #: default. The API and subscription scheduler remain snapshot-only.
    live_ingestion_enabled: bool = False
    ccgp_api_base_url: AnyHttpUrl | None = None
    ccgp_client_id: SecretStr | None = None
    ccgp_signing_key: SecretStr | None = None
    #: Dotted entry point supplied by the authorized operator. The application
    #: never guesses endpoint fields or a signing algorithm.
    ccgp_runner_factory: str | None = Field(default=None, max_length=256)
    ccgp_authorization_ref: str | None = Field(default=None, max_length=256)
    ccgp_poll_seconds: int = Field(default=900, ge=60)
    ccgp_request_timeout_seconds: int = Field(default=20, gt=0)
    ccgp_max_response_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    ccgp_max_pages_per_run: int = Field(default=100, gt=0, le=100)
    ccgp_min_interval_seconds: int = Field(default=1, gt=0)
    ccgp_data_contract_version: str | None = Field(default=None, max_length=256)
    ccgp_data_owner: str | None = Field(default=None, max_length=256)
    ccgp_data_regions: list[str] = Field(default_factory=list)
    ccgp_data_categories: list[str] = Field(default_factory=list)
    ccgp_data_review_status: Literal["approved", "pending", "rejected"] | None = None
    ccgp_data_reviewed_at: datetime | None = None
    ccgp_data_update_sla: Literal["weekly"] | None = None
    ccgp_data_retention_days: int | None = Field(default=None, gt=0, le=3650)
    #: Root directory for the local object store (DOCX outputs and snapshot
    #: payloads in local/demo deployments).
    object_store_root: str = "data/objects"
    #: Selects the object-store backend. ``local`` writes to ``object_store_root``;
    #: ``s3`` uses an S3-compatible endpoint (MinIO in compose, AWS S3 in prod)
    #: configured via the ``s3_*`` fields below.
    object_store_type: Literal["local", "s3"] = "local"
    #: S3-compatible endpoint URL (e.g. ``http://minio:9000``). Required when
    #: ``object_store_type == "s3"``.
    s3_endpoint: str | None = None
    s3_region: str = "us-east-1"
    #: S3 bucket name. Required when ``object_store_type == "s3"``.
    s3_bucket: str | None = None
    #: Static access key for the S3 backend. Required when
    #: ``object_store_type == "s3"`` so the store never falls back to ambient
    #: (IAM/env) credentials implicitly.
    s3_access_key: SecretStr | None = None
    #: Static secret key paired with ``s3_access_key``.
    s3_secret_key: SecretStr | None = None
    #: Optional logical key prefix applied to every stored object (e.g.
    #: ``imports/2026``). Defaults to no prefix.
    s3_prefix: str = ""
    #: Root directory for versioned local PostgreSQL/object-store backups.
    backup_root: str = "data/backups"
    backup_daily_retention: int = Field(default=7, gt=0)
    backup_weekly_retention: int = Field(default=4, gt=0)
    backup_s3_enabled: bool = False
    backup_s3_endpoint: str | None = None
    backup_s3_bucket: str | None = None
    backup_s3_access_key: SecretStr | None = None
    backup_s3_secret_key: SecretStr | None = None
    backup_s3_prefix: str = "bidscope-backups"
    backup_tool_timeout_seconds: int = Field(default=900, gt=0)
    db_pool_size: int = Field(default=10, gt=0)
    db_max_overflow: int = Field(default=20, gt=0)
    db_pool_recycle_seconds: int = Field(default=1800, gt=0)
    db_connect_timeout_seconds: int = Field(default=5, gt=0)
    db_command_timeout_seconds: int = Field(default=30, gt=0)
    s3_connect_timeout_seconds: int = Field(default=5, gt=0)
    s3_read_timeout_seconds: int = Field(default=60, gt=0)
    s3_max_attempts: int = Field(default=3, gt=0)
    max_concurrent_runs: int = Field(default=4, gt=0)
    max_request_body_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_sse_connections: int = Field(default=100, gt=0)
    max_report_items: int = Field(default=100, gt=0)
    #: Bounds for staging an untrusted snapshot import.  These are enforced
    #: before copying the bundle into temporary storage.
    snapshot_import_max_files: int = Field(default=1_000, gt=0)
    snapshot_import_max_file_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    snapshot_import_max_bundle_bytes: int = Field(default=200 * 1024 * 1024, gt=0)
    graceful_shutdown_seconds: int = Field(default=30, gt=0)
    scheduler_tick_timeout_seconds: int = Field(default=25, gt=0)
    stale_run_after_seconds: int = Field(default=300, gt=0)
    run_heartbeat_seconds: int = Field(default=30, gt=0)
    #: Token required by the ``/api/test-controls/*`` routes. Those routes are
    #: only registered when ``app_mode == "test"``, and this token gates them.
    test_control_token: str | None = None

    @model_validator(mode="after")
    def validate_run_heartbeat_interval(self) -> Settings:
        if self.run_heartbeat_seconds >= self.stale_run_after_seconds:
            raise ValueError("run_heartbeat_seconds must be less than stale_run_after_seconds")
        return self

    @model_validator(mode="after")
    def validate_snapshot_import_limits(self) -> Settings:
        if self.snapshot_import_max_bundle_bytes < self.snapshot_import_max_file_bytes:
            raise ValueError(
                "snapshot_import_max_bundle_bytes must be at least "
                "snapshot_import_max_file_bytes"
            )
        return self

    @model_validator(mode="after")
    def validate_shutdown_timeout(self) -> Settings:
        if self.scheduler_tick_timeout_seconds >= self.graceful_shutdown_seconds:
            raise ValueError(
                "scheduler_tick_timeout_seconds must be less than graceful_shutdown_seconds"
            )
        return self

    @model_validator(mode="after")
    def validate_production_admin_token(self) -> Settings:
        if self.app_mode not in {"development", "production"}:
            return self
        if self.app_mode == "production" and self.process_role == "ingestion":
            return self

        raw_token = self._secret_text(self.admin_token)
        if raw_token is not None:
            try:
                token_bytes = raw_token.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ValueError("admin_token must be valid UTF-8") from error
            if len(token_bytes) > MAX_ADMIN_TOKEN_HEADER_LENGTH:
                raise ValueError("admin_token exceeds the maximum header byte length")
        if raw_token is None or not raw_token.strip():
            if self.app_mode == "production":
                raise ValueError("admin_token must be non-empty in production")
            raise ValueError("admin_token must be non-empty in development")
        token = raw_token.strip()
        if self.app_mode == "production":
            normalized_token = token.casefold()
            if normalized_token in self.production_placeholder_tokens:
                raise ValueError("admin_token must not be a production placeholder")
            if len(token) < self.admin_token_min_length:
                raise ValueError("admin_token must meet admin_token_min_length in production")
        return self

    @model_validator(mode="after")
    def validate_real_model_credentials(self) -> Settings:
        if not self.real_model_enabled:
            return self

        model_api_key = self._secret_text(self.model_api_key)
        if model_api_key is None or not model_api_key.strip():
            raise ValueError("model_api_key must be non-empty when real_model_enabled is true")
        return self

    @model_validator(mode="after")
    def validate_ccgp_configuration(self) -> Settings:
        """Keep live CCGP access HTTPS-only, allowlisted and role-isolated."""
        if self.ccgp_api_base_url is not None:
            url = self.ccgp_api_base_url
            official_hosts = OFFICIAL_HOSTS_BY_SOURCE[SourceName.CCGP]
            if (
                url.scheme != "https"
                or url.host is None
                or url.host.casefold() not in official_hosts
                or url.port not in {None, 443}
                or url.username is not None
                or url.password is not None
                or url.path not in {"", "/"}
                or url.query is not None
                or url.fragment is not None
            ):
                raise ValueError(
                    "ccgp_api_base_url must be an HTTPS origin on an approved CCGP host"
                )

        has_signing_credential = any(
            not self._is_blank(value) for value in (self.ccgp_client_id, self.ccgp_signing_key)
        )
        if self.process_role != "ingestion" and has_signing_credential:
            raise ValueError("CCGP credentials require process_role='ingestion'")
        if self.live_ingestion_enabled and self.process_role != "ingestion":
            raise ValueError("live_ingestion_enabled requires process_role='ingestion'")
        if not self.live_ingestion_enabled:
            return self

        required_values = {
            "ccgp_api_base_url": self.ccgp_api_base_url,
            "ccgp_client_id": self.ccgp_client_id,
            "ccgp_signing_key": self.ccgp_signing_key,
            "ccgp_authorization_ref": self.ccgp_authorization_ref,
            "ccgp_data_contract_version": self.ccgp_data_contract_version,
            "ccgp_data_owner": self.ccgp_data_owner,
            "ccgp_data_review_status": self.ccgp_data_review_status,
            "ccgp_data_reviewed_at": self.ccgp_data_reviewed_at,
            "ccgp_data_update_sla": self.ccgp_data_update_sla,
            "ccgp_data_retention_days": self.ccgp_data_retention_days,
            "ccgp_runner_factory": self.ccgp_runner_factory,
        }
        missing = [name for name, value in required_values.items() if self._is_blank(value)]
        if not self.ccgp_data_regions:
            missing.append("ccgp_data_regions")
        if not self.ccgp_data_categories:
            missing.append("ccgp_data_categories")
        if missing:
            raise ValueError(
                "live_ingestion_enabled requires non-empty values for: " + ", ".join(missing)
            )
        if self.ccgp_runner_factory is None or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*", self.ccgp_runner_factory
        ):
            raise ValueError("ccgp_runner_factory must be a bounded module:attribute reference")
        for field_name, values in (
            ("ccgp_data_regions", self.ccgp_data_regions),
            ("ccgp_data_categories", self.ccgp_data_categories),
        ):
            if any(not self._is_bounded_label(value) for value in values):
                raise ValueError(f"{field_name} contains an invalid bounded label")
        for field_name, value in (
            ("ccgp_authorization_ref", self.ccgp_authorization_ref),
            ("ccgp_data_contract_version", self.ccgp_data_contract_version),
            ("ccgp_data_owner", self.ccgp_data_owner),
        ):
            if value is None or not self._is_bounded_label(value):
                raise ValueError(f"{field_name} must be a non-empty bounded label")
        if self.ccgp_data_review_status != "approved":
            raise ValueError("ccgp_data_review_status must be approved")
        if self.ccgp_data_reviewed_at is None or self.ccgp_data_reviewed_at.tzinfo is None:
            raise ValueError("ccgp_data_reviewed_at must be timezone-aware")
        if self.ccgp_data_update_sla != "weekly":
            raise ValueError("ccgp_data_update_sla must be weekly")
        return self

    @staticmethod
    def _is_bounded_label(value: str, *, max_length: int = 256) -> bool:
        return bool(
            value.strip()
            and len(value) <= max_length
            and not any(ord(character) <= 0x1F or ord(character) == 0x7F for character in value)
        )

    @model_validator(mode="before")
    @classmethod
    def validate_backup_s3_requirements(cls, data: Any) -> Any:
        if not isinstance(data, Mapping) or not data.get("backup_s3_enabled", False):
            return data
        missing = [
            name
            for name, value in (
                ("backup_s3_endpoint", data.get("backup_s3_endpoint")),
                ("backup_s3_bucket", data.get("backup_s3_bucket")),
                ("backup_s3_access_key", data.get("backup_s3_access_key")),
                ("backup_s3_secret_key", data.get("backup_s3_secret_key")),
            )
            if cls._is_blank(value)
        ]
        if missing:
            raise ValueError(
                "backup_s3_enabled requires non-empty values for: " + ", ".join(missing)
            )
        return data

    @model_validator(mode="before")
    @classmethod
    def validate_s3_storage_requirements(cls, data: Any) -> Any:
        """When S3 storage is selected, all S3 connection fields must be present.

        A misconfigured S3 deployment would otherwise fall back to ambient
        credentials or fail at first use with an opaque error; failing fast at
        settings construction surfaces the missing variables by name.
        """
        if not isinstance(data, Mapping) or data.get("object_store_type", "local") != "s3":
            return data
        missing = [
            name
            for name, value in (
                ("s3_endpoint", data.get("s3_endpoint")),
                ("s3_region", data.get("s3_region", "us-east-1")),
                ("s3_bucket", data.get("s3_bucket")),
                ("s3_access_key", data.get("s3_access_key")),
                ("s3_secret_key", data.get("s3_secret_key")),
            )
            if cls._is_blank(value)
        ]
        if missing:
            raise ValueError(
                "object_store_type='s3' requires non-empty values for: " + ", ".join(missing)
            )
        return data

    @model_validator(mode="after")
    def validate_production_configuration(self) -> Settings:
        if self.app_mode != "production":
            return self

        invalid_fields: list[str] = []
        dsn_fields = (
            frozenset({"database_url"})
            if self.process_role == "ingestion"
            else self._dsn_field_names
        )
        for field_name in dsn_fields:
            value = self._secret_text(getattr(self, field_name))
            if not self._is_valid_production_dsn(field_name, value):
                invalid_fields.append(field_name)
        if self.object_store_type != "s3":
            invalid_fields.append("object_store_type")
        if self.process_role != "ingestion":
            if not self.allowed_origins or any(
                "*" in str(origin) or not self._is_exact_production_origin(origin)
                for origin in self.allowed_origins
            ):
                invalid_fields.append("allowed_origins")
            if not self.trusted_hosts or any(
                not host.strip() or "*" in host for host in self.trusted_hosts
            ):
                invalid_fields.append("trusted_hosts")
            if self.external_scheme != "https":
                invalid_fields.append("external_scheme")
        if invalid_fields:
            raise ValueError("production requires valid values for: " + ", ".join(invalid_fields))
        return self

    @staticmethod
    def _is_exact_production_origin(origin: AnyHttpUrl) -> bool:
        return (
            origin.username is None
            and origin.password is None
            and origin.path == "/"
            and origin.query is None
            and origin.fragment is None
        )

    @staticmethod
    def _has_valid_percent_encoding(value: str) -> bool:
        index = 0
        while index < len(value):
            if value[index] == "%":
                if index + 2 >= len(value) or any(
                    character not in "0123456789abcdefABCDEF"
                    for character in value[index + 1 : index + 3]
                ):
                    return False
                index += 3
                continue
            index += 1
        return True

    @classmethod
    def _is_valid_production_dsn(cls, field_name: str, value: str | None) -> bool:
        """Accept one explicit driver target and its minimal TLS configuration."""
        if not value or value == cls._database_dsn_defaults[field_name]:
            return False
        if any(ord(character) <= 0x1F or ord(character) == 0x7F for character in value):
            return False
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme != cls._production_dsn_schemes[field_name]
                or not value.startswith(f"{parsed.scheme}://")
                or not parsed.netloc
                or value.endswith("?")
                or parsed.fragment
                or parsed.hostname is None
                or not parsed.hostname.strip()
                or not parsed.path.startswith("/")
                or parsed.path.count("/") != 1
                or not parsed.path[1:].strip()
                or ";" in parsed.path
            ):
                return False

            userinfo, separator, authority = parsed.netloc.rpartition("@")
            if (
                not separator
                or "@" in userinfo
                or authority.endswith(":")
                or not cls._has_valid_percent_encoding(userinfo)
                or parsed.username is None
                or parsed.password is None
                or not unquote(parsed.username).strip()
                or not unquote(parsed.password).strip()
            ):
                return False

            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                return False

            if parsed.query:
                query_key, valid_values = cls._production_dsn_tls_queries[field_name]
                if not cls._has_valid_percent_encoding(parsed.query):
                    return False
                query_items = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
                if len(query_items) != 1 or query_items[0][0] != query_key:
                    return False
                if query_items[0][1] not in valid_values:
                    return False
        except ValueError:
            return False
        return True


@lru_cache
def get_settings() -> Settings:
    return Settings()
