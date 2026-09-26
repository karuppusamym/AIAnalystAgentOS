"""Uploaded documents (Markdown, plain text, PDF) as knowledge drafts (P4-K06, spec v3 §6.3 "Documents").

An upload becomes `documents/<name>.md` (split into `documents/<name>.part-N.md` when large), a
`type: Document` OKF draft, untrusted until a person reviews it. The text is data, never instructions:

* limits: 10 MiB per upload; `.md`, `.markdown`, `.txt`, `.pdf` only, checked by extension *and*
  content (UTF-8 text; a PDF must start with `%PDF-` and must not be encrypted); PDF decompression is
  capped at 32 MiB and 500 pages;
* an uploaded Markdown file's own frontmatter is dropped (it could claim `verified: human:...`);
* credentials, e-mail addresses and card-like numbers are redacted (`llm/redaction.py`); lines that
  read like instructions to a model are replaced by a marker; control characters are removed;
* links into the bundle are reduced to their text (they would dangle in this pack); external links stay.

PDF text: `pypdf` when installed (the optional `documents` extra); otherwise a built-in extractor for
PDFs whose text uses simple fonts (literal/hex strings under `Tj`/`TJ`/`'`/`"`, Flate or no compression,
one content stream per page). It does not decode CID/Type0 fonts; such a PDF yields no text and the
upload is refused with a message naming the extra.
"""
from __future__ import annotations

import hashlib
import re
import zlib
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from analystos.core.errors import InvalidInput
from analystos.knowledge import okf
from analystos.knowledge.crawl_docs import safe_segment

