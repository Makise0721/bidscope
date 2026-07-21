"""Unit tests for the retrieval embedding providers.

The :class:`HashEmbeddingProvider` is the deterministic, offline provider used
in tests and the public demo. It must be stable across processes and runs, so
it derives vectors from cryptographic hashes rather than Python's randomized
hash(). Both providers expose an async ``embed`` contract.
"""

from __future__ import annotations

import math

import pytest
from bidscope.retrieval.embeddings import (
    DIMENSION,
    HashEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
)


class TestHashEmbeddingProvider:
    """Deterministic, normalized, offline embeddings."""

    def test_default_dimension_is_1024(self) -> None:
        provider = HashEmbeddingProvider()
        assert provider.dimension == DIMENSION == 1024

    @pytest.mark.asyncio
    async def test_embeddings_across_runs_are_stable(self) -> None:
        """Same input must yield identical vectors (no Python hash randomization)."""
        provider = HashEmbeddingProvider(dimension=1024)
        first = await provider.embed(["GPU 服务器采购"])
        second = await provider.embed(["GPU 服务器采购"])
        assert first == second

    @pytest.mark.asyncio
    async def test_embedding_has_requested_dimension(self) -> None:
        provider = HashEmbeddingProvider(dimension=1024)
        vectors = await provider.embed(["服务器"])
        assert len(vectors) == 1
        assert len(vectors[0]) == 1024

    @pytest.mark.asyncio
    async def test_embedding_is_l2_normalized(self) -> None:
        provider = HashEmbeddingProvider(dimension=1024)
        (vector,) = await provider.embed(["GPU 服务器采购"])
        l2 = math.sqrt(sum(value * value for value in vector))
        assert abs(l2 - 1.0) < 1e-6

    @pytest.mark.asyncio
    async def test_empty_string_behavior_is_defined(self) -> None:
        """Empty input must produce a deterministic, normalized vector."""
        provider = HashEmbeddingProvider(dimension=1024)
        (vector,) = await provider.embed([""])
        assert len(vector) == 1024
        # Stable across calls.
        assert await provider.embed([""]) == [vector]
        l2 = math.sqrt(sum(value * value for value in vector))
        assert abs(l2 - 1.0) < 1e-6

    @pytest.mark.asyncio
    async def test_batch_preserves_order(self) -> None:
        provider = HashEmbeddingProvider(dimension=1024)
        texts = ["服务器", "智算中心", "GPU 采购", ""]
        vectors = await provider.embed(texts)
        assert len(vectors) == len(texts)
        # Per-position stability.
        for text, vector in zip(texts, vectors, strict=False):
            assert await provider.embed([text]) == [vector]

    @pytest.mark.asyncio
    async def test_different_inputs_differ(self) -> None:
        provider = HashEmbeddingProvider(dimension=1024)
        (a,) = await provider.embed(["服务器"])
        (b,) = await provider.embed(["完全不同"])
        assert a != b


class TestOpenAICompatibleEmbeddingProvider:
    """Port implementation; tests inject a stub transport, no network/key."""

    @pytest.mark.asyncio
    async def test_uses_stub_transport_without_network(self) -> None:
        async def stub_embed(texts: list[str]) -> list[list[float]]:
            return [[0.01] * 1024 for _ in texts]

        provider = OpenAICompatibleEmbeddingProvider(client=stub_embed)
        vectors = await provider.embed(["服务器"])
        assert len(vectors) == 1
        assert len(vectors[0]) == 1024

    def test_exposes_dimension(self) -> None:
        provider = OpenAICompatibleEmbeddingProvider(client=lambda _texts: [])
        assert provider.dimension == 1024
