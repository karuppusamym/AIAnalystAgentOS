"""Workspace knowledge version (P4-T06): part of the L0 response-cache key, so an edit to the
knowledge a workspace sees (its context entries, the head revisions of the knowledge packs it sees,
enabled domain-pack versions or the platform settings) makes the next identical request miss the cache instead of replaying an answer
given under the old knowledge.

Episodes are left out: one is written at the end of every run, and when an episode reaches a
prompt the prompt text itself already changes the key."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from sqlalchemy import text

_DIGEST = text("""
SELECT count(*) AS n,
       coalesce(md5(string_agg(id || ':' || md5(kind || '|' || name || '|' || body || '|' ||
                                            coalesce(CAST(mapped_columns AS text), '') || '|' ||
                                            coalesce(CAST(synonyms AS text), '') || '|' || CAST(trusted AS text)),
                               ',' ORDER BY id)), '') AS digest
FROM context_entry
WHERE workspace_id = :ws AND kind <> 'episode'
""")
# Knowledge packs (P4-K01) the workspace sees: the platform pack and its own packs, by head content.
_PACKS = text("""
SELECT coalesce(string_agg(p.id || ':' || coalesce(r.content_digest, ''), ',' ORDER BY p.id), '') AS packs
FROM knowledge_pack p
LEFT JOIN knowledge_revision r ON r.pack_id = p.id AND r.number = p.head_revision
WHERE p.workspace_id = :ws OR p.kind = 'platform'
""")


def knowledge_version(session: Any, workspace_id: str | None, *, pack_refs: Iterable[str] = (),
                      settings_version: int | None = None) -> str:
    row = session.execute(_DIGEST, {"ws": workspace_id}).one()
    packs = session.execute(_PACKS, {"ws": workspace_id}).scalar()
    material = {"entries": [int(row.n), row.digest], "packs": sorted(set(pack_refs)), "settings": settings_version,
                "knowledge": packs or ""}
    return "kv:" + hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


def workspace_knowledge_version(workspace_id: str | None, *, pack_refs: Iterable[str] | None = None,
                                policy: Any = None) -> str | None:
    """Knowledge version with its own session. `pack_refs` defaults to the packs the workspace
    policy enables (all installed packs when the policy names none). Returns None when it cannot
    be computed — the key then falls back to the pre-P4-T06 form rather than failing the call."""
    from analystos.core.logging import get_logger
    from analystos.db.base import session_scope
    from analystos.services.platform_settings import current

    try:
        if pack_refs is None:
            from analystos.capabilities import packs

            explicit = getattr(policy, "domain_packs", None)
            pack_refs = [p.ref for p in (packs.enabled_for([], [], explicit) if explicit is not None else packs.installed())]
        with session_scope() as s:
            return knowledge_version(s, workspace_id, pack_refs=pack_refs, settings_version=current()[0])
    except Exception as exc:  # pragma: no cover - a missing version must never break model routing
        get_logger(__name__).warning("knowledge version unavailable for %s: %s", workspace_id, exc)
        return None
