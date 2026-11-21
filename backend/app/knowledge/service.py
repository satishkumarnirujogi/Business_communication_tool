"""Clinic knowledge base: document registry, processing pipeline and hybrid search.

upload -> parse -> chunk (~800 tokens) -> embed -> kb_chunks (text) + vector index (vectors)
search -> vector similarity + keyword (BM25) ranking, fused; each hit carries its source file name.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePath

import sqlalchemy as sa

from ..config import ROOT, Settings, get_settings
from ..data.models import KB_CATEGORIES, kb_chunks, kb_documents
from .chunker import chunk_sections, passages
from .embeddings import Embedder, EmbeddingError, build_embedder
from .parsers import MAX_FILE_BYTES, SUPPORTED, ParseError, Section, check_url, fetch_url, parse_file
from .vectors import ChromaIndex, PgVectorIndex, VectorIndex, VectorItem

log = logging.getLogger("clinic.knowledge")

UPLOADS = ROOT / "data" / "uploads"
RRF_K = 60
MIN_KEYWORD_COVERAGE = 0.45  # share of the (idf-weighted) query words a passage must contain

STOPWORDS = set(["a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from", "has", "have", "how", "i", "in", "is", "it", "its", "me", "my", "of", "on", "or", "our", "please", "so", "that", "the", "this", "to", "was", "we", "what", "when", "where", "which", "who", "why", "will", "with", "you", "your", "yes", "no", "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem", "einer", "und", "oder", "ist", "sind", "bei", "mit", "von", "zu", "zur", "zum", "im", "in", "am", "an", "auf", "für", "fur", "wie", "was", "wann", "wo", "wer", "ich", "sie", "es", "wir", "ihr", "mein", "meine", "ihre", "ihr", "bitte", "kann", "können", "gibt", "haben", "hat", "nicht", "noch", "auch", "nur"])


class KnowledgeError(ValueError):
    """Readable problem for the admin (bad category, unsupported file...)."""


def _now() -> datetime:
    return datetime.now(UTC)


_SUFFIXES = ("ungen", "ung", "ing", "en", "es", "er", "e", "s", "n")


def _stem(t: str) -> str:
    """Very light English/German suffix stripping: dogs->dog, Impfungen->impf, parking->park."""
    for suf in _SUFFIXES:
        if t.endswith(suf) and len(t) - len(suf) >= 3:
            return t[: -len(suf)]
    return t


def _terms(text: str) -> list[str]:
    return [_stem(t) for t in re.findall(r"\w+", text.lower()) if len(t) > 1 and t not in STOPWORDS]


@dataclass
class Hit:
    chunk_id: int
    document_id: int
    source: str
    category: str
    page: int | None
    text: str
    vector_score: float | None = None
    keyword_score: float = 0.0
    keyword_coverage: float = 0.0
    score: float = 0.0


@dataclass
class SearchResult:
    query: str
    hits: list[Hit] = field(default_factory=list)
    found: bool = False


class _Lexical:
    """In-memory BM25 over all chunks. Clinic knowledge bases are small (hundreds of chunks)."""

    def __init__(self, rows: list[dict]):
        self.rows = {r["id"]: r for r in rows}
        self.tf = {r["id"]: Counter(_terms(r["text"])) for r in rows}
        self.len = {cid: sum(c.values()) or 1 for cid, c in self.tf.items()}
        self.avg = (sum(self.len.values()) / len(self.len)) if self.len else 1
        df: Counter = Counter()
        for c in self.tf.values():
            df.update(c.keys())
        n = len(self.tf)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, k: int, categories: list[str] | None) -> list[tuple[int, float, float]]:
        q = list(dict.fromkeys(_terms(query)))
        if not q:
            return []
        max_idf = max(self.idf.values(), default=1.0) + 1.0  # words found nowhere weigh the most
        weights = {t: self.idf.get(t, max_idf) for t in q}
        total = sum(weights.values())
        out = []
        for cid, tf in self.tf.items():
            if categories and self.rows[cid]["category"] not in categories:
                continue
            score, covered = 0.0, 0.0
            for t in q:
                f = tf.get(t, 0)
                if f:
                    covered += weights[t]
                    score += weights[t] * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * self.len[cid] / self.avg))
            if score > 0:
                out.append((cid, score, covered / total))
        out.sort(key=lambda x: x[1], reverse=True)
        return out[:k]


class KnowledgeBase:
    def __init__(self, engine: sa.Engine, index: VectorIndex, embedder: Embedder, settings: Settings | None = None):
        self.engine = engine
        self.index = index
        self.embedder = embedder
        self.settings = settings or get_settings()
        self._lexical: _Lexical | None = None
        self._lex_lock = threading.Lock()
        self._process_lock = threading.Lock()

    # ---- registry --------------------------------------------------------------
    def list_documents(self) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(kb_documents).order_by(kb_documents.c.category, kb_documents.c.name)).mappings()
            return [self._public(dict(r)) for r in rows]

    def get(self, doc_id: int) -> dict | None:
        with self.engine.connect() as conn:
            r = conn.execute(sa.select(kb_documents).where(kb_documents.c.id == doc_id)).mappings().first()
            return dict(r) if r else None

    @staticmethod
    def _public(d: dict) -> dict:
        return {k: d[k] for k in ("id", "name", "category", "source_type", "url", "status", "error", "chunk_count",
                                  "size_bytes", "created_at", "updated_at")}

    @staticmethod
    def _check_category(category: str) -> str:
        c = (category or "").strip().lower()
        if c not in KB_CATEGORIES:
            raise KnowledgeError(f"Category must be one of: {', '.join(KB_CATEGORIES)}.")
        return c

    def _register(self, *, name: str, category: str, source_type: str, url: str | None, sha: str | None,
                  size: int | None) -> tuple[dict, bool]:
        """Create or reset the row for this name. Returns (row, unchanged)."""
        key = name.strip().lower()
        with self.engine.begin() as conn:
            old = conn.execute(sa.select(kb_documents).where(kb_documents.c.name_key == key)).mappings().first()
            if old and source_type == "file" and old["sha256"] == sha and old["status"] == "ready" \
                    and old["category"] == category and old["embedding_model"] == self.embedder.model_id:
                return dict(old), True
            values = dict(name=name, category=category, source_type=source_type, url=url, sha256=sha, size_bytes=size,
                          status="processing", error=None, updated_at=_now())
            if old:
                conn.execute(sa.update(kb_documents).where(kb_documents.c.id == old["id"]).values(**values))
                doc_id = old["id"]
            else:
                doc_id = conn.execute(sa.insert(kb_documents).values(name_key=key, **values).returning(kb_documents.c.id)).scalar_one()
            return dict(conn.execute(sa.select(kb_documents).where(kb_documents.c.id == doc_id)).mappings().one()), False

    def add_file(self, filename: str, data: bytes, category: str) -> tuple[dict, bool]:
        """Register an upload; call process(id) afterwards (in the background). Returns (doc, unchanged)."""
        category = self._check_category(category)
        name = PurePath(filename or "").name.strip()
        if not name:
            raise KnowledgeError("The file needs a name.")
        # Full parsing happens in process(), in the background; only cheap checks here.
        ext = PurePath(name).suffix.lower()
        if ext not in SUPPORTED:
            raise KnowledgeError(f"'{ext or name}' files aren't supported. Upload PDF, DOCX, TXT, MD or CSV.")
        if len(data) > MAX_FILE_BYTES:
            raise KnowledgeError(f"The file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
        sha = hashlib.sha256(data).hexdigest()
        doc, unchanged = self._register(name=name, category=category, source_type="file", url=None, sha=sha, size=len(data))
        if not unchanged:
            folder = UPLOADS / str(doc["id"])
            folder.mkdir(parents=True, exist_ok=True)
            for old in folder.iterdir():
                old.unlink()
            path = folder / re.sub(r"[^\w.\- ]", "_", name)
            path.write_bytes(data)
            with self.engine.begin() as conn:
                conn.execute(sa.update(kb_documents).where(kb_documents.c.id == doc["id"]).values(stored_path=str(path)))
        return self._public(doc), unchanged

    def add_url(self, url: str, category: str) -> dict:
        category = self._check_category(category)
        try:
            url = check_url(url, self.settings.kb_allow_private_urls)
        except ParseError as e:
            raise KnowledgeError(str(e)) from None
        doc, _ = self._register(name=url, category=category, source_type="url", url=url, sha=None, size=None)
        return self._public(doc)

    def delete(self, doc_id: int) -> bool:
        doc = self.get(doc_id)
        if not doc:
            return False
        self.index.delete_document(doc_id)
        with self.engine.begin() as conn:
            conn.execute(sa.delete(kb_chunks).where(kb_chunks.c.document_id == doc_id))
            conn.execute(sa.delete(kb_documents).where(kb_documents.c.id == doc_id))
        folder = UPLOADS / str(doc_id)
        if folder.is_dir():
            for f in folder.iterdir():
                f.unlink()
            folder.rmdir()
        self._invalidate()
        return True

    # ---- processing ----------------------------------------------------------
    def _sections(self, doc: dict) -> list[Section]:
        if doc["source_type"] == "url":
            _title, sections = fetch_url(doc["url"], self.settings.kb_allow_private_urls)
            return sections
        path = Path(doc["stored_path"] or "")
        if not path.is_file():
            raise ParseError("The uploaded file is missing on the server. Upload it again.")
        return parse_file(doc["name"], path.read_bytes())

    def process(self, doc_id: int) -> dict:
        """Parse, chunk, embed and index one document. Never raises; failures are stored on the row."""
        with self._process_lock:  # one at a time keeps memory and CPU predictable on small servers
            doc = self.get(doc_id)
            if not doc:
                return {}
            try:
                chunks = chunk_sections(self._sections(doc))
                if not chunks:
                    raise ParseError("No text was found in the document.")
                parts = [(i, n, text) for i, c in enumerate(chunks) for n, text in enumerate(passages(c.text))]
                vectors = self.embedder.embed_documents([t for _, _, t in parts])
                with self.engine.begin() as conn:  # replaces the old chunks of this document
                    conn.execute(sa.delete(kb_chunks).where(kb_chunks.c.document_id == doc_id))
                    ids = [
                        conn.execute(sa.insert(kb_chunks).values(document_id=doc_id, idx=i, text=c.text, page=c.page,
                                                                 tokens=c.tokens).returning(kb_chunks.c.id)).scalar_one()
                        for i, c in enumerate(chunks)
                    ]
                self.index.delete_document(doc_id)
                self.index.add(self.embedder.model_id, [
                    VectorItem(ids[i], n, doc_id, doc["category"], v) for (i, n, _), v in zip(parts, vectors, strict=True)
                ])
                self._set(doc_id, status="ready", error=None, chunk_count=len(chunks), embedding_model=self.embedder.model_id)
                log.info("knowledge: indexed %s (%d chunks)", doc["name"], len(chunks))
            except (ParseError, EmbeddingError) as e:
                self._set(doc_id, status="failed", error=str(e))
            except Exception as e:
                log.exception("knowledge: processing %s failed", doc["name"])
                self._set(doc_id, status="failed", error=f"Processing failed unexpectedly ({type(e).__name__}). See the server log.")
            finally:
                self._invalidate()
            return self._public(self.get(doc_id) or doc)

    def _set(self, doc_id: int, **values) -> None:
        with self.engine.begin() as conn:
            conn.execute(sa.update(kb_documents).where(kb_documents.c.id == doc_id).values(updated_at=_now(), **values))

    def reindex_needed(self) -> list[int]:
        """Docs left 'processing' by a restart, or indexed with a different embedding model."""
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(kb_documents.c.id, kb_documents.c.status, kb_documents.c.embedding_model)).all()
        return [r.id for r in rows if r.status == "processing" or (r.status == "ready" and r.embedding_model != self.embedder.model_id)]

    def reindex(self, doc_ids: list[int] | None = None) -> list[dict]:
        ids = doc_ids if doc_ids is not None else [d["id"] for d in self.list_documents()]
        return [self.process(i) for i in ids]

    # ---- search ----------------------------------------------------------------
    def _invalidate(self) -> None:
        with self._lex_lock:
            self._lexical = None

    def _lex(self) -> _Lexical:
        with self._lex_lock:
            if self._lexical is None:
                with self.engine.connect() as conn:
                    rows = conn.execute(
                        sa.select(kb_chunks.c.id, kb_chunks.c.text, kb_chunks.c.page, kb_chunks.c.document_id,
                                  kb_documents.c.name, kb_documents.c.category)
                        .join(kb_documents, kb_documents.c.id == kb_chunks.c.document_id)
                        .where(kb_documents.c.status == "ready")
                    ).mappings()
                    self._lexical = _Lexical([dict(r) for r in rows])
            return self._lexical

    def search(self, query: str, categories: list[str] | None = None, k: int = 4) -> SearchResult:
        query = (query or "").strip()[:500]
        result = SearchResult(query=query)
        if not query:
            return result
        cats = [c for c in (categories or []) if c in KB_CATEGORIES] or None
        lex = self._lex()
        if not lex.rows:
            return result

        vec_hits: list[tuple[int, float]] = []
        try:
            vec_hits = [(cid, s) for cid, s in self.index.search(self.embedder.model_id, self.embedder.embed_query(query), 20, cats)
                        if cid in lex.rows]
        except Exception as e:  # keyword search still works if the embedder is unavailable
            log.warning("knowledge: vector search unavailable: %s", e)
        kw_hits = lex.search(query, 20, cats)

        hits: dict[int, Hit] = {}

        def hit(cid: int) -> Hit:
            if cid not in hits:
                r = lex.rows[cid]
                hits[cid] = Hit(cid, r["document_id"], r["name"], r["category"], r["page"], r["text"])
            return hits[cid]

        for rank, (cid, s) in enumerate(vec_hits):
            h = hit(cid)
            h.vector_score = s
            h.score += 1 / (RRF_K + rank)
        for rank, (cid, s, cov) in enumerate(kw_hits):
            h = hit(cid)
            h.keyword_score, h.keyword_coverage = s, cov
            h.score += 1 / (RRF_K + rank)

        ranked = sorted(hits.values(), key=lambda h: h.score, reverse=True)
        min_vec = self.settings.kb_min_score
        relevant = [h for h in ranked if (h.vector_score or 0) >= min_vec or h.keyword_coverage >= MIN_KEYWORD_COVERAGE]
        result.hits = relevant[:k]
        result.found = bool(result.hits)
        return result


# --------------------------------------------------------------------------
_kb: KnowledgeBase | None = None
_kb_lock = threading.Lock()


def build_index(settings: Settings, engine: sa.Engine, store_kind: str) -> VectorIndex:
    kind = settings.kb_vector_store
    if kind == "auto":
        kind = "pgvector" if store_kind == "postgres" else "chroma"
    if kind == "pgvector":
        if store_kind != "postgres":
            raise KnowledgeError("KB_VECTOR_STORE=pgvector needs DATA_SOURCE=postgres.")
        return PgVectorIndex(engine)
    return ChromaIndex(settings.chroma_path, settings.chroma_host, settings.chroma_port)


def get_kb() -> KnowledgeBase:
    global _kb
    with _kb_lock:
        if _kb is None:
            from ..data import get_store

            s = get_settings()
            store = get_store()
            _kb = KnowledgeBase(store.engine, build_index(s, store.engine, store.kind), build_embedder(s), s)
        return _kb


def set_kb(kb: KnowledgeBase | None) -> None:
    global _kb
    with _kb_lock:
        _kb = kb
