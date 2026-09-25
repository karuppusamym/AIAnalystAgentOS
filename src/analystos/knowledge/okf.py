"""Open Knowledge Format v0.2 reader, writer and checks (P4-K01/K02, ADR-0013).

Pure: no database, clock, network or model. The profile this module implements, and what it does
*not* claim, is written down in `docs/10-architecture/okf-profile.md`.

Pinned text: upstream `SPEC.md` at the commit below, SHA-256 recorded beside it (the same pin
Atlas uses, so bundles interoperate). Conformance here means the three §11 clauses as this module
reads them — no upstream conformance suite exists, so no certification is claimed.

Two verdicts are kept apart, as Atlas does:

* `check_conformance` — is this a conformant OKF v0.2 bundle? Nothing stricter. Broken links,
  unknown types and unknown keys are *not* problems (§11 forbids rejecting them).
* `check_publish_policy` — may AnalystOS publish (export, push) it? Conformance plus: safe
  paths, no dangling internal links, and bounded document, frontmatter and bundle sizes.
"""
from __future__ import annotations

import hashlib
import posixpath
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import yaml

OKF_VERSION = "0.2"
OKF_SPEC_REPOSITORY = "https://github.com/GoogleCloudPlatform/open-knowledge-format"
OKF_SPEC_PATH = "SPEC.md"
OKF_SPEC_REVISION = "0b87c52c6ef999286c745e19998fdfcd03d5dbee"
OKF_SPEC_SHA256 = "26aa5da029278939f914e578107242d9607d4f2dc5fe153272b82f9ed1030101"
CONFORMANCE_STATUS = "SELF_CHECKED_AGAINST_PINNED_SPEC_CLAUSES"
RESERVED = ("index.md", "log.md")


@dataclass(frozen=True)
class Limits:
    """Publish-policy and import bounds. Code constants, not settings: a hostile archive must not
    be able to wait for an operator to raise one."""

    max_document_bytes: int = 256 * 1024
    max_frontmatter_bytes: int = 32 * 1024
    max_bundle_bytes: int = 64 * 1024 * 1024
    max_files: int = 20_000
    max_path_chars: int = 400
    max_yaml_depth: int = 16
    max_yaml_events: int = 8_000
    max_links_per_document: int = 5_000


LIMITS = Limits()


class OkfError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


@dataclass(frozen=True)
class Problem:
    code: str
    path: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "detail": self.detail}


@dataclass(frozen=True)
class Section:
    ordinal: int
    anchor: str
    heading: str
    text: str


@dataclass(frozen=True)
class Link:
    raw: str
    kind: str  # internal | external | fragment | invalid
    target: str | None  # bundle path (with the pack's okf root) for internal links


@dataclass
class OkfDocument:
    path: str  # pack path, e.g. "glossary/sla.md" or "bundle/concepts/x.md"
    concept_id: str  # bundle-relative path without ".md" (spec §2)
    frontmatter: dict[str, Any]
    body: str
    sha256: str
    size: int
    sections: list[Section] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)

    @property
    def type(self) -> str:
        return str(self.frontmatter.get("type") or "")

    @property
    def title(self) -> str:
        t = self.frontmatter.get("title")
        return str(t) if t else self.concept_id.rsplit("/", 1)[-1]

    @property
    def description(self) -> str | None:
        d = self.frontmatter.get("description")
        return str(d) if d else None

    @property
    def status(self) -> str:
        """§5.4: absent status reads as stable."""
        return str(self.frontmatter.get("status") or "stable")

    @property
    def trust_tier(self) -> str:
        return trust_tier(self.frontmatter)

    @property
    def extension(self) -> dict[str, Any]:
        ext = self.frontmatter.get("analystos")
        return ext if isinstance(ext, dict) else {}