DOCUMENT_ACTOR = "process:analystos-document-ingest"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_PDF_INFLATED = 32 * 1024 * 1024
MAX_PDF_PAGES = 500
PART_BYTES = 180 * 1024
EXTENSIONS = {".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain", ".pdf": "application/pdf"}
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁠-⁤﻿]")
_INLINE_LINK = re.compile(r"(!?)\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
_REF_DEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*\S+.*$")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


@dataclass
class Extracted:
    media_type: str
    text: str
    pages: list[str] = field(default_factory=list)
    extractor: str = "text"


# ------------------------------------------------------------------------------------ extraction
def extract(filename: str, data: bytes) -> Extracted:
    """Text of an upload, after the type and size checks. InvalidInput on anything else."""
    ext = PurePosixPath(filename.lower()).suffix
    if ext not in EXTENSIONS:
        raise InvalidInput(f"document type {ext or '(none)'} is not accepted; upload {', '.join(sorted(EXTENSIONS))}")
    if len(data) > MAX_UPLOAD_BYTES:
        raise InvalidInput(f"document is {len(data)} bytes; the limit is {MAX_UPLOAD_BYTES}")
    if not data:
        raise InvalidInput("document is empty")
    if ext == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise InvalidInput("not a PDF (no %PDF- header)")
        pages, extractor = pdf_pages(data)
        pages = [p for p in (_clean_ws(p) for p in pages)]
        if not any(pages):
            raise InvalidInput("the PDF has no extractable text (scanned, or fonts the built-in extractor cannot decode; "
                               "install the `documents` extra for pypdf)")
        return Extracted(media_type="application/pdf", text="\n\n".join(pages), pages=pages, extractor=extractor)
    if data.startswith(b"%PDF-") or b"\x00" in data[:8192]:
        raise InvalidInput("binary content under a text extension")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidInput("text documents must be UTF-8") from None
    return Extracted(media_type=EXTENSIONS[ext], text=text.lstrip("﻿"))


def _clean_ws(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip()


def pdf_pages(data: bytes) -> tuple[list[str], str]:
    if b"/Encrypt" in data:
        raise InvalidInput("encrypted PDFs are not accepted")
    try:
        from pypdf import PdfReader  # optional extra
    except ImportError:
        return _builtin_pdf_pages(data), "builtin"
    import io

    reader = PdfReader(io.BytesIO(data))
    if len(reader.pages) > MAX_PDF_PAGES:
        raise InvalidInput(f"PDF has {len(reader.pages)} pages; the limit is {MAX_PDF_PAGES}")
    return [(p.extract_text() or "") for p in reader.pages], "pypdf"


_STREAM = re.compile(rb"<<(.*?)>>\s*stream\r?\n", re.S)


def _inflate(raw: bytes, budget: list[int]) -> bytes:
    d = zlib.decompressobj()
    out = d.decompress(raw, budget[0] + 1)
    if len(out) > budget[0]:
        raise InvalidInput(f"PDF decompresses to more than {MAX_PDF_INFLATED} bytes")
    budget[0] -= len(out)
    return out


def _builtin_pdf_pages(data: bytes) -> list[str]:
    pages: list[str] = []
    budget = [MAX_PDF_INFLATED]
    pos = 0
    while True:
        m = _STREAM.search(data, pos)
        if m is None:
            break
        head = m.group(1)
        start = m.end()
        end = data.find(b"endstream", start)
        if end < 0:
            break
        pos = end + 9
        raw = data[start:end].rstrip(b"\r\n")
        if b"/Subtype" in head or b"/Length1" in head or b"/Image" in head:
            continue  # images and embedded font programs
        if b"/Filter" in head:
            if b"/FlateDecode" not in head or re.search(rb"/Filter\s*\[[^\]]*/\w+[^\]]*/\w+", head):
                continue  # other or chained filters: not text we can read
            try:
                raw = _inflate(raw, budget)
            except zlib.error:
                continue
        if b"BT" not in raw or not re.search(rb"T[jJ]|'|\"", raw):
            continue
        pages.append(_content_text(raw))
        if len(pages) > MAX_PDF_PAGES:
            raise InvalidInput(f"PDF has more than {MAX_PDF_PAGES} pages")
    return pages


_TOKEN = re.compile(rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>|\[|\]|-?\d*\.?\d+|/[^\s/<>\[\]()]+|[A-Za-z'\"*]+", re.S)


def _pdf_string(tok: bytes) -> str:
    if tok.startswith(b"<"):
        hexs = re.sub(rb"\s+", b"", tok[1:-1])
        if len(hexs) % 2:
            hexs += b"0"
        raw = bytes.fromhex(hexs.decode())
    else:
        body, out, i = tok[1:-1], bytearray(), 0
        esc = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\"}
        while i < len(body):
            ch = body[i:i + 1]
            if ch == b"\\" and i + 1 < len(body):
                nxt = body[i + 1:i + 2]
                octal = re.match(rb"[0-7]{1,3}", body[i + 1:i + 4])
                if octal:
                    out.append(int(octal.group(0), 8) & 0xFF)
                    i += 1 + len(octal.group(0))
                    continue
                out += esc.get(nxt, b"" if nxt in (b"\n", b"\r") else nxt)
                i += 2
                continue
            out += ch
            i += 1
        raw = bytes(out)
    return raw.decode("cp1252", errors="replace")


def _content_text(stream: bytes) -> str:
    """Text shown by one content stream: strings of Tj/TJ/'/\", with line breaks on moves."""
    out: list[str] = []
    operands: list[bytes] = []
    in_array: list[bytes] | None = None
    for tok in _TOKEN.findall(stream):
        if tok == b"[":
            in_array = []
            continue
        if tok == b"]":
            operands.append(b"\x00ARRAY")
            operands.append(b"".join(in_array or []))
            in_array = None
            continue
        if in_array is not None:
            if tok.startswith((b"(", b"<")) and not tok.startswith(b"<<"):
                in_array.append(_pdf_string(tok).encode("utf-8", "replace"))
            elif re.fullmatch(rb"-?\d*\.?\d+", tok) and float(tok) < -200:
                in_array.append(b" ")
            continue
        if tok in (b"Tj", b"'", b'"'):
            strs = [o for o in operands if o.startswith((b"(", b"<"))]
            if tok != b"Tj":
                out.append("\n")
            if strs:
                out.append(_pdf_string(strs[-1]))
            operands = []
        elif tok == b"TJ":
            if b"\x00ARRAY" in operands:
                out.append(operands[operands.index(b"\x00ARRAY") + 1].decode("utf-8", "replace"))
            operands = []
        elif tok in (b"Td", b"TD", b"T*", b"Tm", b"ET"):
            if out and not out[-1].endswith("\n"):
                out.append("\n")
            operands = []
        elif tok[:1].isalpha() and not tok.startswith(b"/"):
            operands = []
        else:
            operands.append(tok)
    return "".join(out)


# ------------------------------------------------------------------------------------ screening
@dataclass
class Screening:
    redactions: int = 0
    instruction_lines: int = 0
    links_neutralized: int = 0


_REMOVED = "[removed: text that reads like an instruction to a model]"


def _drop_spread_instructions(lines: list[str], s: Screening, flagged: Any) -> list[str]:
    """An instruction split over several lines ("ignore\\nall\\nprevious\\ninstructions") passes the
    per-line test; a paragraph (lines between blank lines) that reads as one is removed whole."""
    out: list[str] = []
    block: list[str] = []
    for line in [*lines, ""]:
        if line.strip() and line != _REMOVED:
            block.append(line)
            continue
        if len(block) > 1 and flagged(" ".join(block)):
            s.instruction_lines += len(block)
            out.append(_REMOVED)
        else:
            out.extend(block)
        block = []
        out.append(line)
    return out[:-1]


def screen(text: str) -> tuple[str, Screening]:
    from analystos.llm.redaction import redact
    from analystos.skills.catalog import has_injection

    s = Screening()
    text = _CTRL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    redacted = redact(text)
    s.redactions = redacted.count("[REDACTED")
    lines = []
    for line in redacted.split("\n"):
        if has_injection(line):
            s.instruction_lines += 1
            lines.append(_REMOVED)
        elif _REF_DEF.match(line) and not _SCHEME.match(line.split("]:", 1)[1].strip()):
            s.links_neutralized += 1
        else:
            lines.append(line)
    lines = _drop_spread_instructions(lines, s, has_injection)

    def link(m: re.Match) -> str:
        target = m.group(3)
        if _SCHEME.match(target) or target.startswith("//") or target.startswith("#"):
            return m.group(0)
        s.links_neutralized += 1
        return m.group(2)

    return _INLINE_LINK.sub(link, "\n".join(lines)), s


# ------------------------------------------------------------------------------------ documents
def _markdown_body(text: str, title: str) -> str:
    _, body = okf.split_frontmatter(text)  # their frontmatter is dropped, never trusted
    body = body.strip()
    has_top = any(not code and line.startswith("# ") for line, code in okf._without_code(body.split("\n")))
    return body if has_top else f"# {title}\n\n{body}"


def _title(filename: str, text: str, media_type: str) -> str:
    if media_type == "text/markdown":
        _, body = okf.split_frontmatter(text)
        for line in body.split("\n"):
            if line.startswith("# ") and line[2:].strip():
                return line[2:].strip()[:200]
    return PurePosixPath(filename).stem.replace("_", " ").strip()[:200] or "Document"


def _parts(body: str, limit: int = PART_BYTES) -> list[str]:
    """Split at top-level headings (then at lines) into chunks of at most `limit` bytes."""
    sections: list[list[str]] = [[]]
    for line, code in okf._without_code(body.split("\n")):
        if not code and line.startswith("# ") and sections[-1]:
            sections.append([])
        sections[-1].append(line)
    chunks: list[str] = []
    current = ""
    for sec in ("\n".join(x) for x in sections):
        pieces = [sec]
        if len(sec.encode()) > limit:
            pieces, buf = [], ""
            for line in sec.split("\n"):
                if buf and len((buf + "\n" + line).encode()) > limit:
                    pieces.append(buf)
                    buf = ""
                buf = f"{buf}\n{line}" if buf else line[: limit // 2]
            pieces.append(buf)
        for p in pieces:
            if current and len((current + "\n\n" + p).encode()) > limit:
                chunks.append(current)
                current = p
            else:
                current = f"{current}\n\n{p}" if current else p
    if current:
        chunks.append(current)
    return chunks or [body]


def document_drafts(filename: str, data: bytes, *, uploaded_by: str) -> tuple[dict[str, str], dict[str, Any]]:
    """({pack path: OKF draft}, report) for one upload. `uploaded_by` is the OKF actor (`human:<id>`)."""
    name = PurePosixPath(filename.replace("\\", "/")).name or "document"
    ex = extract(name, data)
    title = _title(name, ex.text, ex.media_type)
    if ex.media_type == "text/markdown":
        body = _markdown_body(ex.text, title)
    elif ex.pages:
        body = "\n\n".join(f"# Page {i}\n\n{p}" for i, p in enumerate(ex.pages, start=1) if p)
    else:
        body = f"# {title}\n\n{ex.text.strip()}"
    body, screening = screen(body)
    sha = hashlib.sha256(data).hexdigest()
    stem = safe_segment(PurePosixPath(name).stem or "document")
    chunks = _parts(body)
    docs: dict[str, str] = {}
    for i, chunk in enumerate(chunks, start=1):
        path = f"documents/{stem}.md" if i == 1 else f"documents/{stem}.part-{i}.md"
        fm: dict[str, Any] = {
            "type": "Document", "title": title if len(chunks) == 1 else f"{title} (part {i} of {len(chunks)})",
            "status": "draft", "tags": ["document", "upload"], "generated": {"by": DOCUMENT_ACTOR},
            "sources": [{"id": "upload", "resource": f"upload:{name}", "title": name, "author": uploaded_by}],
            "analystos": {"kind": "document", "origin": "upload", "trusted": False, "media_type": ex.media_type,
                          "sha256": sha, "bytes": len(data), "extractor": ex.extractor, "part": i, "parts": len(chunks)}}
        docs[path] = okf.render_document(fm, chunk)
    report = {"filename": name, "media_type": ex.media_type, "bytes": len(data), "sha256": sha, "extractor": ex.extractor,
              "pages": len(ex.pages) or None, "parts": len(chunks), "redactions": screening.redactions,
              "instruction_lines_removed": screening.instruction_lines, "links_neutralized": screening.links_neutralized}
    return docs, report
