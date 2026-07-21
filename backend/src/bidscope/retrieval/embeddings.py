"""Embedding providers for tender retrieval.

Two providers, one contract:

* :class:`HashEmbeddingProvider` — the deterministic, fully offline provider
  used by tests and the public demo. It derives each dimension from a
  SHA-256 digest, so vectors are identical across processes and runs. It never
  touches a network or a model key.
* :class:`OpenAICompatibleEmbeddingProvider` — an async port over an
  OpenAI-compatible endpoint, used when real embeddings are configured. Tests
  inject a stub transport instead of calling a live endpoint.

Both expose ``async embed(texts) -> list[vector]`` and a ``dimension``.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Awaitable, Callable

#: Default embedding dimension. Matches the ``VECTOR(1024)`` column in the
#: ``notice_versions`` table, so providers must not drift from this value.
DIMENSION = 1024
#: Classic reciprocal-rank-fusion constant. Kept here as the single source of
#: truth so the search module does not sprinkle magic numbers.
RRF_K = 60


def _l2_normalize(vector: list[float]) -> list[float]:
    """Scale a vector to unit L2 length. A zero vector is returned unchanged."""
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector[:]
    return [value / norm for value in vector]


class HashEmbeddingProvider:
    """Deterministic, offline embedding provider.

    For each input text, dimensions are filled from successive SHA-256 digests
    of ``"{counter}:{text}"`` (counter mode), mapped to ``[-1, 1]``, then
    L2-normalized. Because the whole pipeline is a pure cryptographic hash, the
    same text always produces the same unit vector — regardless of process,
    interpreter, or PYTHONHASHSEED.
    """

    def __init__(self, dimension: int = DIMENSION) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        self.dimension = dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        values: list[float] = []
        counter = 0
        while len(values) < self.dimension:
            digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
            # Each digest is 32 bytes -> 16 uint16 samples in [0, 1).
            for offset in range(0, len(digest), 2):
                sample = int.from_bytes(digest[offset : offset + 2], "big") / 65535.0
                values.append(sample * 2.0 - 1.0)
                if len(values) >= self.dimension:
                    break
            counter += 1
        return _l2_normalize(values[: self.dimension])


# A transport the OpenAI-compatible provider delegates to. Tests inject a stub;
# production injects an async callable around an OpenAI-compatible client.
EmbedTransport = Callable[[list[str]], Awaitable[list[list[float]]]]


class OpenAICompatibleEmbeddingProvider:
    """Async port over an OpenAI-compatible embedding endpoint.

    Offline by default — it does nothing until a ``client`` transport is
    supplied. Unit and integration tests pass a stub transport so no API key or
    network access is required.
    """

    def __init__(
        self,
        client: EmbedTransport | None = None,
        *,
        dimension: int = DIMENSION,
    ) -> None:
        self.client = client
        self.dimension = dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self.client is None:
            raise RuntimeError(
                "OpenAICompatibleEmbeddingProvider has no client transport configured"
            )
        return await self.client(texts)
