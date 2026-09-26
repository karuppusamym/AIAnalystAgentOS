"""Definition lifecycle (ADR-0021, P7-03): draft → published → (deprecated) → retired.

Generic over `kind`: a kind registers a validator that normalizes its spec, so recipes, ML specs and
query tools reuse the same versions, hashes, status checks and events. A published version is
immutable; editing makes a new draft (version = next number). Triggers (API, schedules, monitors,
MCP) resolve what they run through `resolve` + `require_runnable`: drafts only in a workspace whose
settings say `environment: dev`, retired never. Built-in and pack YAML playbooks resolve as
published, identified by manifest version plus content digest (`builtin_ref`).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analystos.contracts.capability import CapabilityManifest
from analystos.contracts.definition import RUNNABLE_STATUSES, DefinitionDraftIn, DefinitionPatch, DefinitionRef
from analystos.core.errors import Conflict, InvalidInput, NotFound, PolicyDenied, PreconditionFailed
from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.db.models import Definition, User, Workspace
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import get_workspace, require_role

Validator = Callable[[Session, str, str, dict[str, Any]], dict[str, Any]]
_KINDS: dict[str, Validator] = {}


def register_kind(kind: str, validator: Validator | None = None) -> None:
    """A new definition kind (recipe, ML spec, query tool...). The validator returns the normalized
    spec or raises InvalidInput; without one any JSON object is accepted as-is."""
    _KINDS[kind] = validator or (lambda _s, _w, _k, spec: dict(spec))


def kinds() -> list[str]:
    return sorted(_KINDS)


# ------------------------------------------------------------------------------------ hashing / refs
def manifest_digest(manifest: CapabilityManifest | dict[str, Any]) -> str:
    """Content digest of a manifest. `source` (where the file was found) is not content."""
    raw = manifest.model_dump(mode="json") if isinstance(manifest, CapabilityManifest) else dict(manifest)
    return stable_hash({k: v for k, v in raw.items() if k != "source"})


def content_hash(kind: str, spec: dict[str, Any]) -> str:
    return stable_hash({"kind": kind, "spec": spec})


def builtin_ref(manifest: CapabilityManifest) -> DefinitionRef:
    """Built-in and pack YAML is published by definition: manifest version plus content digest."""
    return DefinitionRef(kind=manifest.kind.lower(), key=manifest.id, version=manifest.version,
                         content_hash=manifest_digest(manifest), source="builtin",
                         status="deprecated" if manifest.certification.status == "deprecated" else "published")


def ref_of(row: Definition) -> DefinitionRef:
    return DefinitionRef(kind=row.kind, key=row.key, version=row.version, content_hash=row.content_hash, source="workspace",
                         status=row.status, id=row.id)


def is_dev(session: Session, workspace_id: str) -> bool:
    ws = session.get(Workspace, workspace_id)
    return ws is not None and str((ws.settings or {}).get("environment", "")).lower() == "dev"


# ------------------------------------------------------------------------------------ kinds
def _playbook(session: Session, workspace_id: str, key: str, spec: dict[str, Any]) -> dict[str, Any]:
    """A workspace playbook is a `kind: Playbook` manifest composing registered agents; its references
    are checked against the current registry exactly as a pack's would be at load."""
    from pydantic import ValidationError

    from analystos.capabilities import registry
    from analystos.capabilities.validation import validate_kinds

    source = f"workspace:{workspace_id}"
    try:
        m = CapabilityManifest.model_validate({**spec, "source": source})
    except ValidationError as exc:
        raise InvalidInput("spec is not a capability manifest: " + "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])) from None
    if m.kind != "Playbook":
        raise InvalidInput("a playbook definition's spec must be a kind: Playbook manifest")
    if m.id != key:
        raise InvalidInput(f"the manifest id ({m.id}) must equal the definition key ({key})")
    snap = registry.current()
    where = f"{source}: {m.id}"
    problems = [p for p in validate_kinds({**snap.manifests, m.id: m}) if p.startswith(where)]
    if problems:
        raise InvalidInput("playbook does not validate: " + "; ".join(problems[:5]), details={"problems": problems})
    return m.model_dump(mode="json", exclude={"source"})


def _saved_analysis(session: Session, workspace_id: str, key: str, spec: dict[str, Any]) -> dict[str, Any]:
    missing = {"turn_id", "sql", "fingerprint"} - set(spec)
    if missing:
        raise InvalidInput(f"a saved analysis needs {sorted(missing)}")
    return dict(spec)


def _typed(model: Any) -> Validator:
    def check(session: Session, workspace_id: str, key: str, spec: dict[str, Any]) -> dict[str, Any]:
        from pydantic import ValidationError

        try:
            return model.model_validate(spec).model_dump(mode="json")
        except ValidationError as exc:
            raise InvalidInput("; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])) from None
    return check


def _register_builtin_kinds() -> None:
    from analystos.contracts.work import MLSpec, PipelineSpec

    register_kind("playbook", _playbook)
    register_kind("saved_analysis", _saved_analysis)
    register_kind("recipe", _typed(PipelineSpec))  # P6-04 replaces the placeholder contract
    register_kind("ml_spec", _typed(MLSpec))  # P5-01 replaces the placeholder contract
    register_kind("query_tool")  # P7-11


_register_builtin_kinds()


def _validate(session: Session, workspace_id: str, kind: str, key: str, spec: dict[str, Any]) -> dict[str, Any]:
    if kind not in _KINDS:
        raise InvalidInput(f"unknown definition kind {kind}; one of {kinds()}")
    if not isinstance(spec, dict):
        raise InvalidInput("spec must be an object")
    return _KINDS[kind](session, workspace_id, key, spec)


# ------------------------------------------------------------------------------------ lifecycle
def _latest_version(session: Session, workspace_id: str, kind: str, key: str) -> int:
    return session.scalar(select(func.max(Definition.version)).where(
        Definition.workspace_id == workspace_id, Definition.kind == kind, Definition.key == key)) or 0


def latest_published(session: Session, workspace_id: str, kind: str, key: str) -> Definition | None:
    return session.scalar(select(Definition).where(
        Definition.workspace_id == workspace_id, Definition.kind == kind, Definition.key == key,
        Definition.status.in_(RUNNABLE_STATUSES)).order_by(Definition.version.desc()).limit(1))


def _check_revision(row: Definition, expected: int | None) -> None:
    if expected is not None and expected != row.revision:
        raise PreconditionFailed(f"definition {row.id} is at revision {row.revision}, not {expected}",
                                 details={"current_revision": row.revision})


def _event(session: Session, row: Definition, type_: str, actor: str, **extra: Any) -> None:
    payload = {"definition_id": row.id, "kind": row.kind, "key": row.key, "version": row.version,
               "content_hash": row.content_hash, "status": row.status, **extra}
    emit(row.workspace_id, type_, payload, actor=actor, session=session)
    audit(actor, type_, workspace_id=row.workspace_id, target=row.id, details=payload, session=session)


def create_draft(session: Session, user: User, workspace_id: str, body: DefinitionDraftIn) -> Definition:
    require_role(session, user, workspace_id, "editor")
    spec = _validate(session, workspace_id, body.kind, body.key, body.spec)
    existing = session.scalar(select(Definition).where(Definition.workspace_id == workspace_id, Definition.kind == body.kind,
                                                       Definition.key == body.key, Definition.status == "draft"))
    if existing is not None:
        raise Conflict(f"{body.kind} {body.key} already has a draft ({existing.id}); edit it instead",
                       details={"draft_id": existing.id, "revision": existing.revision})
    row = Definition(id=new_id("defn"), workspace_id=workspace_id, kind=body.kind, key=body.key,
                     version=_latest_version(session, workspace_id, body.kind, body.key) + 1, status="draft",
                     title=body.title, spec=spec, content_hash=content_hash(body.kind, spec), revision=1,
                     created_by=user.id)
    session.add(row)
    session.flush()
    _event(session, row, "definition.draft_saved", f"user:{user.id}")
    return row


def update_draft(session: Session, user: User, row: Definition, patch: DefinitionPatch, expected_revision: int | None) -> Definition:
    require_role(session, user, row.workspace_id, "editor")
    if row.status != "draft":
        raise Conflict(f"version {row.version} is {row.status} and immutable; create a new draft to change it")
    _check_revision(row, expected_revision)
    if patch.spec is not None:
        row.spec = _validate(session, row.workspace_id, row.kind, row.key, patch.spec)
        row.content_hash = content_hash(row.kind, row.spec)
    if patch.title is not None:
        row.title = patch.title
    row.revision += 1
    _event(session, row, "definition.draft_saved", f"user:{user.id}")
    return row


def publish(session: Session, user: User, row: Definition, expected_revision: int | None, *,
            actor: str | None = None) -> Definition:
    """Freeze the draft. Re-validated against the registry as it is now, so a draft that went stale
    (an agent it uses was removed) cannot be published."""
    require_role(session, user, row.workspace_id, "editor")
    if row.status != "draft":
        raise Conflict(f"version {row.version} is already {row.status}")
    _check_revision(row, expected_revision)
    row.spec = _validate(session, row.workspace_id, row.kind, row.key, row.spec)
    row.content_hash = content_hash(row.kind, row.spec)
    row.status, row.published_by, row.published_at = "published", actor or user.id, utcnow()
    row.revision += 1
    session.flush()
    _event(session, row, "definition.published", f"user:{user.id}")
    _refresh_pins(session, row.workspace_id)
    return row


def publish_frozen(session: Session, workspace_id: str, kind: str, key: str, spec: dict[str, Any], *, created_by: str,
                   published_by: str, title: str | None = None) -> Definition:
    """Publish in one step what an approval already froze (e.g. a saved analysis). Same content as the
    newest published version returns that version instead of a duplicate."""
    spec = _validate(session, workspace_id, kind, key, spec)
    digest = content_hash(kind, spec)
    current = latest_published(session, workspace_id, kind, key)
    if current is not None and current.content_hash == digest:
        return current
    row = Definition(id=new_id("defn"), workspace_id=workspace_id, kind=kind, key=key,
                     version=_latest_version(session, workspace_id, kind, key) + 1, status="published", title=title,
                     spec=spec, content_hash=digest, revision=1, created_by=created_by, published_by=published_by,
                     published_at=utcnow())
    session.add(row)
    session.flush()
    _event(session, row, "definition.published", published_by if ":" in published_by else f"user:{published_by}")
    return row


def deprecate(session: Session, user: User, row: Definition, *, reason: str | None = None) -> Definition:
    require_role(session, user, row.workspace_id, "editor")
    if row.status != "published":
        raise Conflict(f"only a published version can be deprecated (this one is {row.status})")
    row.status, row.reason = "deprecated", reason or "deprecated"
    row.revision += 1
    _event(session, row, "definition.deprecated", f"user:{user.id}", reason=row.reason)
    _refresh_pins(session, row.workspace_id)
    return row


def retire(session: Session, user: User, row: Definition, *, reason: str | None = None) -> Definition:
    """Retired versions never run again; schedules pinned to one are blocked and their owners notified."""
    require_role(session, user, row.workspace_id, "editor")
    if row.status not in RUNNABLE_STATUSES:
        raise Conflict(f"only a published or deprecated version can be retired (this one is {row.status})")
    row.status, row.reason, row.retired_by, row.retired_at = "retired", reason or "retired", user.id, utcnow()
    row.revision += 1
    _event(session, row, "definition.retired", f"user:{user.id}", reason=row.reason)
    _refresh_pins(session, row.workspace_id)
    return row


def _refresh_pins(session: Session, workspace_id: str) -> None:
    from analystos.services.pins import refresh_workspace

    session.flush()
    refresh_workspace(session, workspace_id)


# ------------------------------------------------------------------------------------ resolution
def resolve(session: Session, workspace_id: str, ref: DefinitionRef | dict[str, Any] | str) -> tuple[DefinitionRef, dict[str, Any]]:
    """What a trigger names -> (exact ref, spec). A string is a definition row id or a key; a key with no
    version means the newest published version. A playbook key the workspace never defined resolves to
    the built-in/pack manifest of that id."""
    if isinstance(ref, str):
        ref = {"id": ref} if ref.startswith("defn_") else {"key": ref}
    if isinstance(ref, DefinitionRef):
        want = ref
    else:
        from pydantic import ValidationError

        try:
            want = DefinitionRef.model_validate({"kind": "playbook", **ref})
        except (ValidationError, TypeError):
            raise InvalidInput("a definition is named by {key, version} (kind defaults to playbook) or by its id") from None
    if not want.id and not want.key:
        raise InvalidInput("a definition is named by {key, version} or by its id")
    row: Definition | None = None
    if want.id:
        row = session.get(Definition, want.id)
        if row is None or row.workspace_id != workspace_id:
            raise NotFound("definition not found")
    elif want.source == "workspace":
        stmt = select(Definition).where(Definition.workspace_id == workspace_id, Definition.kind == want.kind,
                                        Definition.key == want.key)
        if want.version is not None:
            row = session.scalar(stmt.where(Definition.version == int(want.version)))
        else:
            row = latest_published(session, workspace_id, want.kind, want.key)
    if row is not None:
        if want.content_hash and want.content_hash != row.content_hash:
            raise Conflict(f"{row.kind} {row.key} v{row.version} has content {row.content_hash[:12]}, not {want.content_hash[:12]}")
        return ref_of(row), dict(row.spec)
    if want.kind == "playbook":
        from analystos.capabilities import registry

        manifest = registry.current().manifests.get(want.key)
        if manifest is not None and manifest.kind == "Playbook":
            got = builtin_ref(manifest)
            if want.version is not None and str(want.version) != got.version:
                raise NotFound(f"{want.key}@{want.version} is not installed (installed: {got.version})")
            return got, manifest.model_dump(mode="json", exclude={"source"})
    raise NotFound(f"{want.kind} {want.key} has no {'version ' + str(want.version) if want.version else 'published version'}")


def require_runnable(session: Session, workspace_id: str, ref: DefinitionRef, *, trigger: str) -> None:
    """Triggers run published versions only; a draft runs only in a workspace marked `environment: dev`."""
    if ref.status == "retired":
        raise PolicyDenied(f"{ref.label} is retired and cannot run", details={"definition": ref.model_dump()})
    if ref.status == "draft" and not is_dev(session, workspace_id):
        raise PolicyDenied(f"{ref.label} is a draft: {trigger} runs published versions only (publish it, or mark the "
                           "workspace environment: dev to run drafts)", details={"definition": ref.model_dump()})


def resolve_runnable(session: Session, workspace_id: str, ref: DefinitionRef | dict[str, Any] | str, *,
                     trigger: str) -> tuple[DefinitionRef, dict[str, Any]]:
    got, spec = resolve(session, workspace_id, ref)
    require_runnable(session, workspace_id, got, trigger=trigger)
    return got, spec


# ------------------------------------------------------------------------------------ views
def diff(a: Any, b: Any, path: str = "", limit: int = 200) -> list[dict[str, Any]]:
    """Field-level diff of two specs (what an upgrade would change)."""
    out: list[dict[str, Any]] = []

    def walk(x: Any, y: Any, p: str) -> None:
        if len(out) >= limit:
            return
        if isinstance(x, dict) and isinstance(y, dict):
            for k in sorted(set(x) | set(y)):
                walk(x.get(k), y.get(k), f"{p}.{k}" if p else str(k))
        elif x != y:
            out.append({"path": p, "from": x, "to": y})
    walk(a, b, path)
    return out


def out(row: Definition, *, spec: bool = True) -> dict[str, Any]:
    d = {c: getattr(row, c) for c in ("id", "workspace_id", "kind", "key", "version", "status", "title", "content_hash",
                                       "revision", "created_by", "published_by", "retired_by", "reason")}
    for c in ("published_at", "retired_at", "created_at", "updated_at"):
        v = getattr(row, c)
        d[c] = v.isoformat() if v else None
    if spec:
        d["spec"] = row.spec
    return d


def workspace_environment(session: Session, workspace_id: str) -> str:
    ws = get_workspace(session, workspace_id)
    return str((ws.settings or {}).get("environment") or "prod")
