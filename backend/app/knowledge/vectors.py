"""Vector indexes behind one interface: ChromaDB (local folder) or pgvector (inside Postgres/Supabase).

Chunk text lives in the main database (kb_chunks); the index only holds chunk id -> vector.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa


@dataclass
class VectorItem:
    """One embedded passage. A chunk (~800 tokens, what the LLM reads) has several short passages,
    because small embedding models only read the first ~128 tokens of their input."""

    chunk_id: int
    part: int
    document_id: int
    category: str
    vector: list[float]


def _best_per_chunk(pairs) -> list[tuple[int, float]]:
    best: dict[int, float] = {}
    for cid, score in pairs:
        if score > best.get(cid, -1.0):
            best[cid] = score
    return sorted(best.items(), key=lambda x: x[1], reverse=True)


class VectorIndex(ABC):
    kind: str

    @abstractmethod
    def add(self, model_id: str, items: list[VectorItem]) -> None: ...

    @abstractmethod
    def delete_document(self, document_id: int) -> None: ...

    @abstractmethod
    def search(self, model_id: str, vector: list[float], k: int, categories: list[str] | None = None) -> list[tuple[int, float]]:
        """[(chunk_id, best passage cosine similarity)], best first, one entry per chunk."""

    @abstractmethod
    def count(self, model_id: str, document_id: int | None = None) -> int: ...


class ChromaIndex(VectorIndex):
    kind = "chroma"

    def __init__(self, path: Path | str, host: str = "", port: int = 8000):
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        settings = ChromaSettings(anonymized_telemetry=False)
        if host:  # a Chroma server, e.g. the "chroma" service in docker-compose.yml
            self.client = chromadb.HttpClient(host=host, port=port, settings=settings)
        else:
            Path(path).mkdir(parents=True, exist_ok=True)
            self.client = chromadb.PersistentClient(path=str(path), settings=settings)

    def _collection(self, model_id: str):
        name = "kb_" + hashlib.sha1(model_id.encode()).hexdigest()[:16]
        return self.client.get_or_create_collection(name, metadata={"hnsw:space": "cosine", "model": model_id})

    def add(self, model_id: str, items: list[VectorItem]) -> None:
        if not items:
            return
        self._collection(model_id).upsert(
            ids=[f"{i.chunk_id}:{i.part}" for i in items],
            embeddings=[i.vector for i in items],
            metadatas=[{"document_id": i.document_id, "chunk_id": i.chunk_id, "category": i.category} for i in items],
        )

    def delete_document(self, document_id: int) -> None:
        for item in self.client.list_collections():  # Collection objects (older Chroma: names)
            name = item if isinstance(item, str) else item.name
            self.client.get_collection(name).delete(where={"document_id": document_id})

    def search(self, model_id, vector, k, categories=None):
        col = self._collection(model_id)
        n = col.count()
        if n == 0:
            return []
        where = {"category": {"$in": categories}} if categories else None
        res = col.query(query_embeddings=[vector], n_results=min(k * 4, n), where=where)
        pairs = ((int(i.split(":")[0]), 1.0 - float(d)) for i, d in zip(res["ids"][0], res["distances"][0], strict=True))
        return _best_per_chunk(pairs)[:k]

    def count(self, model_id, document_id=None):
        col = self._collection(model_id)
        if document_id is None:
            return col.count()
        return len(col.get(where={"document_id": document_id}, include=[])["ids"])


class PgVectorIndex(VectorIndex):
    """Needs the `vector` extension (Supabase: Database > Extensions > vector; created automatically if allowed)."""

    kind = "pgvector"

    def __init__(self, engine: sa.Engine):
        self.engine = engine
        with engine.begin() as conn:
            conn.execute(sa.text("create extension if not exists vector"))
            conn.execute(sa.text("""
                create table if not exists kb_embeddings (
                    chunk_id integer not null references kb_chunks(id) on delete cascade,
                    part integer not null,
                    document_id integer not null,
                    category text not null,
                    model text not null,
                    embedding vector not null,
                    primary key (chunk_id, part)
                )"""))
            conn.execute(sa.text("create index if not exists ix_kb_embeddings_model on kb_embeddings (model, category)"))
            conn.execute(sa.text("alter table kb_embeddings enable row level security"))

    @staticmethod
    def _lit(v: list[float]) -> str:
        return "[" + ",".join(f"{x:.7f}" for x in v) + "]"

    def add(self, model_id, items):
        if not items:
            return
        with self.engine.begin() as conn:
            conn.execute(
                sa.text("""insert into kb_embeddings (chunk_id, part, document_id, category, model, embedding)
                           values (:c, :p, :d, :cat, :m, cast(:v as vector))
                           on conflict (chunk_id, part) do update set model = excluded.model,
                               category = excluded.category, embedding = excluded.embedding"""),
                [{"c": i.chunk_id, "p": i.part, "d": i.document_id, "cat": i.category, "m": model_id,
                  "v": self._lit(i.vector)} for i in items],
            )

    def delete_document(self, document_id):
        with self.engine.begin() as conn:
            conn.execute(sa.text("delete from kb_embeddings where document_id = :d"), {"d": document_id})

    def search(self, model_id, vector, k, categories=None):
        sql = "select chunk_id, 1 - (embedding <=> cast(:v as vector)) as score from kb_embeddings where model = :m"
        params = {"v": self._lit(vector), "m": model_id, "k": k * 4}
        if categories:
            sql += " and category = any(:cats)"
            params["cats"] = list(categories)
        sql += " order by embedding <=> cast(:v as vector) limit :k"
        with self.engine.connect() as conn:
            return _best_per_chunk((r.chunk_id, float(r.score)) for r in conn.execute(sa.text(sql), params))[:k]

    def count(self, model_id, document_id=None):
        sql = "select count(*) from kb_embeddings where model = :m"
        params: dict = {"m": model_id}
        if document_id is not None:
            sql += " and document_id = :d"
            params["d"] = document_id
        with self.engine.connect() as conn:
            return conn.execute(sa.text(sql), params).scalar_one()
