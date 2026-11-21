"""Text embeddings: a local multilingual model (default, no API key, runs on CPU) or any
embedding model LiteLLM supports (OpenAI, Mistral, Cohere, Azure, Ollama, ...)."""

from __future__ import annotations

import math
import threading
from abc import ABC, abstractmethod
from typing import Any

from ..config import ROOT, Settings, get_settings

LOCAL_DEFAULT = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"  # German + English, ~220 MB
BATCH = 32


class EmbeddingError(RuntimeError):
    pass


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class Embedder(ABC):
    model_id: str  # stored next to every vector; changing the model triggers a re-index

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    @abstractmethod
    def embed_query(self, text: str) -> list[float]: ...


class LocalEmbedder(Embedder):
    """fastembed (ONNX). The model is downloaded once into data/models/ on first use."""

    def __init__(self, model: str = LOCAL_DEFAULT):
        self.model = model
        self.model_id = f"local:{model}"
        self._impl: Any = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._impl is None:
                try:
                    from fastembed import TextEmbedding

                    self._impl = TextEmbedding(model_name=self.model, cache_dir=str(ROOT / "data" / "models"))
                except Exception as e:
                    raise EmbeddingError(
                        f"The local embedding model '{self.model}' couldn't be loaded ({type(e).__name__}: {e}). "
                        "The first start needs internet access to download it."
                    ) from None
            return self._impl

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        impl = self._load()
        return [_normalize(list(map(float, v))) for v in impl.passage_embed(texts, batch_size=BATCH)]

    def embed_query(self, text: str) -> list[float]:
        impl = self._load()
        return _normalize(list(map(float, next(iter(impl.query_embed(text))))))


class LiteLLMEmbedder(Embedder):
    def __init__(self, model: str, api_key: str | None, base_url: str | None):
        if not model:
            raise EmbeddingError("EMBEDDING_PROVIDER is litellm but EMBEDDING_MODEL is empty, e.g. openai/text-embedding-3-small.")
        self.model, self.api_key, self.base_url = model, api_key, base_url
        self.model_id = f"litellm:{model}"

    def _call(self, texts: list[str]) -> list[list[float]]:
        import litellm

        from ..llm import provider  # noqa: F401  (LiteLLM setup)

        kwargs: dict[str, Any] = {"model": self.model, "input": texts, "timeout": 60}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.base_url:
            kwargs["api_base"] = self.base_url
        try:
            resp = litellm.embedding(aembedding=False, **kwargs)
        except Exception as e:
            raise EmbeddingError(f"The embedding provider failed: {type(e).__name__}: {str(e)[:200]}") from None
        return [_normalize(list(map(float, d["embedding"]))) for d in resp.data]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH):
            out.extend(self._call(texts[i:i + BATCH]))
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._call([text])[0]


def build_embedder(s: Settings | None = None) -> Embedder:
    s = s or get_settings()
    if s.embedding_provider == "litellm":
        key = s.secret("embedding_api_key") or s.secret("llm_api_key")
        return LiteLLMEmbedder(s.embedding_model, key, s.embedding_base_url)
    return LocalEmbedder(s.embedding_model or LOCAL_DEFAULT)
