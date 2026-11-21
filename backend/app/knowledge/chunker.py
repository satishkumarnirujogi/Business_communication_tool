"""Split text into ~800-token chunks with overlap, keeping paragraphs (and PDF pages) together."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .parsers import Section

CHUNK_TOKENS = 800
OVERLAP_TOKENS = 120


@lru_cache(maxsize=1)
def _encoding():
    # LiteLLM ships tiktoken's cl100k encoding, so this works offline.
    import tiktoken

    from ..llm import provider  # noqa: F401  (sets LiteLLM env before tiktoken loads)

    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def count_tokens(text: str) -> int:
    enc = _encoding()
    return len(enc.encode(text)) if enc else max(1, round(len(text.split()) * 1.3))


@dataclass
class Chunk:
    text: str
    tokens: int
    page: int | None = None


_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _pieces(text: str, limit: int = CHUNK_TOKENS) -> list[str]:
    """Paragraphs; paragraphs over the limit are split further."""
    out = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if count_tokens(para) <= limit:
            out.append(para)
            continue
        # Too long: lines (tables, CSV rows), then sentences, then a hard split by words.
        for line in para.split("\n"):
            for sent in ([line] if count_tokens(line) <= limit else _SENTENCE.split(line)):
                if count_tokens(sent) <= limit:
                    out.append(sent)
                else:
                    words = sent.split()
                    step = max(20, int(limit / 1.5))
                    out.extend(" ".join(words[i:i + step]) for i in range(0, len(words), step))
    return out


def _tail(text: str, tokens: int) -> str:
    """Last ~`tokens` tokens of text, starting at a word boundary."""
    words = text.split()
    tail: list[str] = []
    for w in reversed(words):
        tail.insert(0, w)
        if count_tokens(" ".join(tail)) >= tokens:
            break
    return " ".join(tail)


def chunk_sections(sections: list[Section], chunk_tokens: int = CHUNK_TOKENS, overlap: int = OVERLAP_TOKENS) -> list[Chunk]:
    """Greedy packing of paragraphs. Each new chunk starts with the last ~`overlap` tokens of the
    previous one, so a fact split across a boundary is still found. `page` = page of the chunk's first new text."""
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_tokens = 0
    page: int | None = None
    only_carry = True  # buf holds nothing but the overlap from the previous chunk

    for section in sections:
        for piece in _pieces(section.text, chunk_tokens):
            t = count_tokens(piece)
            if buf and not only_carry and buf_tokens + t > chunk_tokens:
                text = "\n\n".join(buf)
                chunks.append(Chunk(text, count_tokens(text), page))
                carry = _tail(text, overlap) if overlap else ""
                buf, buf_tokens = ([carry], count_tokens(carry)) if carry else ([], 0)
                page, only_carry = None, True
            if page is None:
                page = section.page
            buf.append(piece)
            buf_tokens += t
            only_carry = False
    if buf and not only_carry:
        text = "\n\n".join(buf)
        chunks.append(Chunk(text, count_tokens(text), page))
    return chunks


PASSAGE_TOKENS = 100  # small local embedding models only read ~128 tokens


def passages(chunk_text: str) -> list[str]:
    """Short overlapping passages of one chunk, for embedding."""
    return [c.text for c in chunk_sections([Section(chunk_text)], PASSAGE_TOKENS, 20)] or [chunk_text]
