"""Knowledge base: parsing, chunking, indexing, replacement on re-upload, search and the agent tool.

Uses a deterministic bag-of-words embedder so the tests run offline and fast; the real local
model is exercised by the live check, not here.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import re

import pytest

from app.agent.engine import cited_sources
from app.agent.tools import run_tool
from app.config import get_settings
from app.data import PostgresStore, SQLiteStore, set_store
from app.knowledge import KnowledgeError, set_kb
from app.knowledge.chunker import chunk_sections, count_tokens, passages
from app.knowledge.embeddings import Embedder
from app.knowledge.parsers import ParseError, Section, check_url, parse_file
from app.knowledge.service import KnowledgeBase
from app.knowledge.vectors import ChromaIndex, PgVectorIndex


class HashEmbedder(Embedder):
    model_id = "test:hash-256"

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * 256
        for w in re.findall(r"\w+", text.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 256] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


PRICES = b"Service,Price (EUR)\nSkin check,75\nGeneral consultation,45\nMedical certificate,15\n"
PARKING = b"# FAQ\n\n## Is there parking?\nYes, 20 free spaces behind the building for up to 2 hours.\n"


@pytest.fixture
def kb(tmp_path):
    store = SQLiteStore(tmp_path / "c.db")
    store.init_schema()
    set_store(store)
    kb = KnowledgeBase(store.engine, ChromaIndex(tmp_path / "chroma"), HashEmbedder(), get_settings())
    set_kb(kb)
    yield kb
    set_kb(None)
    set_store(None)


def add(kb, name, data, category):
    doc, unchanged = kb.add_file(name, data, category)
    if not unchanged:
        doc = kb.process(doc["id"])
    return doc


# ---- parsing ---------------------------------------------------------------
def _pdf(pages: list[str]) -> bytes:
    """Minimal valid PDF with one text line per page."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode()}\nendstream")
        content_id = len(objs)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
                    f"/Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n{body}\nendobj\n".encode())
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return out.getvalue()


def test_parse_formats():
    assert "Skin check" in parse_file("p.csv", PRICES)[0].text
    assert "Price (EUR): 75" in parse_file("p.csv", PRICES)[0].text  # rows are labelled with their header
    assert parse_file("f.md", PARKING)[0].text.startswith("# FAQ")
    assert parse_file("f.txt", "Grüße aus Köln".encode("cp1252"))[0].text == "Grüße aus Köln"

    pdf = parse_file("fees.pdf", _pdf(["No-show fee is 40 EUR", "Parking is free"]))
    assert [(s.page, s.text) for s in pdf] == [(1, "No-show fee is 40 EUR"), (2, "Parking is free")]

    import docx

    d = docx.Document()
    d.add_heading("Payment", 1)
    d.add_paragraph("We accept cards and cash.")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text, t.cell(1, 0).text, t.cell(1, 1).text = "Service", "Price", "Skin check", "75"
    buf = io.BytesIO()
    d.save(buf)
    text = parse_file("info.docx", buf.getvalue())[0].text
    assert "## Payment" in text and "cards and cash" in text and "Service: Skin check; Price: 75" in text


@pytest.mark.parametrize("name,data,msg", [
    ("x.exe", b"MZ", "aren't supported"), ("empty.txt", b"", "empty"), ("blank.md", b"   \n\n ", "No text"),
])
def test_parse_errors_are_readable(name, data, msg):
    with pytest.raises(ParseError, match=msg):
        parse_file(name, data)


def test_private_urls_rejected():
    for url in ["http://127.0.0.1/admin", "http://localhost:8010/", "ftp://example.com/x", "not a url"]:
        with pytest.raises(ParseError):
            check_url(url)


