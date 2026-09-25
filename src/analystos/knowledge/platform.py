"""The read-only platform pack (P4-K01): knowledge every workspace sees, replacing the old
NULL-workspace `context_entry` rows. Built from the installed domain packs by `analystos seed`;
files outside `domain/` (e.g. entries carried over by migration 0018) are kept as they are.

Layout: `index.md` (bundle root, `okf_version`), `domain/<pack>/<doc>.md` per domain-pack document.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy.orm import Session

from analystos.knowledge import okf, store
from analystos.knowledge.entries import render_entry, slugify

DOMAIN_DIR = "domain"


def domain_files(packs: Iterable[Any]) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for pack in packs:
        seen: set[str] = set()
        for doc in pack.knowledge:
            stem = slugify(doc.path.rsplit("/", 1)[-1][:-3] if doc.path and doc.path.endswith(".md") else doc.name)
            while stem in seen:
                stem += "-x"
            seen.add(stem)
            files[f"{DOMAIN_DIR}/{slugify(pack.name)}/{stem}.md"] = render_entry(
                kind=doc.kind, name=doc.name, body=doc.body, synonyms=doc.synonyms, mapped_columns=doc.maps_to,
                origin=f"pack:{pack.name}", domain_pack=pack.name,
                generated_by=f"process:analystos-domain-pack-{slugify(pack.name)}").encode()
    return files


def root_index(files: dict[str, bytes]) -> bytes:
    """Bundle-root index.md (§8), the one index allowed frontmatter, carrying `okf_version` (§12)."""
    groups: dict[str, int] = {}
    for path in files:
        if path != "index.md" and okf.is_markdown(path) and not path.endswith(("/index.md", "/log.md")):
            top = path.split("/", 2)
            key = "/".join(top[:2]) if top[0] == DOMAIN_DIR and len(top) > 2 else top[0]
            groups[key] = groups.get(key, 0) + 1
    lines = [f"---\nokf_version: '{okf.OKF_VERSION}'\n---\n", "# Platform knowledge", ""]
    for key in sorted(groups):
        lines.append(f"* {key} - {groups[key]} document(s)")
    return ("\n".join(lines) + "\n").encode()


def sync_from_domain_packs(session: Session, packs: Iterable[Any], *, author: str = "process:analystos-seed") -> dict[str, Any]:
    pack = store.platform_pack(session)
    assert pack is not None
    current = store.revision_files(session, pack)
    files = {p: b for p, b in current.items() if not p.startswith(DOMAIN_DIR + "/") and p != "index.md"}
    files.update(domain_files(packs))
    files["index.md"] = root_index(files)
    rev = store.commit(session, pack, files, author=author, reason="sync domain-pack knowledge", origin="seed",
                       system=True)
    return {"pack_id": pack.id, "revision": pack.head_revision, "changed": rev is not None,
            "documents": sum(1 for p in files if okf.is_markdown(p) and not p.endswith("index.md"))}
