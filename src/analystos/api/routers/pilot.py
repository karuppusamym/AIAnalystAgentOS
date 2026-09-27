"""Named owners and controlled-pilot readiness (P4-09; `services/pilot.py`).

* `PUT /workspaces/{id}/owners`, `PUT /workspaces/{id}/sources/{source_id}/owners` — the business and technical
  owner (workspace owners only; audited, with an event).
* `GET /workspaces/{id}/pilot-readiness` — what the pilot still lacks, check by check.
* `GET /admin/pilot-readiness` — every active workspace's verdict (administrators).
* `GET /admin/sso`, `POST /admin/sso/preview` — the group -> role mapping as applied, its dead grants, and what a
  set of IdP groups would grant (administrators; nobody is signed in).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from analystos.api.deps import admin_user, current_user, db
from analystos.contracts.pilot import OwnersIn, PilotReadiness
from analystos.db.models import User
from analystos.services import pilot as svc

router = APIRouter(prefix="/api", tags=["pilot"])


@router.put("/workspaces/{workspace_id}/owners")
def put_workspace_owners(workspace_id: str, body: OwnersIn, user: User = Depends(current_user),
                         session: Session = Depends(db, scope="function")) -> dict:
    ws = svc.set_workspace_owners(session, session.merge(user), workspace_id, body)
    return {"workspace_id": ws.id, "owners": ws.owners}


@router.put("/workspaces/{workspace_id}/sources/{source_id}/owners")
def put_source_owners(workspace_id: str, source_id: str, body: OwnersIn, user: User = Depends(current_user),
                      session: Session = Depends(db, scope="function")) -> dict:
    src = svc.set_source_owners(session, session.merge(user), workspace_id, source_id, body)
    return {"source_id": src.id, "owners": src.owners}


@router.get("/workspaces/{workspace_id}/pilot-readiness", response_model=PilotReadiness)
def get_pilot_readiness(workspace_id: str, user: User = Depends(current_user),
                        session: Session = Depends(db, scope="function")) -> PilotReadiness:
    return svc.readiness(session, session.merge(user), workspace_id)


@router.get("/admin/pilot-readiness")
def get_pilot_overview(user: User = Depends(admin_user), session: Session = Depends(db, scope="function")) -> list[dict]:
    return svc.overview(session, session.merge(user))


class GroupsIn(BaseModel):
    groups: list[str] = Field(default_factory=list, max_length=200)


@router.get("/admin/sso")
def get_sso(user: User = Depends(admin_user), session: Session = Depends(db, scope="function")) -> dict:
    """The single sign-on configuration as the platform will apply it: the group -> role mapping and the grants
    that name a workspace that does not exist (they grant nothing). No secret is returned."""
    from analystos.core.config import get_settings
    from analystos.security import oidc

    s = get_settings()
    mapping = oidc.load_mapping()
    return {"enabled": oidc.enabled(), "issuer": s.oidc_issuer, "client_id": s.oidc_client_id,
            "groups_claim": s.oidc_groups_claim, "platform_admin_groups": mapping.platform_admin_groups,
            "workspace_roles": [{"group": g.group, "workspace": g.workspace, "role": g.role} for g in mapping.workspace_roles],
            "attributes": mapping.attributes, "require_group": mapping.require_group,
            "problems": oidc.mapping_problems(session, mapping)}


@router.post("/admin/sso/preview")
def preview_sso(body: GroupsIn, user: User = Depends(admin_user), session: Session = Depends(db, scope="function")) -> dict:
    """What a sign-in with these identity-provider groups would grant; nobody is signed in or changed."""
    from analystos.security import oidc

    return oidc.preview(session, body.groups)
