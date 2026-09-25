from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from analystos.db.base import session_scope
from analystos.db.models import AuditEvent


def audit(actor: str, action: str, *, workspace_id: str | None = None, run_id: str | None = None,
          target: str | None = None, decision: str | None = None, reasons: list[str] | None = None,
          details: dict[str, Any] | None = None, session: Session | None = None) -> None:
    row = AuditEvent(actor=actor, action=action, workspace_id=workspace_id, run_id=run_id, target=target,
                     decision=decision, reasons=reasons or [], details=details or {})
    if session is not None:
        session.add(row)
        return
    with session_scope() as s:
        s.add(row)
