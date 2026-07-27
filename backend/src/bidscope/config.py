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
                self._collect_error_input_values(item.get("input"), secret_values, root=True)
            sanitized_errors = [
                self._sanitize_error_item(cast(Mapping[str, Any], item), secret_values)
                for item in raw_errors
            ]
            raise ValidationError.from_exception_data(
                self.__class__.__name__, cast(Any, sanitized_errors)
            ) from error

    @staticmethod
    def _secret_text(value: Any) -> str | None:
        if isinstance(value, SecretStr):
            value = getattr(value, "_secret_value", None)
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
                if key in cls._secret_field_names:
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
        if "input" in item:
            sanitized["input"] = cls._sanitize_error_value(item["input"], secret_values)
        if "ctx" in item:
            sanitized["ctx"] = cls._sanitize_error_context(item["ctx"], secret_values)
        return sanitized

    @classmethod
    def _sanitize_error_context(cls, value: Any, secret_values: set[str]) -> Any:
        if isinstance(value, Mapping):
            return {
                key: cls._sanitize_error_context(item, secret_values)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [cls._sanitize_error_context(item, secret_values) for item in value]
        if isinstance(value, BaseException):
            sanitized_args = tuple(
                cls._sanitize_error_context(item, secret_values) for item in value.args
            )
            if sanitized_args == value.args:
                return value
            try:
                return type(value)(*sanitized_args)
            except TypeError:
                return value
        if isinstance(value, str):
            return cls._redact_secret_string(value, secret_values)
        return value

    @staticmethod
    def _redact_secret_string(value: str, secret_values: set[str]) -> str:
        return "**********" if value in secret_values else value

    @classmethod
    def _sanitize_error_value(cls, value: Any, secret_values: set[str]) -> Any:
        if isinstance(value, SecretStr):
            return value
        if isinstance(value, Mapping):
            return {
                key: (
                    value[key]
                    if isinstance(value[key], SecretStr)
                    else SecretStr(str(value[key]))
                )
                if key in cls._secret_field_names and value[key] is not None
                else cls._sanitize_error_value(value[key], secret_values)
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

        raw_token = self._secret_text(self.admin_token)
        token = raw_token.strip() if raw_token is not None else ""
        normalized_token = token.casefold()
        if not token:
            raise ValueError("admin_token must be non-empty in production")
        if normalized_token in self.production_placeholder_tokens:
            raise ValueError("admin_token must not be a production placeholder")
        if len(token) < self.admin_token_min_length:
            raise ValueError("admin_token must meet admin_token_min_length in production")
        return self

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