# ------------------------------------------------------------------------------------ YAML
def safe_yaml(text: str, limits: Limits = LIMITS) -> Any:
    """`yaml.safe_load` behind structural limits: no anchors or aliases (the billion-laughs shape),
    no explicit tags, bounded depth and event count, one document only."""
    depth = events = docs = 0
    try:
        for ev in yaml.parse(text, Loader=yaml.SafeLoader):
            events += 1
            if events > limits.max_yaml_events:
                raise OkfError("YAML_TOO_MANY_NODES")
            if isinstance(ev, yaml.AliasEvent) or getattr(ev, "anchor", None):
                raise OkfError("YAML_ALIAS_NOT_ALLOWED")
            if isinstance(ev, (yaml.ScalarEvent, yaml.SequenceStartEvent, yaml.MappingStartEvent)):
                tag = getattr(ev, "tag", None)
                implicit = getattr(ev, "implicit", True)
                explicit = (tag is not None and not (implicit if isinstance(implicit, bool) else any(implicit)))
                if explicit and not (isinstance(ev, yaml.ScalarEvent) and tag == "!"):
                    raise OkfError("YAML_TAG_NOT_ALLOWED")
            if isinstance(ev, (yaml.SequenceStartEvent, yaml.MappingStartEvent)):
                depth += 1
                if depth > limits.max_yaml_depth:
                    raise OkfError("YAML_TOO_DEEP")
            elif isinstance(ev, (yaml.SequenceEndEvent, yaml.MappingEndEvent)):
                depth -= 1
            elif isinstance(ev, yaml.DocumentStartEvent):
                docs += 1
                if docs > 1:
                    raise OkfError("YAML_MULTIPLE_DOCUMENTS")
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise OkfError("YAML_INVALID", str(exc).splitlines()[0][:200]) from None


# ------------------------------------------------------------------------------------ parsing
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """(frontmatter text, body). Frontmatter must open on the first line with `---` (§4)."""
    if not text.startswith("---"):
        return None, text
    lines = text.split("\n")
    if lines[0].rstrip("\r ") != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r ") == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return None, text


