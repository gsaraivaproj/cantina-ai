"""The embeddings port, and an adapter for a local Ollama server.

An embedding model turns a text into a fixed-size vector, so that texts with a
similar meaning point in similar directions. The question and the chunks must go
through the same model: each model has its own "map", and vectors from different
models can't be compared.
"""

import math
from typing import Protocol

import httpx2 as httpx


class Embedder(Protocol):
    model: str

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """One L2-normalized vector per text, in the same order."""
        ...


class OllamaEmbedder:
    """Calls Ollama's /api/embed. Runs on our own server: the text never leaves it."""

    def __init__(
        self, base_url: str, model: str, batch_size: int = 16, keep_alive: str | None = None
    ) -> None:
        self.model = model
        self._batch_size = batch_size
        # How long Ollama keeps the model in memory after a call (its default is 5m).
        # Short on machines where RAM is tight; the next call reloads it.
        self._keep_alive = keep_alive
        # CPU inference is slow on the first call (the model is loaded into RAM).
        self._client = httpx.AsyncClient(base_url=base_url, timeout=300.0)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            body: dict[str, object] = {"model": self.model, "input": batch}
            if self._keep_alive is not None:
                body["keep_alive"] = self._keep_alive
            response = await self._client.post("/api/embed", json=body)
            response.raise_for_status()
            vectors.extend(normalize(v) for v in response.json()["embeddings"])
        return vectors

    async def pull_model(self) -> None:
        """Download the model into Ollama's volume (a no-op when it is already there)."""
        response = await self._client.post(
            "/api/pull", json={"model": self.model, "stream": False}, timeout=1800.0
        )
        response.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()


def normalize(vector: list[float]) -> list[float]:
    """Scale to length 1, so cosine similarity is just the dot product."""
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else vector


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two normalized vectors: 1 = same direction, ~0 = unrelated."""
    return sum(x * y for x, y in zip(a, b, strict=True))


def to_pgvector(vector: list[float]) -> str:
    """pgvector's text format, e.g. '[0.1,0.2]', cast with %s::vector in SQL."""
    return "[" + ",".join(f"{x:.7f}" for x in vector) + "]"
