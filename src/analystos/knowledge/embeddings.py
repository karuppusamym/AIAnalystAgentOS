"""Embedding providers for the knowledge index (P4-K10, amends ADR-0007).

* `hashing` — the zero-dependency deterministic fallback (`context/embeddings.embed`), any dimension.
* `sentence_transformers` — a local sentence-transformer (optional extra `.[embeddings]`, never a
  hard dependency). Air-gapped by default: the model is loaded with `local_files_only`, so nothing
  is downloaded at runtime unless `knowledge_embedding_allow_download` is set. A configured
  dimension below the model's truncates and re-normalises (valid for Matryoshka-trained models;
  for others it is a lossy projection, so leave the dimension unset).
* `auto` (default) — the sentence-transformer when the package and the model are present locally,
  hashing otherwise.

The index records which provider built it (`knowledge_index_state.embedding`); queries always embed
with that provider, so a configuration change never mixes vector spaces. `analystos knowledge
reembed` moves the index to the configured provider (and dimension).
"""
from __future__ import annotations

import importlib.util
import math
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from analystos.core.logging import get_logger

log = get_logger(__name__)

HASHING = "hashing"
SENTENCE_TRANSFORMERS = "sentence_transformers"
HASHING_DEFAULT_DIM = 256
MAX_DIM = 2000  # pgvector HNSW limit for `vector`


class EmbeddingProvider(Protocol):
    name: str
    model: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def provider_id(p: EmbeddingProvider) -> str:
    return f"{p.name}:{p.model}:{p.dim}"


@dataclass
class HashingProvider:
    dim: int = HASHING_DEFAULT_DIM
    name: str = HASHING
    model: str = "feature-hash-v1"

    def embed(self, texts: list[str]) -> list[list[float]]:
        from analystos.context.embeddings import embed

        return [embed(t, self.dim) for t in texts]


_models: dict[tuple[str, bool], Any] = {}
_lock = threading.Lock()


def sentence_transformers_installed() -> bool:
    return importlib.util.find_spec("sentence_transformers") is not None


def _load(model: str, allow_download: bool) -> Any:
    with _lock:
        key = (model, allow_download)
        if key not in _models:
            from sentence_transformers import SentenceTransformer

            _models[key] = SentenceTransformer(model, local_files_only=not allow_download, device="cpu")
        return _models[key]


@dataclass
class SentenceTransformerProvider:
    model: str
    dim: int
    native_dim: int
    allow_download: bool = False
    name: str = SENTENCE_TRANSFORMERS

    def embed(self, texts: list[str]) -> list[list[float]]:
        m = _load(self.model, self.allow_download)
        vecs = m.encode(list(texts), batch_size=64, normalize_embeddings=True, show_progress_bar=False)
        out = []
        for v in vecs:
            row = [float(x) for x in v[: self.dim]]
            if self.dim < self.native_dim:
                norm = math.sqrt(sum(x * x for x in row)) or 1.0
                row = [x / norm for x in row]
            out.append(row)
        return out


def sentence_transformer(model: str, dim: int | None = None, *, allow_download: bool = False) -> SentenceTransformerProvider:
    """Raises (ImportError, OSError, ...) when the package or the model is not available locally."""
    m = _load(model, allow_download)
    getter = getattr(m, "get_embedding_dimension", None) or m.get_sentence_embedding_dimension
    native = int(getter())
    use = min(dim or native, native)
    return SentenceTransformerProvider(model=model, dim=use, native_dim=native, allow_download=allow_download)


def configured(settings: Any = None) -> EmbeddingProvider:
    """The provider the platform is configured to build the index with."""
    from analystos.core.config import get_settings

    s = settings or get_settings()
    kind = (s.knowledge_embedding_provider or "auto").lower()
    dim = s.knowledge_embedding_dim
    if dim is not None and not 1 <= int(dim) <= MAX_DIM:
        raise ValueError(f"knowledge_embedding_dim must be between 1 and {MAX_DIM}")
    if kind == HASHING:
        return HashingProvider(dim=int(dim or HASHING_DEFAULT_DIM))
    if kind in (SENTENCE_TRANSFORMERS, "auto"):
        try:
            return sentence_transformer(s.knowledge_embedding_model, dim,
                                        allow_download=bool(s.knowledge_embedding_allow_download))
        except Exception as exc:  # noqa: BLE001 - missing extra, missing model, broken install
            if kind == SENTENCE_TRANSFORMERS:
                raise
            log.info("sentence-transformer unavailable (%s); knowledge index uses hashing embeddings",
                     type(exc).__name__)
            return HashingProvider(dim=int(dim or HASHING_DEFAULT_DIM))
    raise ValueError(f"unknown knowledge_embedding_provider {kind!r} (hashing | sentence_transformers | auto)")


def from_state(state: dict[str, Any] | None) -> EmbeddingProvider | None:
    """The provider that built the index, to embed queries in the same space. None when it cannot
    be loaded here (the vector leg is then skipped and the lexical leg still answers)."""
    if not state:
        return HashingProvider()
    if state.get("name") == HASHING:
        return HashingProvider(dim=int(state.get("dim") or HASHING_DEFAULT_DIM))
    if state.get("name") == SENTENCE_TRANSFORMERS:
        try:
            from analystos.core.config import get_settings

            return sentence_transformer(str(state["model"]), int(state["dim"]),
                                        allow_download=bool(get_settings().knowledge_embedding_allow_download))
        except Exception as exc:  # noqa: BLE001
            log.warning("index embedding provider %s unavailable: %s", state.get("model"), type(exc).__name__)
            return None
    return None


def describe(p: EmbeddingProvider) -> dict[str, Any]:
    return {"name": p.name, "model": p.model, "dim": p.dim, "id": provider_id(p)}
