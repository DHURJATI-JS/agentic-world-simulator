"""
Text embeddings for pgvector similarity search.

Primary: EmbeddingGemma served by the Docker Ollama container (OpenAI-compatible
/v1/embeddings). Fallback: a deterministic feature-hashing embedder so semantic
search keeps working offline. Each stored vector records its `embed_model`, and
queries only compare vectors from the same model; a periodic job re-embeds rows
whose model differs from the current one.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import time

import httpx
import numpy as np
import openai

import config

log = logging.getLogger("worldsim.embed")

HASH_MODEL = "hash-v1"
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = set(
    "a an and are as at be been but by for from has have he her his i in is it its of on or "
    "our she that the their them they this to was were will with you your we not no after over "
    "into than then there these those up out about more new says said say also amid who what".split()
)


def hash_embed(text: str, dim: int = config.EMBEDDING_DIM) -> np.ndarray:
    tokens = [t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOP and len(t) > 1]
    feats: dict[str, float] = {}
    for tok in tokens:
        feats[tok] = feats.get(tok, 0.0) + 1.0
    for a, b in zip(tokens, tokens[1:]):
        key = f"{a}_{b}"
        feats[key] = feats.get(key, 0.0) + 0.7
    vec = np.zeros(dim, dtype=np.float32)
    for feat, tf in feats.items():
        h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "little")
        idx = h % dim
        sign = 1.0 if (h >> 63) & 1 else -1.0
        vec[idx] += sign * (1.0 + math.log(tf))
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec /= norm
    return vec


def _fit(vec: list[float], dim: int) -> np.ndarray | None:
    arr = np.asarray(vec, dtype=np.float32)
    if arr.size < dim:
        return None
    if arr.size > dim:  # Matryoshka-style truncation (EmbeddingGemma supports it)
        arr = arr[:dim]
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm > 0 else arr


class Embedder:
    def __init__(self) -> None:
        self.dim = config.EMBEDDING_DIM
        self.remote_model = config.EMBEDDING_MODEL
        self.remote_ok: bool | None = None
        self._last_probe = 0.0
        self.client = openai.AsyncOpenAI(
            base_url=config.EMBEDDING_BASE_URL,
            api_key=config.EMBEDDING_API_KEY or "none",
            timeout=httpx.Timeout(60.0, connect=5.0),
            max_retries=0,
        )

    @property
    def model_name(self) -> str:
        return self.remote_model if self.remote_ok else HASH_MODEL

    async def probe(self) -> bool:
        self._last_probe = time.monotonic()
        try:
            resp = await self.client.embeddings.create(model=self.remote_model, input=["ping"])
            ok = bool(resp.data) and _fit(resp.data[0].embedding, self.dim) is not None
        except Exception as exc:  # noqa: BLE001
            log.info("remote embeddings unavailable (%s) - using %s", type(exc).__name__, HASH_MODEL)
            ok = False
        self.remote_ok = ok
        return ok

    async def embed(self, texts: list[str]) -> tuple[list[np.ndarray], str]:
        texts = [(t or "")[:4000] for t in texts]
        if not texts:
            return [], self.model_name
        if self.remote_ok is None or (not self.remote_ok and time.monotonic() - self._last_probe > 600):
            await self.probe()
        if self.remote_ok:
            try:
                vectors: list[np.ndarray] = []
                for start in range(0, len(texts), 32):
                    chunk = texts[start : start + 32]
                    resp = await self.client.embeddings.create(model=self.remote_model, input=chunk)
                    ordered = sorted(resp.data, key=lambda d: d.index)
                    for item in ordered:
                        fitted = _fit(item.embedding, self.dim)
                        if fitted is None:
                            raise ValueError("embedding dimension too small")
                        vectors.append(fitted)
                return vectors, self.remote_model
            except Exception as exc:  # noqa: BLE001
                log.warning("remote embedding failed (%s) - falling back to %s", exc, HASH_MODEL)
                self.remote_ok = False
        return [hash_embed(t, self.dim) for t in texts], HASH_MODEL

    async def embed_one(self, text: str) -> tuple[np.ndarray, str]:
        vectors, model = await self.embed([text])
        return vectors[0], model
