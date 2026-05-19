"""
storage/vector_store.py
------------------------
ChromaDB-backed semantic vector store for code context retrieval.
Used by the context engine to find relevant existing code patterns.

Phase 2+: indexes the full repository; finds similar code to the diff.
Falls back gracefully if ChromaDB is not installed.
"""
from __future__ import annotations
import hashlib
import logging
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

try:
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    HAS_CHROMA = True
except ImportError:
    HAS_CHROMA = False
    log.info("chromadb not installed — vector store disabled")


@runtime_checkable
class VectorStore(Protocol):
    def upsert(self, doc_id: str, text: str, metadata: dict) -> None: ...
    def search(self, query: str, n_results: int = 5) -> list[dict]: ...
    def delete(self, doc_id: str) -> None: ...


# ── ChromaDB implementation ───────────────────────────────────────────────────

class ChromaVectorStore:

    def __init__(self, host: str = "localhost", port: int = 8000, collection: str = "code_context") -> None:
        if not HAS_CHROMA:
            raise ImportError("pip install chromadb to enable vector store")
        self._client = chromadb.HttpClient(
            host=host, port=port,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self._col = self._client.get_or_create_collection(
            name=collection,
            metadata={"hnsw:space": "cosine"},
        )

    def upsert(self, doc_id: str, text: str, metadata: dict) -> None:
        self._col.upsert(
            ids=[doc_id],
            documents=[text],
            metadatas=[metadata],
        )

    def search(self, query: str, n_results: int = 5) -> list[dict]:
        if self._col.count() == 0:
            return []
        results = self._col.query(query_texts=[query], n_results=min(n_results, self._col.count()))
        hits = []
        for i, doc in enumerate(results.get("documents", [[]])[0]):
            meta = results.get("metadatas", [[]])[0][i]
            hits.append({"text": doc, "metadata": meta, "distance": results["distances"][0][i]})
        return hits

    def delete(self, doc_id: str) -> None:
        self._col.delete(ids=[doc_id])


# ── In-memory implementation (testing / no-ChromaDB fallback) ─────────────────

class InMemoryVectorStore:
    """Keyword-based fallback when ChromaDB is not available."""

    def __init__(self) -> None:
        self._docs: dict[str, dict] = {}

    def upsert(self, doc_id: str, text: str, metadata: dict) -> None:
        self._docs[doc_id] = {"text": text, "metadata": metadata}

    def search(self, query: str, n_results: int = 5) -> list[dict]:
        """Simple keyword overlap scoring."""
        query_tokens = set(query.lower().split())
        scored = []
        for doc_id, doc in self._docs.items():
            tokens  = set(doc["text"].lower().split())
            overlap = len(query_tokens & tokens) / max(len(query_tokens), 1)
            scored.append((overlap, doc))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored[:n_results]]

    def delete(self, doc_id: str) -> None:
        self._docs.pop(doc_id, None)


# ── Factory ────────────────────────────────────────────────────────────────────

def make_vector_store(settings=None) -> VectorStore:
    from config.settings import get_settings
    cfg = settings or get_settings()
    if HAS_CHROMA and cfg.chroma_host:
        try:
            return ChromaVectorStore(host=cfg.chroma_host, port=cfg.chroma_port)
        except Exception as e:
            log.warning("ChromaDB connection failed (%s) — using in-memory fallback", e)
    return InMemoryVectorStore()


def make_doc_id(file_path: str, content: str) -> str:
    """Stable document ID from path + content hash."""
    digest = hashlib.sha256(content.encode()).hexdigest()[:12]
    return f"{file_path.replace('/', '_')}_{digest}"
