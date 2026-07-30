"""Typed ports shared by authorized source clients and the ingestion service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class AuthorizedEndpoint:
    """Operator-supplied request and response field contract.

    The client deliberately does not contain a public CCGP query path or a
    guessed response schema.  The approved endpoint specification supplies the
    path and the names of the request/response fields at process startup.
    """

    method: Literal["GET", "POST"]
    path: str
    cursor_field: str
    items_field: str
    next_cursor_field: str
    request_fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.path.startswith("/") or "?" in self.path or "#" in self.path:
            raise ValueError("authorized endpoint path must be an absolute path without query data")
        if not all(
            isinstance(field_name, str) and field_name.strip()
            for field_name in (self.cursor_field, self.items_field, self.next_cursor_field)
        ):
            raise ValueError("authorized endpoint field names must be non-blank")


@dataclass(frozen=True)
class SignableRequest:
    """Canonical request shape handed to operator-provided signing code."""

    method: str
    url: str
    body: bytes
    timestamp: datetime
    client_id: str


class RequestSigner(Protocol):
    """Signs a request without exposing signing implementation to the client."""

    def sign(self, request: SignableRequest) -> Mapping[str, str]: ...


@dataclass(frozen=True)
class AuthorizedSourcePage:
    """One bounded, immutable-in-memory source response page."""

    cursor_before: str | None
    next_cursor: str | None
    items: tuple[Mapping[str, Any], ...]
    response_bytes: bytes
    response_sha256: str
    retrieved_at: datetime
    status_code: int
    source_url: str


class SourceClient(Protocol):
    async def fetch_page(self, cursor: str | None) -> AuthorizedSourcePage: ...


class SourceObjectStore(Protocol):
    def put_bytes(self, key: str, data: bytes) -> str: ...


class BundleMaterializer(Protocol):
    def materialize(
        self,
        page: AuthorizedSourcePage,
        *,
        batch_id: str,
        data_contract: Any,
    ) -> Any: ...


class SnapshotImporterPort(Protocol):
    async def import_bundle(self, bundle: Any) -> Any: ...


class AcquisitionRepository(Protocol):
    async def get_or_create_source_sync_cursor(
        self, source: str, cursor_value: str, watermark_at: datetime
    ) -> Any: ...

    async def create_acquisition_run(
        self, source: str, started_at: datetime, cursor_before: str, *, status: str = "running"
    ) -> Any: ...

    async def advance_source_sync_cursor(self, **kwargs: Any) -> bool: ...

    async def finalize_acquisition_run(self, **kwargs: Any) -> Any: ...


AuditRecorder = Callable[[Mapping[str, object]], Awaitable[None]]
Committer = Callable[[], Awaitable[None]]
