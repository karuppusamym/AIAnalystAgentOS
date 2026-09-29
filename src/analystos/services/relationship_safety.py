"""Admission of a person-declared lookup requires fresh, whole-table key evidence."""
from __future__ import annotations

from sqlalchemy.orm import Session

from analystos.db.models import Relationship, SourceAsset


def admissible(session: Session, relationship: Relationship, target: SourceAsset) -> bool:
    if relationship.validated:
        return True
    if relationship.origin != "user":
        return False
    meta = (target.stats or {}).get("profile_meta") or {}
    if meta.get("sampled") or meta.get("truncated"):
        return False
    from analystos.services.crawler import profile_reusable
    from analystos.services.platform_settings import get as platform

    if not profile_reusable(target, platform().crawl.profile_reuse_hours):
        return False
    from analystos.services.brief import key_uniqueness

    fq = f"{target.schema_name}.{target.name}"
    return key_uniqueness(session, relationship.workspace_id, fq, [relationship.to_column])["state"] == "unique"
