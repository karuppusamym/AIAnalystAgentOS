"""OKF bundle import and export (P4-K02), including Atlas bundles.

Import is lossless: every accepted file is stored byte for byte in a read-only `imported` pack
(unknown keys, trust claims and extension files such as `atlas-manifest.json` included), so an
export of that revision reproduces the same members with the same bytes. What an imported file
*says* is evidence, not authority: `verified` claims and Attested Computations are counted in the
report and nothing is executed (ADR-0013 §8; the pack is read-only and never auto-approved).

Hostile archives are refused before anything is stored: path traversal, absolute or unsafe
names, symlinks, encrypted members, unexpected file types, case-insensitive duplicates, and
size, member-count and compression-ratio bombs. Limits are constants (see `ArchiveLimits`).

Export enforces the publish policy (`okf.check_publish_policy`): no dangling internal links, safe
paths, size caps. The archive is deterministic: sorted members, fixed timestamps and attributes.
"""
from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from analystos.core.errors import Conflict, InvalidInput
from analystos.db.models import KnowledgePack
from analystos.knowledge import okf, store

ATLAS_MANIFEST = "atlas-manifest.json"
ATLAS_ROOT = "bundle"
ALLOWED_SUFFIXES = (".md", ".json", ".yaml", ".yml")
OS_METADATA = ("__MACOSX/", ".DS_Store", "Thumbs.db", "desktop.ini")
_EPOCH = (1980, 1, 1, 0, 0, 0)


@dataclass(frozen=True)
class ArchiveLimits:
    max_archive_bytes: int = 32 * 1024 * 1024
    max_members: int = 20_000
    max_manifest_bytes: int = 16 * 1024 * 1024
    max_ratio: int = 100
    ratio_floor_bytes: int = 64 * 1024


ARCHIVE_LIMITS = ArchiveLimits()


@dataclass
class ImportReport:
    pack_id: str
    slug: str
    format: str
    okf_root: str
    revision: int | None
    changed: bool
    files: int
    documents: int
    ignored: list[str] = field(default_factory=list)
    conformance: list[dict[str, str]] = field(default_factory=list)
    dangling_links: int = 0
    verified_claims: int = 0
    attested_computations: int = 0
    manifest: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


# ------------------------------------------------------------------------------------ reading
def _is_os_metadata(name: str) -> bool:
    return name.startswith("__MACOSX/") or name.rsplit("/", 1)[-1] in OS_METADATA[1:]


def read_zip(data: bytes, limits: ArchiveLimits = ARCHIVE_LIMITS, okf_limits: okf.Limits = okf.LIMITS
             ) -> tuple[dict[str, bytes], list[str]]:
    """Archive bytes -> ({path: bytes}, ignored OS-metadata names). Raises InvalidInput(code)."""
    if len(data) > limits.max_archive_bytes:
        raise InvalidInput("ARCHIVE_TOO_LARGE")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise InvalidInput("ARCHIVE_NOT_A_ZIP") from None
    infos = zf.infolist()
    if len(infos) > limits.max_members:
        raise InvalidInput("ARCHIVE_TOO_MANY_MEMBERS")
    files: dict[str, bytes] = {}
    ignored: list[str] = []
    seen: set[str] = set()
    total = 0
    for info in infos:
        name = info.filename
        if info.is_dir():
            continue
        if _is_os_metadata(name):
            ignored.append(name)
            continue
        code = okf.check_path(name, okf_limits)
        if code:
            raise InvalidInput(f"{code}: {name[:200]!r}")
        mode = (info.external_attr >> 16) & 0o170000
        if mode and mode not in (stat.S_IFREG,):
            raise InvalidInput(f"{'MEMBER_SYMLINK' if mode == stat.S_IFLNK else 'MEMBER_SPECIAL_FILE'}: {name!r}")
        if info.flag_bits & 0x1:
            raise InvalidInput(f"MEMBER_ENCRYPTED: {name!r}")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise InvalidInput(f"MEMBER_COMPRESSION_UNSUPPORTED: {name!r}")
        if not name.lower().endswith(ALLOWED_SUFFIXES):
            raise InvalidInput(f"MEMBER_NOT_ALLOWED: {name!r}")
        if name.lower() in seen:
            raise InvalidInput(f"ARCHIVE_DUPLICATE_MEMBER: {name!r}")
        seen.add(name.lower())
        cap = limits.max_manifest_bytes if name == ATLAS_MANIFEST else okf_limits.max_document_bytes
        if info.file_size > cap:
            raise InvalidInput(f"MEMBER_TOO_LARGE: {name!r}")
        if info.file_size >= limits.ratio_floor_bytes and info.file_size > limits.max_ratio * max(1, info.compress_size):
            raise InvalidInput(f"ARCHIVE_COMPRESSION_RATIO: {name!r}")
        total += info.file_size
        if total > okf_limits.max_bundle_bytes:
            raise InvalidInput("ARCHIVE_EXPANDS_TOO_LARGE")
        with zf.open(info) as fh:
            content = fh.read(cap + 1)
        if len(content) != info.file_size:
            raise InvalidInput(f"ARCHIVE_SIZE_MISMATCH: {name!r}")
        files[name] = content
    return files, ignored


