from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import Any, ClassVar, Literal, cast

from pydantic import AnyHttpUrl, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    _secret_field_names: ClassVar[frozenset[str]] = frozenset(
        {"admin_token", "model_api_key", "s3_access_key", "s3_secret_key"}
    )

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as error:
            raw_errors = error.errors(include_url=True, include_context=True)
            secret_values: set[str] = set()
            for field_name in self._secret_field_names:
                self._collect_secret_value(data.get(field_name), secret_values)
            for item in raw_errors:
                self._collect_error_input_values(item.get("input"), secret_values)
            sanitized_errors = [
                self._sanitize_error_item(cast(Mapping[str, Any], item), secret_values)
                for item in raw_errors
            ]
            raise ValidationError.from_exception_data(
                self.__class__.__name__, cast(Any, sanitized_errors)
            ) from error

    @classmethod
    def _collect_secret_value(cls, value: Any, secret_values: set[str]) -> None:
        if value is None:
            return
        raw_value = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        if raw_value:
            secret_values.add(raw_value)

    @classmethod
    def _collect_error_input_values(cls, value: Any, secret_values: set[str]) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in cls._secret_field_names:
                    cls._collect_secret_value(item, secret_values)
                else:
                    cls._collect_error_input_values(item, secret_values)
        elif isinstance(value, (list, tuple)):
            for item in value:
                cls._collect_error_input_values(item, secret_values)
        elif isinstance(value, str):
            secret_values.add(value)

    @classmethod
    def _sanitize_error_item(
        cls, item: Mapping[str, Any], secret_values: set[str]
    ) -> dict[str, Any]:
        return {
            key: cls._sanitize_error_value(value, secret_values)
            for key, value in item.items()
        }

    @classmethod
    def _sanitize_error_value(cls, value: Any, secret_values: set[str]) -> Any:
        if isinstance(value, SecretStr):
            return value
        if isinstance(value, dict):
            return {
                key: (
                    SecretStr(str(value[key]))
                    if key in cls._secret_field_names and value[key] is not None
                    else cls._sanitize_error_value(value[key], secret_values)
                )
                for key in value
            }
        if isinstance(value, (list, tuple)):
            return [cls._sanitize_error_value(item, secret_values) for item in value]
        if isinstance(value, str):
            sanitized = value
            for secret in secret_values:
                sanitized = sanitized.replace(secret, "**********")
            return sanitized
        return value

    model_config = SettingsConfigDict(
        env_prefix="BIDSCOPE_",
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_mode: Literal["demo", "development", "production", "test"] = "demo"
    database_url: str = "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope"
    checkpoint_database_url: str = "postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope"
    real_model_enabled: bool = False
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
    def validate_production_admin_token(self) -> Settings:
        if self.app_mode != "production":
            return self

        token = (
            self.admin_token.get_secret_value().strip()
            if self.admin_token is not None
            else ""
        )
        normalized_token = token.casefold()
        if not token:
            raise ValueError("admin_token must be non-empty in production")
        if normalized_token in self.production_placeholder_tokens:
            raise ValueError("admin_token must not be a production placeholder")
        if len(token) < self.admin_token_min_length:
            raise ValueError("admin_token must meet admin_token_min_length in production")
        return self

    @model_validator(mode="after")
    def validate_s3_storage_requirements(self) -> Settings:
        """When S3 storage is selected, all S3 connection fields must be present.

        A misconfigured S3 deployment would otherwise fall back to ambient
        credentials or fail at first use with an opaque error; failing fast at
        settings construction surfaces the missing variables by name.
        """
        if self.object_store_type != "s3":
            return self
        missing = [
            name
            for name, value in (
                ("s3_endpoint", self.s3_endpoint),
                ("s3_region", self.s3_region),
                ("s3_bucket", self.s3_bucket),
                (
                    "s3_access_key",
                    self.s3_access_key.get_secret_value()
                    if self.s3_access_key is not None
                    else None,
                ),
                (
                    "s3_secret_key",
                    self.s3_secret_key.get_secret_value()
                    if self.s3_secret_key is not None
                    else None,
                ),
            )
            if not value or not value.strip()
        ]
        if missing:
            raise ValueError(
                "object_store_type='s3' requires non-empty values for: " + ", ".join(missing)
            )
        return self

    @model_validator(mode="after")
    def validate_production_configuration(self) -> Settings:
        if self.app_mode != "production":
            return self

        invalid_fields: list[str] = []
        if self.object_store_type != "s3":
            invalid_fields.append("object_store_type")
        if not self.allowed_origins or any("*" in str(origin) for origin in self.allowed_origins):
            invalid_fields.append("allowed_origins")
        if not self.trusted_hosts or any(
            not host.strip() or "*" in host for host in self.trusted_hosts
        ):
            invalid_fields.append("trusted_hosts")
        if self.external_scheme != "https":
            invalid_fields.append("external_scheme")
        if invalid_fields:
            raise ValueError(
                "production requires valid values for: " + ", ".join(invalid_fields)
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
