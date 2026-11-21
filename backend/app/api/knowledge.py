"""Admin API for the clinic knowledge base (the admin dashboard in Phase 6 uses these)."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from ..data.models import KB_CATEGORIES
from ..knowledge import KnowledgeError, get_kb
from ..knowledge.parsers import MAX_FILE_BYTES
from .deps import require_admin, require_staff

router = APIRouter(prefix="/api/admin/knowledge", dependencies=[Depends(require_staff)], tags=["knowledge"])
ADMIN = [Depends(require_admin)]  # changing the knowledge base is for admins; staff can read and test it


def _kb():
    try:
        return get_kb()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"The knowledge base isn't available: {e}") from None


@router.get("")
def list_documents():
    return {"categories": list(KB_CATEGORIES), "documents": _kb().list_documents()}


@router.post("", status_code=202, dependencies=ADMIN)
async def upload(background: BackgroundTasks, file: UploadFile = File(...), category: str = Form(...)):
    """Upload PDF, DOCX, TXT, MD or CSV. A file with the same name replaces the old one."""
    data = await file.read(MAX_FILE_BYTES + 1)
    kb = _kb()
    try:
        doc, unchanged = kb.add_file(file.filename or "", data, category)
    except KnowledgeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    if not unchanged:
        background.add_task(kb.process, doc["id"])
    return {**doc, "unchanged": unchanged}


class UrlIn(BaseModel):
    url: str = Field(..., max_length=2000)
    category: str


@router.post("/url", status_code=202, dependencies=ADMIN)
def add_url(body: UrlIn, background: BackgroundTasks):
    kb = _kb()
    try:
        doc = kb.add_url(body.url, body.category)
    except KnowledgeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    background.add_task(kb.process, doc["id"])
    return doc


@router.get("/{doc_id}")
def get_document(doc_id: int):
    doc = _kb().get(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    return _kb()._public(doc)


@router.delete("/{doc_id}", dependencies=ADMIN)
def delete_document(doc_id: int):
    if not _kb().delete(doc_id):
        raise HTTPException(status_code=404, detail="Document not found.")
    return {"deleted": doc_id}


class SearchIn(BaseModel):
    query: str = Field(..., max_length=500)
    category: str | None = None


@router.post("/search")
def search(body: SearchIn):
    """Try a question and see which passages the assistant would get (with scores)."""
    r = _kb().search(body.query, [body.category] if body.category else None)
    return {
        "found": r.found,
        "hits": [{"source": h.source, "page": h.page, "category": h.category, "score": round(h.score, 4),
                  "similarity": None if h.vector_score is None else round(h.vector_score, 3),
                  "keyword_coverage": round(h.keyword_coverage, 3), "text": h.text} for h in r.hits],
    }


@router.post("/reindex", status_code=202, dependencies=ADMIN)
def reindex(background: BackgroundTasks):
    """Re-embed every document (needed after changing EMBEDDING_PROVIDER / EMBEDDING_MODEL; done automatically at startup)."""
    kb = _kb()
    background.add_task(kb.reindex)
    return {"queued": len(kb.list_documents())}
