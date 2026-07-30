"""Unit tests for the bounded, operator-configured CCGP source client."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256

import httpx
import pytest
from bidscope.clock import FixedClock
from bidscope.ingestion.ccgp import (
    AuthorizedSourceClient,
    SourceAuthorizationError,
    SourceHTTPError,
    SourcePayloadError,
    SourceRateLimitedError,
    SourceResponseTooLargeError,
    SourceSigningError,
    SourceTimeoutError,
)
from bidscope.ingestion.ports import AuthorizedEndpoint, SignableRequest

FIXED_NOW = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)


class RecordingSigner:
    def __init__(self) -> None:
        self.requests: list[SignableRequest] = []

    def sign(self, request: SignableRequest) -> dict[str, str]:
        self.requests.append(request)
        return {
            "X-Authorization-Signature": "synthetic-signature",
            "X-Authorization-Timestamp": request.timestamp.isoformat(),
        }


class FailingSigner:
    def sign(self, _request: SignableRequest) -> dict[str, str]:
        raise RuntimeError("private-key-material")


def _endpoint() -> AuthorizedEndpoint:
    return AuthorizedEndpoint(
        method="POST",
        path="/authorized/v1/notices",
        cursor_field="cursor",
        items_field="items",
        next_cursor_field="next_cursor",
        request_fields={"scope": "approved"},
    )


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    signer: RecordingSigner | None = None,
    *,
    base_url: str = "https://www.ccgp.gov.cn",
    max_response_bytes: int = 1024,
) -> tuple[AuthorizedSourceClient, RecordingSigner, httpx.AsyncClient]:
    actual_signer = signer or RecordingSigner()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AuthorizedSourceClient(
        base_url=base_url,
        client_id="authorized-client",
        endpoint=_endpoint(),
        signer=actual_signer,
        clock=FixedClock(FIXED_NOW),
        max_response_bytes=max_response_bytes,
        timeout_seconds=3,
        http_client=http_client,
    )
    return client, actual_signer, http_client


@pytest.mark.asyncio
async def test_fetch_page_builds_operator_shaped_signed_request_and_parses_cursor() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"items": [{"notice_id": "n-1"}], "next_cursor": "cursor-2"},
        )

    client, signer, http_client = _client(handler)
    try:
        page = await client.fetch_page("cursor-1")
    finally:
        await http_client.aclose()

    assert page.items == ({"notice_id": "n-1"},)
    assert page.next_cursor == "cursor-2"
    assert page.cursor_before == "cursor-1"
    assert page.status_code == 200
    assert page.response_sha256 == sha256(page.response_bytes).hexdigest()
    assert len(requests) == 1
    assert requests[0].url == "https://www.ccgp.gov.cn/authorized/v1/notices"
    assert json.loads(requests[0].content) == {"cursor": "cursor-1", "scope": "approved"}
    assert requests[0].headers["X-Authorization-Signature"] == "synthetic-signature"
    assert len(signer.requests) == 1
    assert signer.requests[0].method == "POST"
    assert signer.requests[0].url == str(requests[0].url)
    assert signer.requests[0].body == requests[0].content
    assert signer.requests[0].timestamp == FIXED_NOW
    assert signer.requests[0].client_id == "authorized-client"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://www.ccgp.gov.cn",
        "https://not-ccgp.example.test",
        "https://user:password@www.ccgp.gov.cn",
        "https://www.ccgp.gov.cn:8443",
    ],
)
def test_client_rejects_non_official_or_unsafe_base_url(base_url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS|official"):
        _client(lambda _request: httpx.Response(200, json={}), base_url=base_url)


@pytest.mark.asyncio
async def test_fetch_page_rejects_response_before_json_parsing_when_over_limit() -> None:
    client, _signer, http_client = _client(
        lambda _request: httpx.Response(200, content=b"not-json"), max_response_bytes=4
    )
    try:
        with pytest.raises(SourceResponseTooLargeError, match="maximum"):
            await client.fetch_page(None)
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_fetch_page_does_not_follow_redirects_to_an_unapproved_host() -> None:
    call_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            302,
            headers={"Location": "https://not-ccgp.example.test/redirected"},
        )

    client, _signer, http_client = _client(handler)
    try:
        with pytest.raises(SourceHTTPError):
            await client.fetch_page(None)
    finally:
        await http_client.aclose()

    assert call_count == 1


@pytest.mark.asyncio
async def test_fetch_page_exposes_bounded_retry_after_for_rate_limit() -> None:
    client, _signer, http_client = _client(
        lambda _request: httpx.Response(429, headers={"Retry-After": "999999"})
    )
    try:
        with pytest.raises(SourceRateLimitedError) as error:
            await client.fetch_page(None)
    finally:
        await http_client.aclose()

    assert error.value.retry_after_seconds == 86_400
    assert error.value.retryable is True


@pytest.mark.asyncio
async def test_fetch_page_converts_timeout_to_bounded_source_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout")

    client, _signer, http_client = _client(handler)
    try:
        with pytest.raises(SourceTimeoutError) as error:
            await client.fetch_page(None)
    finally:
        await http_client.aclose()

    assert error.value.retryable is True


@pytest.mark.asyncio
async def test_fetch_page_redacts_signer_failures_as_non_retryable_source_errors() -> None:
    client, _signer, http_client = _client(lambda _request: httpx.Response(200), FailingSigner())
    try:
        with pytest.raises(SourceSigningError) as error:
            await client.fetch_page(None)
    finally:
        await http_client.aclose()

    assert error.value.code == "signature_failed"
    assert error.value.retryable is False
    assert "private-key-material" not in str(error.value)


@pytest.mark.asyncio
async def test_fetch_page_rejects_invalid_json() -> None:
    client, _signer, http_client = _client(
        lambda _request: httpx.Response(200, content=b"{not-json")
    )
    try:
        with pytest.raises(SourcePayloadError, match="JSON"):
            await client.fetch_page(None)
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_fetch_page_does_not_retry_authorization_rejection(status: int) -> None:
    call_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(status)

    client, _signer, http_client = _client(handler)
    try:
        with pytest.raises(SourceAuthorizationError) as error:
            await client.fetch_page(None)
    finally:
        await http_client.aclose()

    assert call_count == 1
    assert error.value.retryable is False


@pytest.mark.asyncio
async def test_fetch_page_rejects_malformed_page_shape() -> None:
    client, _signer, http_client = _client(
        lambda _request: httpx.Response(200, json={"items": {"not": "a-list"}})
    )
    try:
        with pytest.raises(SourcePayloadError, match="items"):
            await client.fetch_page(None)
    finally:
        await http_client.aclose()