def read_dir(root: Path, okf_limits: okf.Limits = okf.LIMITS) -> tuple[dict[str, bytes], list[str]]:
    """A bundle directory (or a git checkout of one) -> files. Symlinks are refused, `.git` skipped."""
    files: dict[str, bytes] = {}
    ignored: list[str] = []
    total = 0
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if rel == ".git" or rel.startswith(".git/"):
            continue
        if p.is_symlink():
            raise InvalidInput(f"MEMBER_SYMLINK: {rel!r}")
        if p.is_dir():
            continue
        if _is_os_metadata(rel):
            ignored.append(rel)
            continue
        code = okf.check_path(rel, okf_limits)
        if code:
            raise InvalidInput(f"{code}: {rel!r}")
        if not rel.lower().endswith(ALLOWED_SUFFIXES):
            raise InvalidInput(f"MEMBER_NOT_ALLOWED: {rel!r}")
        data = p.read_bytes()
        total += len(data)
        if total > okf_limits.max_bundle_bytes or len(files) >= okf_limits.max_files:
            raise InvalidInput("BUNDLE_TOO_LARGE")
        files[rel] = data
    return files, ignored


def read_source(source: str | Path | bytes) -> tuple[dict[str, bytes], list[str]]:
    if isinstance(source, bytes):
        return read_zip(source)
    path = Path(source)
    if path.is_dir():
        return read_dir(path)
    if path.stat().st_size > ARCHIVE_LIMITS.max_archive_bytes:
        raise InvalidInput("ARCHIVE_TOO_LARGE")
    return read_zip(path.read_bytes())


def detect_layout(files: dict[str, bytes]) -> tuple[str, str]:
    """(format, okf_root). Atlas: `atlas-manifest.json` beside a `bundle/` root. Otherwise plain OKF,
    rooted at the archive root, or at its single top-level directory when everything is under one."""
    if ATLAS_MANIFEST in files and any(p.startswith(ATLAS_ROOT + "/") for p in files):
        return "atlas", ATLAS_ROOT
    tops = {p.split("/", 1)[0] for p in files}
    if len(tops) == 1 and all("/" in p for p in files):
        return "okf", next(iter(tops))
    return "okf", ""


def _atlas_manifest(files: dict[str, bytes], report: ImportReport) -> None:
    try:
        manifest = json.loads(files[ATLAS_MANIFEST])
    except (ValueError, UnicodeDecodeError):
        raise InvalidInput("MANIFEST_INVALID") from None
    if not isinstance(manifest, dict) or manifest.get("atlas_extension") is not True:
        raise InvalidInput("MANIFEST_NOT_ATLAS")
    spec = manifest.get("specification") or {}
    report.manifest = {k: manifest.get(k) for k in ("okf_version", "bundle_root", "bundle_content_digest",
                                                    "content_snapshot_digest", "scope_digest", "captured_at", "counts")}
    report.manifest["specification"] = {k: spec.get(k) for k in ("revision", "sha256", "conformance")}
    if str(manifest.get("okf_version")) != okf.OKF_VERSION:
        report.warnings.append(f"OKF_VERSION_MISMATCH: bundle declares {manifest.get('okf_version')!r}")
    if spec.get("revision") != okf.OKF_SPEC_REVISION or spec.get("sha256") != okf.OKF_SPEC_SHA256:
        report.warnings.append("SPEC_PIN_MISMATCH: the bundle was written against another SPEC.md pin")
    # The manifest lists each bundle file's sha256; a mismatch means the bundle was edited or damaged.
    declared = {f.get("path"): f.get("sha256") for f in manifest.get("files") or [] if isinstance(f, dict)}
    for rel, sha in declared.items():
        data = files.get(f"{ATLAS_ROOT}/{rel}")
        if data is None:
            report.warnings.append(f"MANIFEST_FILE_MISSING: {rel}")
        elif hashlib.sha256(data).hexdigest() != sha:
            report.warnings.append(f"MANIFEST_DIGEST_MISMATCH: {rel}")