# ---- chunking --------------------------------------------------------------
def test_chunking_size_overlap_and_pages():
    para = "The clinic opens at nine and closes at five on weekdays. " * 30  # ~400 tokens
    sections = [Section(para, page=1), Section(para + "\n\nUNIQUE-MARKER on page two.", page=2), Section(para, page=3)]
    chunks = chunk_sections(sections)
    assert len(chunks) >= 2
    assert all(c.tokens <= 800 + 150 for c in chunks)
    # each chunk is labelled with the page its new text starts on
    pages = [c.page for c in chunks]
    assert pages[0] == 1 and pages == sorted(pages) and pages[-1] == 3
    # overlap: each later chunk starts with the last words of the previous one
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        carry = nxt.text.split("\n\n")[0]
        assert " ".join(prev.text.split()).endswith(carry) and 0 < count_tokens(carry) <= 130
    assert all(count_tokens(p) <= 130 for c in chunks for p in passages(c.text))


# ---- knowledge base --------------------------------------------------------
def test_index_search_and_sources(kb):
    add(kb, "price-list.csv", PRICES, "prices")
    add(kb, "faq.md", PARKING, "faq")
    r = kb.search("how much is a skin check")
    assert r.found and r.hits[0].source == "price-list.csv" and "75" in r.hits[0].text
    r = kb.search("is there parking", categories=["faq"])
    assert r.found and {h.source for h in r.hits} == {"faq.md"}
    assert not kb.search("wifi password").found


def test_reupload_replaces_old_chunks(kb):
    doc = add(kb, "price-list.csv", PRICES, "prices")
    assert add(kb, "price-list.csv", PRICES, "prices")["id"] == doc["id"]          # identical -> unchanged
    _, unchanged = kb.add_file("Price-List.csv", PRICES, "prices")
    assert unchanged

    new = add(kb, "price-list.csv", b"Service,Price (EUR)\nSkin check,99\n", "prices")
    assert new["id"] == doc["id"] and new["status"] == "ready"
    hits = kb.search("skin check price").hits
    assert hits and "99" in hits[0].text and all("75" not in h.text for h in hits)
    assert kb.index.count(kb.embedder.model_id, doc["id"]) == len(passages(hits[0].text))


def test_failed_document_is_reported(kb):
    doc, _ = kb.add_file("scan.pdf", b"%PDF-1.4 garbage", "policy")
    doc = kb.process(doc["id"])
    assert doc["status"] == "failed" and doc["error"]


def test_delete_and_bad_category(kb):
    doc = add(kb, "faq.md", PARKING, "faq")
    assert kb.delete(doc["id"]) and not kb.search("parking").found
    assert kb.index.count(kb.embedder.model_id) == 0
    with pytest.raises(KnowledgeError, match="Category"):
        kb.add_file("x.md", b"hi", "gossip")


def test_reindex_needed_after_model_change(kb):
    add(kb, "faq.md", PARKING, "faq")
    assert kb.reindex_needed() == []
    kb.embedder.model_id = "test:other-model"
    assert len(kb.reindex_needed()) == 1
    kb.reindex(kb.reindex_needed())
    assert kb.search("parking").found


# ---- agent tool ------------------------------------------------------------
def test_search_tool_payload_and_citations(kb):
    add(kb, "price-list.csv", PRICES, "prices")
    payload, err = run_tool("search_clinic_knowledge", {"query": "skin check price"}, "s")
    assert not err and payload["found"] and payload["results"][0]["source"] == "price-list.csv"
    assert "Source" in payload["instructions"]

    payload, err = run_tool("search_clinic_knowledge", {"query": "acupuncture sessions"}, "s")
    assert not err and not payload["found"] and "escalate_to_human" in payload["instructions"]

    state = {"sources": {"price-list.csv": None, "faq.md": None}}
    assert cited_sources("A skin check costs 75 EUR (Source: price-list.csv).", state) == ["price-list.csv"]


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="needs TEST_DATABASE_URL (Postgres with pgvector)")
def test_pgvector_index():
    store = PostgresStore(os.environ["TEST_DATABASE_URL"])
    store.init_schema()
    kb = KnowledgeBase(store.engine, PgVectorIndex(store.engine), HashEmbedder(), get_settings())
    try:
        doc = add(kb, "test-prices.csv", PRICES, "prices")
        r = kb.search("skin check price")
        assert r.found and r.hits[0].source == "test-prices.csv"
        assert kb.index.count(kb.embedder.model_id, doc["id"]) > 0
    finally:
        kb.delete(doc["id"])
        store.close()
