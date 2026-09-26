"""Read-time dependency checks for saved metric answers; never imply statistical verification."""
from __future__ import annotations

from analystos.core.ids import stable_hash
from analystos.db.models import QueryExecution, SemanticMetric, SemanticModel
from analystos.governance.policy import get_workspace, load_policy


def evidence_status(session, turn, freshness: dict) -> dict:
    semantic = (turn.provenance or {}).get("semantic")
    if not semantic:
        return {"state": "ad_hoc", "reasons": ["This answer was not compiled from an approved metric."]}
    reasons = []
    model = session.get(SemanticModel, semantic["model_id"])
    if not model or model.workspace_id != turn.workspace_id or model.status != "approved" or model.content_hash != semantic["model_hash"]:
        reasons.append("The recorded semantic model is unavailable or no longer approved.")
    from analystos.semantic.service import current_model

    current = current_model(session, turn.workspace_id)
    if not current or current.id != semantic["model_id"]:
        reasons.append("The semantic model has a different current version.")
    for ref in semantic["metrics"]:
        metric = session.get(SemanticMetric, ref["id"])
        if not metric or metric.workspace_id != turn.workspace_id or metric.status != "approved" or metric.content_hash != ref["hash"]:
            reasons.append(f"Metric {ref['name']} version {ref['version']} is no longer the approved definition.")
    query = session.get(QueryExecution, (turn.result or {}).get("query_id"))
    if not query or query.workspace_id != turn.workspace_id or query.status != "ok":
        reasons.append("The successful query receipt is unavailable.")
    elif query.result_hash != (turn.result or {}).get("result_hash"):
        reasons.append("The saved result no longer matches its query receipt.")
    if stable_hash(turn.sql) != semantic["sql_hash"]:
        reasons.append("The SQL differs from the compiled definition.")
    # Policy content is pinned at execution by the caller, independently of the display label.
    policy = load_policy(session, get_workspace(session, turn.workspace_id))
    if semantic.get("policy_hash") and stable_hash(policy.model_dump(mode="json")) != semantic["policy_hash"]:
        reasons.append("Workspace policy has changed since this answer.")
    if freshness["state"] == "changed":
        reasons.append("Source data was refreshed after this answer.")
    if reasons:
        return {"state": "changed", "reasons": reasons}
    return {"state": "recorded", "reasons": [
        "Approved definition and saved query receipt match. This is not an independent statistical verification.",
        "Data freshness: " + freshness["label"],
    ]}
