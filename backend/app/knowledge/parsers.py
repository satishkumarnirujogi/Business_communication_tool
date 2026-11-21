"""Turn uploaded files and web pages into plain text sections (with page numbers for PDFs)."""

from __future__ import annotations

import csv
import io
import ipaddress
import re
import socket
from dataclasses import dataclass
from pathlib import PurePath
from urllib.parse import urlparse

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_URL_BYTES = 5 * 1024 * 1024
SUPPORTED = {".pdf", ".docx", ".txt", ".md", ".markdown", ".csv"}


class ParseError(ValueError):
    """Readable reason a document couldn't be read."""


@dataclass
class Section:
    text: str
    page: int | None = None


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ParseError("The file's text encoding couldn't be read. Save it as UTF-8 and upload again.")


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_file(filename: str, data: bytes) -> list[Section]:
    ext = PurePath(filename).suffix.lower()
    if ext not in SUPPORTED:
        raise ParseError(f"'{ext or filename}' files aren't supported. Upload PDF, DOCX, TXT, MD or CSV.")
    if len(data) > MAX_FILE_BYTES:
        raise ParseError(f"The file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB. Split it into smaller files.")
    if not data:
        raise ParseError("The file is empty.")

    if ext == ".pdf":
        sections = _pdf(data)
    elif ext == ".docx":
        sections = _docx(data)
    elif ext == ".csv":
        sections = _csv(_decode(data))
    else:
        sections = [Section(_clean(_decode(data)))]

    sections = [s for s in sections if s.text.strip()]
    if not sections:
        raise ParseError(
            "No text was found in the file. If it is a scanned PDF, export it with text (OCR) and upload again."
        )
    return sections


def _pdf(data: bytes) -> list[Section]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                raise ParseError("The PDF is password-protected. Remove the password and upload again.") from None
        return [Section(_clean(p.extract_text() or ""), page=i) for i, p in enumerate(reader.pages, start=1)]
    except PdfReadError as e:
        raise ParseError(f"The PDF couldn't be read ({e}).") from None


def _docx(data: bytes) -> list[Section]:
    import docx

    try:
        d = docx.Document(io.BytesIO(data))
    except Exception:
        raise ParseError("The Word file couldn't be read. Save it as .docx (not .doc) and upload again.") from None
    parts = []
    for p in d.paragraphs:
        text = p.text.strip()
        if not text:
            continue
        if p.style is not None and p.style.name.lower().startswith("heading"):
            text = f"\n## {text}"
        parts.append(text)
    for table in d.tables:
        rows = [[c.text.strip() for c in r.cells] for r in table.rows]
        if rows:
            parts.append(_rows_to_text(rows))
    return [Section(_clean("\n".join(parts)))]


def _rows_to_text(rows: list[list[str]]) -> str:
    header, body = rows[0], rows[1:]
    if not body:
        return " | ".join(header)
    lines = []
    for r in body:
        cells = [f"{h}: {v}" for h, v in zip(header, r, strict=False) if str(v).strip()]
        if cells:
            lines.append("; ".join(cells))
    return "\n".join(lines)


def _csv(text: str) -> list[Section]:
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        return []
    # One line per row, each value labelled with its column: "Service: Skin check; Price: 75 EUR"
    return [Section(_rows_to_text(rows))]


# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------
def check_url(url: str, allow_private: bool = False) -> str:
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ParseError("Enter a full web address starting with http:// or https://.")
    if not allow_private:
        try:
            infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
        except socket.gaierror:
            raise ParseError(f"The address {u.hostname} couldn't be found. Check the URL.") from None
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise ParseError("Only public web pages can be imported (this address points to a private network).")
    return url.strip()


def fetch_url(url: str, allow_private: bool = False) -> tuple[str, list[Section]]:
    """Returns (page title, sections)."""
    import httpx
    from bs4 import BeautifulSoup

    url = check_url(url, allow_private)
    try:
        with httpx.Client(timeout=15, follow_redirects=True, headers={"User-Agent": "ClinicAssistant/1.0"}) as client, \
                client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    raise ParseError(f"The page answered with error {resp.status_code}.")
                # Redirects must not lead to a private address either.
                check_url(str(resp.url), allow_private)
                body = b""
                for part in resp.iter_bytes():
                    body += part
                    if len(body) > MAX_URL_BYTES:
                        raise ParseError("The page is too large to import.")
                ctype = resp.headers.get("content-type", "")
    except httpx.HTTPError as e:
        raise ParseError(f"The page couldn't be downloaded ({type(e).__name__}).") from None

    if "pdf" in ctype:
        return PurePath(urlparse(url).path).name or url, _pdf(body)
    if "html" not in ctype and "text" not in ctype:
        raise ParseError(f"The address returned '{ctype or 'unknown'}' content, not a web page.")
    soup = BeautifulSoup(body, "html.parser")
    title = (soup.title.string or "").strip() if soup.title else ""
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form", "svg"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    lines = []
    for el in main.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "th", "dt", "dd"]):
        text = el.get_text(" ", strip=True)
        if text:
            lines.append(f"\n## {text}" if el.name in ("h1", "h2", "h3") else text)
    text = _clean("\n".join(lines)) or _clean(main.get_text("\n", strip=True))
    if not text:
        raise ParseError("No readable text was found on that page.")
    return title or url, [Section(text)]
