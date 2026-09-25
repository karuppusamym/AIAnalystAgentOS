"""Deterministic local embeddings (feature hashing of word + char-trigram tokens). No network, no
data leaves the platform; good enough for glossary/term retrieval. Swap for a model embedding
provider behind the same function when one is approved (ADR-0007)."""
from __future__ import annotations

import hashlib
import math
import re

from analystos.db.models import EMBEDDING_DIM

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    words = _WORD.findall(text.lower().replace("_", " "))
    grams = [f"#{w[i:i + 3]}" for w in words for i in range(max(1, len(w) - 2))]
    return words + grams + [f"{a} {b}" for a, b in zip(words, words[1:], strict=False)]


def embed(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    vec = [0.0] * dim
    for tok in _tokens(text):
        h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
        vec[h % dim] += 1.0 if (h >> 63) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]