def decode(path: str, data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise OkfError("ENCODING_INVALID", path) from None
    return text[1:] if text.startswith("﻿") else text


def parse_frontmatter(path: str, text: str, limits: Limits = LIMITS) -> tuple[dict[str, Any] | None, str]:
    fm, body = split_frontmatter(text)
    if fm is None:
        return None, body
    if len(fm.encode()) > limits.max_frontmatter_bytes:
        raise OkfError("FRONTMATTER_TOO_LARGE", path)
    meta = safe_yaml(fm, limits)
    if meta is None:
        meta = {}
    if not isinstance(meta, dict) or not all(isinstance(k, str) for k in meta):
        raise OkfError("FRONTMATTER_NOT_A_MAPPING", path)
    return meta, body


def parse_document(path: str, data: bytes, *, root: str = "", limits: Limits = LIMITS) -> OkfDocument:
    """One concept document. Raises OkfError when it has no parseable frontmatter or no `type`."""
    text = decode(path, data)
    meta, body = parse_frontmatter(path, text, limits)
    if meta is None:
        raise OkfError("FRONTMATTER_MISSING", path)
    if not str(meta.get("type") or "").strip():
        raise OkfError("TYPE_MISSING", path)
    rel = relative_to_root(path, root)
    doc = OkfDocument(path=path, concept_id=rel[:-3] if rel.endswith(".md") else rel, frontmatter=meta, body=body,
                      sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    doc.sections = split_sections(body, meta)
    doc.links = [resolve_link(path, raw, root) for raw in extract_links(body)]
    return doc


def slug(text: str) -> str:
    s = re.sub(r"[^\w\- ]+", "", text.strip().lower(), flags=re.U)
    return re.sub(r"[\s]+", "-", s).strip("-") or "section"


def _without_code(lines: Iterable[str]) -> list[tuple[str, bool]]:
    """(line, inside a fenced block)."""
    out: list[tuple[str, bool]] = []
    fence: str | None = None
    for line in lines:
        m = _FENCE.match(line)
        if fence is None and m:
            fence = m.group(1)[0] * 3
            out.append((line, True))
        elif fence is not None:
            out.append((line, True))
            if line.strip().startswith(fence):
                fence = None
        else:
            out.append((line, False))
    return out


def split_sections(body: str, frontmatter: Mapping[str, Any] | None = None) -> list[Section]:
    """Cut at top-level (`# `) headings outside code fences, as Atlas's okf_context does. Text
    before the first heading is the `preamble`; a document with no body text gets one section
    made of its title and description so it is still retrievable."""
    chunks: list[tuple[str, list[str]]] = [("", [])]
    for line, in_code in _without_code(body.split("\n")):
        if not in_code and line.startswith("# "):
            chunks.append((line[2:].strip(), []))
        else:
            chunks[-1][1].append(line)
    out: list[Section] = []
    seen: dict[str, int] = {}
    for heading, lines in chunks:
        text = "\n".join(lines).strip()
        if not heading and not text:
            continue
        base = slug(heading) if heading else "preamble"
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append(Section(ordinal=len(out), anchor=base if n == 0 else f"{base}-{n}", heading=heading, text=text))
    if not out:
        fm = frontmatter or {}
        text = " ".join(str(fm.get(k)) for k in ("title", "description") if fm.get(k))
        out.append(Section(ordinal=0, anchor="summary", heading="", text=text))
    return out


_INLINE = re.compile(r"!?\[(?:[^\]\\]|\\.)*\]\(\s*<?([^)\s>]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
_REFDEF = re.compile(r"^\s{0,3}\[(?!\^)(?:[^\]\\]|\\.)+\]:\s*<?(\S+?)>?(?:\s+.*)?$")
_CODESPAN = re.compile(r"(`+)(?:(?!\1).)+?\1")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def extract_links(body: str) -> list[str]:
    """Markdown link destinations (inline and reference definitions), outside code."""
    out: list[str] = []
    for line, in_code in _without_code(body.split("\n")):
        if in_code:
            continue
        plain = _CODESPAN.sub("", line.replace("\\[", "").replace("\\]", ""))
        out.extend(m.group(1) for m in _INLINE.finditer(plain))
        ref = _REFDEF.match(plain)
        if ref:
            out.append(ref.group(1))
    return out


def relative_to_root(path: str, root: str) -> str:
    if not root:
        return path
    prefix = root.rstrip("/") + "/"
    return path[len(prefix):] if path.startswith(prefix) else path


def resolve_link(doc_path: str, raw: str, root: str = "") -> Link:
    """§6.1: `/x` is bundle-relative, anything else relative to the document. A directory link
    (`dir/`) means its `index.md`. A scheme or `//` makes it external."""
    target = raw.strip()
    if target.startswith("#"):
        return Link(raw, "fragment", None)
    if _SCHEME.match(target) or target.startswith("//"):
        return Link(raw, "external", None)
    target = target.split("#", 1)[0].split("?", 1)[0]
    if not target:
        return Link(raw, "fragment", None)
    rel_doc = relative_to_root(doc_path, root)
    joined = target.lstrip("/") if target.startswith("/") else posixpath.join(posixpath.dirname(rel_doc), target)
    norm = posixpath.normpath(joined) if joined else "."
    if norm.startswith("../") or norm == "..":
        return Link(raw, "invalid", None)
    if target.endswith("/") or norm == ".":
        norm = "index.md" if norm == "." else f"{norm}/index.md"
    full = f"{root.rstrip('/')}/{norm}" if root else norm
    return Link(raw, "internal", full)


# ------------------------------------------------------------------------------------ trust
def verified_entries(meta: Mapping[str, Any]) -> list[dict[str, Any]]:
    """§5.2: a bare `{by, at}` mapping is a one-element list."""
    v = meta.get("verified")
    if isinstance(v, dict):
        return [v]
    return [e for e in v if isinstance(e, dict)] if isinstance(v, list) else []


def trust_tier(meta: Mapping[str, Any]) -> str:
    """§5.3: unverified | machine-confirmed | human-reviewed. Advisory, never access control."""
    entries = verified_entries(meta)
    if not entries:
        return "unverified"
    if any(str(e.get("by") or "").startswith("human:") for e in entries):
        return "human-reviewed"
    return "machine-confirmed"


def parse_instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def is_stale(meta: Mapping[str, Any], now: datetime) -> bool:
    """§5.5: stale when now >= stale_after."""
    at = parse_instant(meta.get("stale_after"))
    return at is not None and now >= at


# ------------------------------------------------------------------------------------ writing
def render_document(frontmatter: Mapping[str, Any], body: str) -> str:
    """Deterministic document text: sorted keys, block style, one trailing newline."""
    fm = yaml.safe_dump(dict(frontmatter), sort_keys=True, allow_unicode=True, default_flow_style=False, width=4096)
    return f"---\n{fm}---\n\n{body.strip()}\n"


def content_digest(files: Mapping[str, str]) -> str:
    """Digest of a {path: sha256} manifest: the identity of one revision's content."""
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(f"{path}\0{files[path]}\n".encode())
    return h.hexdigest()


# ------------------------------------------------------------------------------------ checks
_DATE_HEADING = re.compile(r"^## (\d{4}-\d{2}-\d{2})\s*$")
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")


def is_markdown(path: str) -> bool:
    return path.lower().endswith(".md")


def check_path(path: str, limits: Limits = LIMITS) -> str | None:
    """Reason code when a pack path is unsafe, else None. Paths are posix, relative, no dot segments."""
    if not path or len(path) > limits.max_path_chars:
        return "PATH_UNSAFE"
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return "PATH_ABSOLUTE"
    if "\\" in path or any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        return "PATH_UNSAFE"
    for seg in path.split("/"):
        if seg in ("", ".", ".."):
            return "PATH_TRAVERSAL" if seg == ".." else "PATH_UNSAFE"
        if not _SAFE_SEGMENT.match(seg):
            return "PATH_UNSAFE"
    return None


def _in_root(path: str, root: str) -> bool:
    return not root or path.startswith(root.rstrip("/") + "/")


def check_conformance(files: Mapping[str, bytes], root: str = "", limits: Limits = LIMITS) -> list[Problem]:
    """OKF v0.2 §11, the three clauses, over the `.md` files under `root`."""
    problems: list[Problem] = []
    for path in sorted(files):
        if not is_markdown(path) or not _in_root(path, root):
            continue
        name = path.rsplit("/", 1)[-1]
        rel = relative_to_root(path, root)
        try:
            text = decode(path, files[path])
            if name in RESERVED:
                problems.extend(_check_reserved(path, rel, name, text, limits))
                continue
            meta, _ = parse_frontmatter(path, text, limits)
        except OkfError as exc:
            problems.append(Problem(exc.code, path))
            continue
        if meta is None:
            problems.append(Problem("FRONTMATTER_MISSING", path))
        elif not str(meta.get("type") or "").strip():
            problems.append(Problem("TYPE_MISSING", path))
    return problems


def _check_reserved(path: str, rel: str, name: str, text: str, limits: Limits) -> list[Problem]:
    meta, body = parse_frontmatter(path, text, limits)
    out: list[Problem] = []
    # §8/§12: only the bundle-root index.md may carry frontmatter, and only `okf_version`.
    if meta is not None and not (name == "index.md" and rel == "index.md" and set(meta) <= {"okf_version"}):
        out.append(Problem("RESERVED_FILE_FRONTMATTER", path))
    if name == "log.md":
        for line in body.split("\n"):
            if line.startswith("## ") and not _DATE_HEADING.match(line):
                out.append(Problem("LOG_DATE_HEADING", path, line[3:40]))
    return out


def check_publish_policy(files: Mapping[str, bytes], root: str = "", limits: Limits = LIMITS,
                         allowed_extras: Iterable[str] = ()) -> list[Problem]:
    """Conformance plus the AnalystOS publish rules: safe paths, size caps, no dangling links."""
    problems = check_conformance(files, root, limits)
    extras = set(allowed_extras)
    total = 0
    if len(files) > limits.max_files:
        problems.append(Problem("BUNDLE_TOO_MANY_FILES", "", str(len(files))))
    for path in sorted(files):
        total += len(files[path])
        code = check_path(path, limits)
        if code:
            problems.append(Problem(code, path))
        if len(files[path]) > limits.max_document_bytes and path not in extras:
            problems.append(Problem("DOCUMENT_TOO_LARGE", path, str(len(files[path]))))
    if total > limits.max_bundle_bytes:
        problems.append(Problem("BUNDLE_TOO_LARGE", "", str(total)))
    for path in sorted(files):
        if not is_markdown(path) or not _in_root(path, root):
            continue
        try:
            text = decode(path, files[path])
        except OkfError:
            continue
        _, body = split_frontmatter(text)
        raws = extract_links(body)
        if len(raws) > limits.max_links_per_document:
            problems.append(Problem("LINK_LIMIT_EXCEEDED", path))
            continue
        for raw in raws:
            link = resolve_link(path, raw, root)
            if link.kind == "invalid":
                problems.append(Problem("LINK_OUTSIDE_BUNDLE", path, raw[:200]))
            elif link.kind == "internal" and link.target not in files:
                problems.append(Problem("LINK_DANGLING", path, raw[:200]))
    return problems


__all__ = ["CONFORMANCE_STATUS", "LIMITS", "OKF_SPEC_REVISION", "OKF_SPEC_SHA256", "OKF_VERSION", "Limits", "Link",
           "OkfDocument", "OkfError", "Problem", "Section", "check_conformance", "check_path", "check_publish_policy",
           "content_digest", "extract_links", "is_stale", "parse_document", "render_document", "resolve_link",
           "safe_yaml", "split_sections", "trust_tier"]