# ------------------------------------------------------------------------------------ import
def import_bundle(session: Session, workspace_id: str, source: str | Path | bytes, *, slug: str, author: str,
                  reason: str = "OKF import", origin: dict[str, Any] | None = None) -> ImportReport:
    files, ignored = read_source(source)
    if not any(okf.is_markdown(p) for p in files):
        raise InvalidInput("BUNDLE_EMPTY: no markdown documents")
    fmt, root = detect_layout(files)
    report = ImportReport(pack_id="", slug=slug, format=fmt, okf_root=root, revision=None, changed=False,
                          files=len(files), documents=0, ignored=ignored)
    if fmt == "atlas":
        _atlas_manifest(files, report)
    report.conformance = [p.as_dict() for p in okf.check_conformance(files, root)[:200]]
    policy = okf.check_publish_policy(files, root, allowed_extras=[ATLAS_MANIFEST])
    report.dangling_links = sum(1 for p in policy if p.code == "LINK_DANGLING")
    for path, data in files.items():
        if not okf.is_markdown(path) or path.rsplit("/", 1)[-1] in okf.RESERVED:
            continue
        try:
            meta, _ = okf.parse_frontmatter(path, okf.decode(path, data))
        except okf.OkfError:
            continue
        if meta and meta.get("type"):
            report.documents += 1
            report.verified_claims += 1 if okf.verified_entries(meta) else 0
            report.attested_computations += 1 if str(meta.get("type")) == "Attested Computation" else 0
    pack = store.imported_pack(session, workspace_id, slug, okf_root=root,
                               origin={**(origin or {}), "format": fmt})
    rev = store.commit(session, pack, files, author=author, reason=reason, origin="okf_import", system=True,
                       meta={"import": {k: v for k, v in report.as_dict().items() if k not in ("pack_id",)}})
    report.pack_id, report.revision, report.changed = pack.id, pack.head_revision, rev is not None
    if rev is not None:
        from analystos.events.bus import emit

        emit(workspace_id, "knowledge.imported", {"pack_id": pack.id, "slug": slug, "format": fmt,
                                                  "revision": pack.head_revision, "documents": report.documents},
             actor=author, session=session)
    return report


# ------------------------------------------------------------------------------------ export
def zip_bytes(files: dict[str, bytes]) -> bytes:
    """Deterministic archive: sorted names, fixed timestamp, fixed attributes and compression."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            zf.writestr(info, files[name], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return buf.getvalue()


def export_files(session: Session, pack: KnowledgePack, *, revision: int | None = None, enforce_policy: bool = True
                 ) -> dict[str, bytes]:
    files = store.revision_files(session, pack, revision)
    if not files:
        raise Conflict(f"knowledge pack {pack.slug} has no revision to export")
    if enforce_policy:
        problems = okf.check_publish_policy(files, pack.okf_root, allowed_extras=[ATLAS_MANIFEST])
        if problems:
            raise Conflict(f"publish policy refused pack {pack.slug}: " + "; ".join(
                f"{p.code} {p.path} {p.detail}".strip() for p in problems[:10]))
    return files


def export_zip(session: Session, pack: KnowledgePack, *, revision: int | None = None) -> bytes:
    return zip_bytes(export_files(session, pack, revision=revision))


def write_dir(files: dict[str, bytes], target: Path) -> None:
    for name, data in sorted(files.items()):
        out = (target / name).resolve()
        if target.resolve() not in out.parents:
            raise InvalidInput(f"PATH_TRAVERSAL: {name!r}")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
