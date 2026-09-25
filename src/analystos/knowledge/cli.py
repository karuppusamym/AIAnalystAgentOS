"""`analystos knowledge ...`: operator commands for the knowledge packs and their index."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


def _pack(s: Any, args: argparse.Namespace) -> Any:
    from analystos.core.errors import NotFound
    from analystos.knowledge import store

    if args.pack:
        return store.get_pack(s, args.pack)
    if args.platform:
        return store.platform_pack(s, create=False)
    if args.workspace:
        if args.slug and args.slug != store.WORKSPACE_SLUG:
            from sqlalchemy import select

            from analystos.db.models import KnowledgePack

            pack = s.scalar(select(KnowledgePack).where(KnowledgePack.workspace_id == args.workspace,
                                                        KnowledgePack.slug == args.slug))
            if pack is None:
                raise NotFound(f"no pack {args.slug} in workspace {args.workspace}")
            return pack
        return store.workspace_pack(s, args.workspace)
    raise SystemExit("name a pack: --pack ID, --platform, or --workspace WS [--slug SLUG]")


def _user(s: Any, email: str) -> Any:
    from sqlalchemy import select

    from analystos.db.models import User

    user = s.scalar(select(User).where(User.email == email))
    if user is None:
        raise SystemExit(f"no user {email}")
    return user


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="analystos knowledge")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("reindex", help="drop the knowledge index and rebuild it from the packs' head revisions")
    sub.add_parser("status", help="index counts, HNSW index, embedding provider, packs")
    r = sub.add_parser("reembed", help="re-embed every section with the configured (or given) provider")
    r.add_argument("--provider", choices=["hashing", "sentence_transformers", "auto"])
    r.add_argument("--dim", type=int)
    q = sub.add_parser("search", help="hybrid retrieval as a workspace sees it")
    q.add_argument("--workspace", required=True)
    q.add_argument("--limit", type=int, default=8)
    q.add_argument("query")
    i = sub.add_parser("import", help="import an OKF or Atlas bundle (directory or .zip) as a read-only pack")
    i.add_argument("--workspace", required=True)
    i.add_argument("--slug", required=True)
    i.add_argument("--author", default="process:analystos-cli")
    i.add_argument("source")
    for name, helptext in (("export", "export a pack revision (.zip or directory); the publish policy must pass"),
                           ("history", "a pack's revisions"), ("remote", "set or clear a pack's remote repository"),
                           ("request-push", "request the approval a push needs"),
                           ("push", "push the head revision under an approved approval")):
        c = sub.add_parser(name, help=helptext)
        c.add_argument("--pack")
        c.add_argument("--platform", action="store_true")
        c.add_argument("--workspace")
        c.add_argument("--slug")
        if name == "export":
            c.add_argument("--revision", type=int)
            c.add_argument("out")
        if name == "remote":
            c.add_argument("--url")
            c.add_argument("--branch", default="main")
        if name in ("request-push", "push"):
            c.add_argument("--user", required=True, help="email of the requesting user")
        if name == "push":
            c.add_argument("--approval", required=True)
    args = p.parse_args(argv)

    from sqlalchemy import select

    from analystos.db.base import session_scope
    from analystos.db.models import KnowledgePack
    from analystos.knowledge import bundle, index, remote, store

    with session_scope() as s:
        if args.cmd == "reindex":
            report = index.reindex(s)
            _print({"documents": report["documents"], "sections": report["sections"], "embedding": report["embedding"],
                    "packs": [{k: r[k] for k in ("slug", "revision", "documents", "sections", "dangling_links")}
                              for r in report["packs"]], "hnsw_index": index.stats(s)["hnsw_index"]})
        elif args.cmd == "status":
            _print({**index.stats(s), "packs": [{"id": x.id, "slug": x.slug, "kind": x.kind, "workspace_id": x.workspace_id,
                                                 "head_revision": x.head_revision, "read_only": x.read_only,
                                                 "remote": x.git_remote}
                                                for x in s.scalars(select(KnowledgePack).order_by(KnowledgePack.id))]})
        elif args.cmd == "reembed":
            from analystos.core.config import get_settings
            from analystos.knowledge import embeddings

            settings = get_settings().model_copy(update={k: v for k, v in (("knowledge_embedding_provider", args.provider),
                                                                            ("knowledge_embedding_dim", args.dim)) if v})
            _print(index.reembed(s, embeddings.configured(settings)))
        elif args.cmd == "search":
            _print([h.as_dict() for h in index.retrieve(s, args.workspace, args.query, limit=args.limit)])
        elif args.cmd == "import":
            _print(bundle.import_bundle(s, args.workspace, Path(args.source), slug=args.slug, author=args.author).as_dict())
        else:
            pack = _pack(s, args)
            if args.cmd == "export":
                out = Path(args.out)
                files = bundle.export_files(s, pack, revision=args.revision)
                if out.suffix == ".zip":
                    out.write_bytes(bundle.zip_bytes(files))
                else:
                    out.mkdir(parents=True, exist_ok=True)
                    bundle.write_dir(files, out)
                _print({"pack": pack.slug, "revision": args.revision or pack.head_revision, "files": len(files), "out": str(out)})
            elif args.cmd == "history":
                _print(store.history(s, pack))
            elif args.cmd == "remote":
                remote.set_remote(s, pack, args.url, args.branch)
                _print({"pack": pack.slug, "remote": pack.git_remote, "branch": pack.git_branch})
            elif args.cmd == "request-push":
                apr = remote.request_push(s, pack, _user(s, args.user))
                _print({"approval_id": apr.id, "status": apr.status, "payload_hash": apr.payload_hash})
            elif args.cmd == "push":
                _print(remote.push(s, pack, _user(s, args.user), args.approval))
    return 0
