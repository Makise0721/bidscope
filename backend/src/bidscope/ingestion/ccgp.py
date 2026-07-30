"""Authorized, bounded CCGP source client.

This module only implements transport and the operator-supplied endpoint
contract.  It does not infer a public query API, retry authorization failures,
or decide how many pages belong to one acquisition run.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC
from email.utils import parsedate_to_datetime
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit

import httpx

from bidscope.clock import Clock, SystemClock
from bidscope.domain.enums import SourceName
from bidscope.domain.provenance import OFFICIAL_HOSTS_BY_SOURCE
from bidscope.ingestion.ports import (
    AuthorizedEndpoint,
    AuthorizedSourcePage,
    RequestSigner,
    SignableRequest,
)

MAX_RETRY_AFTER_SECONDS = 86_400
DEFAULT_RETRY_AFTER_SECONDS = 60
MAX_CURSOR_LENGTH = 512


class SourceClientError(RuntimeError):
    """Bounded source failure safe to persist in an acquisition record."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool,
        retry_after_seconds: int | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.status_code = status_code


class SourceAuthorizationError(SourceClientError):
    def __init__(self, status_code: int) -> None:
        super().__init__(
            "authorized source rejected the configured credentials",
            code="authorization_rejected",
            retryable=False,
            status_code=status_code,
        )


class SourceRateLimitedError(SourceClientError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            "authorized source rate limited the request",
            code="rate_limited",
            retryable=True,
            retry_after_seconds=retry_after_seconds,
            status_code=429,
        )


class SourceResponseTooLargeError(SourceClientError):
    def __init__(self, maximum_bytes: int) -> None:
        super().__init__(
            f"authorized source response exceeds the configured maximum of {maximum_bytes} bytes",
            code="response_too_large",
            retryable=False,
        )


class SourceTimeoutError(SourceClientError):
    def __init__(self) -> None:
        super().__init__(
            "authorized source request timed out",
            code="timeout",
            retryable=True,
        )


class SourceSigningError(SourceClientError):
    def __init__(self) -> None:
        super().__init__(
            "authorized source request signing failed",
            code="signature_failed",
            retryable=False,
        )


class SourcePayloadError(SourceClientError):
    def __init__(self, detail: str) -> None:
        super().__init__(
            f"authorized source response is invalid: {detail}",
            code="invalid_response",
            retryable=False,
        )


class SourceTransportError(SourceClientError):
    def __init__(self) -> None:
        super().__init__(
            "authorized source transport failed",
            code="transport_error",
            retryable=True,
        )


class SourceHTTPError(SourceClientError):
    def __init__(self, status_code: int) -> None:
        retryable = status_code >= 500
        super().__init__(
            f"authorized source returned HTTP {status_code}",
            code="source_http_error",
            retryable=retryable,
            status_code=status_code,
        )


