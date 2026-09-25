"""Context providers (P4-K09, spec v3 §6.6): where knowledge for a question comes from.

* `local` — this workspace's packs (platform + workspace + any imported packs) through the index.
* `okf_import` — an Atlas or other OKF bundle imported on demand or on a schedule into a read-only
  `imported` pack; retrieval then reads that pack through the same index.
* `mcp` — a live call to Atlas's `get_knowledge_context` tool on an MCP server the workspace has
  registered and allowlisted (P4-X05). The call goes through `mcp.client.invoke_tool`, so the
  allowlist, workspace policy, per-run budget, screening and the tool_execution record all apply.

This answers spec v2 §18 open question 1 (CTX-001) and retires the speculative REST adapter
(`Context2AIClient`, v1 §11.1 paths) that no Context2AI deployment ever served.

A provider returns knowledge as *data* with receipts (path, anchor, sha256); it can never grant a
tool, widen scope or approve anything. Which providers a workspace uses is its policy's
`context_providers` list (default: local only).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.orm import Session

from analystos.core.errors import AnalystOSError, InvalidInput
from analystos.core.logging import get_logger

log = get_logger(__name__)

ATLAS_KNOWLEDGE_TOOL = "atlas__get_knowledge_context"
KINDS = ("local", "okf_import", "mcp")


@dataclass(frozen=True)
class ContextItem:
    title: str
    text: str
    path: str
    anchor: str
    sha256: str
    type: str = ""
    heading: str = ""
    score: float = 0.0
    trust: str = "unverified"
    status: str | None = None

    def receipt(self, provider: str) -> dict[str, Any]:
        return {"provider": provider, "path": self.path, "anchor": self.anchor, "sha256": self.sha256}


@dataclass
class ProviderResult:
    provider: str
    status: str  # MATCHED | NO_MATCH | UNAVAILABLE
    items: list[ContextItem] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def receipts(self) -> list[dict[str, Any]]:
        return [i.receipt(self.provider) for i in self.items]

    def as_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "status": self.status, "detail": self.detail, "receipts": self.receipts,
                "items": [i.__dict__ for i in self.items]}


class ContextProvider(Protocol):
    kind: str

    def retrieve(self, session: Session, workspace_id: str, question: str, *, limit: int = 8,
                 user: Any = None, run_id: str | None = None) -> ProviderResult: ...


def _from_hits(provider: str, hits: list[Any]) -> ProviderResult:
    items = [ContextItem(title=h.title, text=h.text, path=h.path, anchor=h.anchor, sha256=h.document_sha256,
                         type=h.type, heading=h.heading, score=h.score, trust=h.trust_tier, status=h.status)
             for h in hits]
    return ProviderResult(provider, "MATCHED" if items else "NO_MATCH", items)


@dataclass
class LocalProvider:
    """This workspace's own knowledge: the platform pack and the workspace's packs."""

    kind: str = "local"
    pack_kinds: tuple[str, ...] = ("platform", "workspace")

    def retrieve(self, session: Session, workspace_id: str, question: str, *, limit: int = 8,
                 user: Any = None, run_id: str | None = None) -> ProviderResult:
        from analystos.knowledge.index import retrieve
        from analystos.knowledge.store import visible_packs

        packs = [p.id for p in visible_packs(session, workspace_id) if p.kind in self.pack_kinds]
        return _from_hits(self.kind, retrieve(session, workspace_id, question, limit=limit, pack_ids=packs))


@dataclass
class OkfImportProvider:
    """An imported bundle. `sync` (re-)imports from `location` (a directory, a .zip, or bytes);
    `retrieve` reads the imported pack through the index — never the source at question time."""

    slug: str
    location: str | None = None
    kind: str = "okf_import"

    def sync(self, session: Session, workspace_id: str, *, author: str, source: Any = None) -> dict[str, Any]:
        from analystos.knowledge.bundle import import_bundle

        src = source if source is not None else self.location
        if src is None:
            raise InvalidInput(f"okf_import provider {self.slug} has no location")
        if source is None:
            src = _confined(str(src))
        return import_bundle(session, workspace_id, src, slug=self.slug, author=author,
                             origin={"provider": "okf_import", "location": str(self.location or "upload")}).as_dict()

    def retrieve(self, session: Session, workspace_id: str, question: str, *, limit: int = 8,
                 user: Any = None, run_id: str | None = None) -> ProviderResult:
        from analystos.knowledge.index import retrieve
        from analystos.knowledge.store import visible_packs

        packs = [p.id for p in visible_packs(session, workspace_id) if p.kind == "imported" and p.slug == self.slug]
        if not packs:
            return ProviderResult(self.kind, "UNAVAILABLE", detail={"reason": "not_imported", "slug": self.slug})
        return _from_hits(self.kind, retrieve(session, workspace_id, question, limit=limit, pack_ids=packs))


def _confined(location: str) -> Any:
    """A configured bundle location must lie below the upload directory, like file sources: a
    workspace policy must not be a way to read arbitrary server paths."""
    from pathlib import Path

    from analystos.core.config import get_settings

    base = Path(get_settings().upload_dir).resolve()
    path = (base / location).resolve() if not Path(location).is_absolute() else Path(location).resolve()
    if base != path and base not in path.parents:
        raise InvalidInput("okf_import location must be below the upload directory")
    return path


# ------------------------------------------------------------------------------------ Atlas over MCP
_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)\n```", re.S)


def parse_atlas_context(result: dict[str, Any]) -> ProviderResult:
    """Atlas `get_knowledge_context` result (content[0] Markdown, content[1] a ```json block of the
    OkfContextRead selection) -> items with Atlas's own receipts. Screened text only: the caller
    passes what `mcp.client.screen_result` returned."""
    if result.get("is_error"):
        return ProviderResult("mcp", "UNAVAILABLE", detail={"reason": "tool_error", "text": str(result.get("text"))[:300]})
    structured = result.get("structured")
    if not (isinstance(structured, dict) and ("documents" in structured or "status" in structured)):
        m = _JSON_BLOCK.search(str(result.get("text") or ""))
        if not m:
            return ProviderResult("mcp", "UNAVAILABLE", detail={"reason": "unparseable_result",
                                                                 "truncated": bool(result.get("truncated"))})
        try:
            structured = json.loads(m.group(1))
        except ValueError:
            return ProviderResult("mcp", "UNAVAILABLE", detail={"reason": "unparseable_result"})
    items: list[ContextItem] = []
    for doc in structured.get("documents") or []:
        if not isinstance(doc, dict):
            continue
        for sec in doc.get("sections") or []:
            if not isinstance(sec, dict):
                continue
            items.append(ContextItem(title=str(doc.get("title") or ""), text=str(sec.get("text") or ""),
                                     path=str(doc.get("path") or ""), anchor=str(sec.get("anchor") or ""),
                                     sha256=str(doc.get("sha256") or ""), type=str(doc.get("type") or ""),
                                     heading=str(sec.get("heading") or ""), score=float(doc.get("score") or 0.0),
                                     trust="atlas-approved" if doc.get("approved_statements") else "unverified",
                                     status=doc.get("status")))
    status = str(structured.get("status") or ("MATCHED" if items else "NO_MATCH"))
    pub = structured.get("publication") if isinstance(structured.get("publication"), dict) else {}
    detail = {"publication_id": pub.get("publication_id"), "bundle_content_digest": pub.get("bundle_content_digest"),
              "omitted_count": structured.get("omitted_count"), "ambiguous": structured.get("ambiguous") or [],
              "egress": structured.get("egress")}
    return ProviderResult("mcp", "NO_MATCH" if status == "NO_MATCH" else ("MATCHED" if items else "NO_MATCH"),
                          items, detail)


@dataclass
class McpAtlasProvider:
    """Atlas `get_knowledge_context` through a registered, allowlisted MCP server."""

    server: str
    product_key: str
    version: int
    tool: str = ATLAS_KNOWLEDGE_TOOL
    max_chars: int | None = None
    kind: str = "mcp"

    def arguments(self, question: str) -> dict[str, Any]:
        args: dict[str, Any] = {"product_key": self.product_key, "version": int(self.version), "question": question[:2000]}
        if self.max_chars:
            args["max_chars"] = int(self.max_chars)
        return args

    def retrieve(self, session: Session, workspace_id: str, question: str, *, limit: int = 8,
                 user: Any = None, run_id: str | None = None) -> ProviderResult:
        from analystos.db.base import session_scope
        from analystos.mcp.client import invoke_tool

        if user is None:
            return ProviderResult(self.kind, "UNAVAILABLE", detail={"reason": "no_user_identity"})
        try:
            out = invoke_tool(session_scope, user, workspace_id, self.server, self.tool, self.arguments(question),
                              run_id=run_id, agent_id="context")
        except AnalystOSError as exc:
            return ProviderResult(self.kind, "UNAVAILABLE", detail={"reason": exc.code, "message": exc.message[:300]})
        if out.get("status") != "ok":
            return ProviderResult(self.kind, "UNAVAILABLE", detail={"reason": out.get("status"),
                                                                     "approval_id": out.get("approval_id")})
        res = parse_atlas_context(out["result"])
        res.items = res.items[: max(limit, 1) * 4]
        res.detail.update({"server": self.server, "tool": self.tool, "product_key": self.product_key,
                           "version": self.version})
        return res


def build(config: dict[str, Any]) -> ContextProvider:
    kind = str(config.get("kind") or "")
    if kind == "local":
        return LocalProvider()
    if kind == "okf_import":
        if not config.get("slug"):
            raise InvalidInput("okf_import provider needs a slug")
        return OkfImportProvider(slug=str(config["slug"]), location=config.get("location"))
    if kind == "mcp":
        missing = [k for k in ("server", "product_key", "version") if not config.get(k)]
        if missing:
            raise InvalidInput(f"mcp provider needs {', '.join(missing)}")
        return McpAtlasProvider(server=str(config["server"]), product_key=str(config["product_key"]),
                                version=int(config["version"]), tool=str(config.get("tool") or ATLAS_KNOWLEDGE_TOOL),
                                max_chars=config.get("max_chars"))
    raise InvalidInput(f"unknown context provider kind {kind!r} ({' | '.join(KINDS)})")


def for_workspace(policy: Any) -> list[ContextProvider]:
    configs = getattr(policy, "context_providers", None) or [{"kind": "local"}]
    out: list[ContextProvider] = []
    for c in configs:
        try:
            out.append(build(c if isinstance(c, dict) else c.model_dump()))
        except InvalidInput as exc:
            log.warning("context provider skipped: %s", exc.message)
    return out