class AuthorizedSourceClient:
    """Fetch one page from an approved CCGP endpoint contract."""

    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        endpoint: AuthorizedEndpoint,
        signer: RequestSigner,
        clock: Clock | None = None,
        timeout_seconds: float = 20,
        max_response_bytes: int = 10 * 1024 * 1024,
        max_retry_after_seconds: int = MAX_RETRY_AFTER_SECONDS,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = self._validate_base_url(base_url)
        if not client_id.strip():
            raise ValueError("authorized client_id must be non-blank")
        if timeout_seconds <= 0 or max_response_bytes <= 0:
            raise ValueError("source client limits must be positive")
        if max_retry_after_seconds <= 0:
            raise ValueError("max_retry_after_seconds must be positive")
        self.client_id = client_id
        self.endpoint = endpoint
        self.signer = signer
        self.clock = clock or SystemClock()
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_retry_after_seconds = min(max_retry_after_seconds, MAX_RETRY_AFTER_SECONDS)
        self._http_client = http_client or httpx.AsyncClient()
        self._owns_http_client = http_client is None

    async def close(self) -> None:
        """Close the internally-created transport; injected clients remain caller-owned."""
        if self._owns_http_client:
            await self._http_client.aclose()

    async def fetch_page(self, cursor: str | None) -> AuthorizedSourcePage:
        """Fetch and validate exactly one page without applying retry policy."""
        request_url, body = self._build_request(cursor)
        timestamp = self.clock.now()
        signing_failed = False
        try:
            signed = self.signer.sign(
                SignableRequest(
                    method=self.endpoint.method,
                    url=request_url,
                    body=body,
                    timestamp=timestamp,
                    client_id=self.client_id,
                )
            )
        except Exception as error:
            del error
            signing_failed = True
        if signing_failed:
            raise SourceSigningError()
        headers = {"Accept": "application/json", **signed}
        if self.endpoint.method == "POST":
            headers["Content-Type"] = "application/json"

        try:
            async with self._http_client.stream(
                self.endpoint.method,
                request_url,
                headers=headers,
                content=body,
                timeout=self.timeout_seconds,
                follow_redirects=False,
            ) as response:
                if response.status_code in (401, 403):
                    raise SourceAuthorizationError(response.status_code)
                if response.status_code == 429:
                    raise SourceRateLimitedError(self._retry_after_seconds(response))
                if response.status_code < 200 or response.status_code >= 300:
                    raise SourceHTTPError(response.status_code)

                declared_length = response.headers.get("Content-Length")
                if declared_length is not None:
                    try:
                        if int(declared_length) > self.max_response_bytes:
                            raise SourceResponseTooLargeError(self.max_response_bytes)
                    except ValueError as error:
                        raise SourcePayloadError("Content-Length is invalid") from error
                response_bytes = await self._read_bounded_response(response)
        except httpx.TimeoutException as error:
            raise SourceTimeoutError() from error
        except httpx.TransportError as error:
            raise SourceTransportError() from error
        payload = self._decode_payload(response_bytes)
        items, next_cursor = self._parse_page(payload)
        return AuthorizedSourcePage(
            cursor_before=cursor,
            next_cursor=next_cursor,
            items=items,
            response_bytes=response_bytes,
            response_sha256=sha256(response_bytes).hexdigest(),
            retrieved_at=timestamp,
            status_code=response.status_code,
            source_url=f"{self.base_url}{self.endpoint.path}",
            response_items_field=self.endpoint.items_field,
            notice_field_map=self.endpoint.notice_field_map,
        )

    async def _read_bounded_response(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > self.max_response_bytes:
                raise SourceResponseTooLargeError(self.max_response_bytes)
            chunks.append(chunk)
        return b"".join(chunks)

    def _build_request(self, cursor: str | None) -> tuple[str, bytes]:
        if cursor is not None and len(cursor) > MAX_CURSOR_LENGTH:
            raise SourcePayloadError("source cursor exceeds the maximum length")
        fields = dict(self.endpoint.request_fields)
        if cursor is not None:
            fields[self.endpoint.cursor_field] = cursor
        if self.endpoint.method == "GET":
            query = httpx.QueryParams(fields)
            url = f"{self.base_url}{self.endpoint.path}"
            if query:
                url = f"{url}?{query}"
            return url, b""
        body = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        return f"{self.base_url}{self.endpoint.path}", body

    @staticmethod
    def _validate_base_url(base_url: str) -> str:
        parsed = urlsplit(base_url)
        official_hosts = OFFICIAL_HOSTS_BY_SOURCE[SourceName.CCGP]
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.hostname.casefold() not in official_hosts
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url must be an HTTPS origin on an official CCGP host")
        return base_url.rstrip("/")

    @staticmethod
    def _decode_payload(response_bytes: bytes) -> Mapping[str, Any]:
        try:
            payload = json.loads(response_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SourcePayloadError("response is not valid UTF-8 JSON") from error
        if not isinstance(payload, dict):
            raise SourcePayloadError("JSON root must be an object")
        return payload

    def _parse_page(
        self, payload: Mapping[str, Any]
    ) -> tuple[tuple[Mapping[str, Any], ...], str | None]:
        if self.endpoint.items_field not in payload:
            raise SourcePayloadError(f"missing {self.endpoint.items_field!r} field")
        raw_items = payload[self.endpoint.items_field]
        if not isinstance(raw_items, list):
            raise SourcePayloadError(f"{self.endpoint.items_field!r} field must be a list")
        items: list[Mapping[str, Any]] = []
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                raise SourcePayloadError(f"items[{index}] must be an object")
            items.append(dict(item))

        if self.endpoint.next_cursor_field not in payload:
            raise SourcePayloadError(f"missing {self.endpoint.next_cursor_field!r} field")
        raw_next_cursor = payload[self.endpoint.next_cursor_field]
        if raw_next_cursor is not None and not isinstance(raw_next_cursor, str):
            raise SourcePayloadError(
                f"{self.endpoint.next_cursor_field!r} must be a string or null"
            )
        if raw_next_cursor is not None and len(raw_next_cursor) > MAX_CURSOR_LENGTH:
            raise SourcePayloadError("source cursor exceeds the maximum length")
        return tuple(items), raw_next_cursor or None

    def _retry_after_seconds(self, response: httpx.Response) -> int:
        raw = response.headers.get("Retry-After")
        if raw is None:
            return min(DEFAULT_RETRY_AFTER_SECONDS, self.max_retry_after_seconds)
        try:
            seconds = int(raw.strip())
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(raw)
            except (TypeError, ValueError, OverflowError):
                return min(DEFAULT_RETRY_AFTER_SECONDS, self.max_retry_after_seconds)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            seconds = max(0, int((retry_at - self.clock.now()).total_seconds()))
        return max(0, min(seconds, self.max_retry_after_seconds))
