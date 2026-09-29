from types import SimpleNamespace

import pytest

from analystos.core.errors import Conflict, Forbidden, InvalidInput
from analystos.services import existing_dashboard
from analystos.services.existing_dashboard import _safe_inspection


def dashboard(**changes):
    return {
        "id": 21, "title": "Operations", "slug": "operations", "url": "/superset/dashboard/21/",
        "published": True, "datasets": [{"id": 4, "name": "incidents", "schema": "public",
                                          "database": "Customer", "sql": "SELECT secret FROM x",
                                          "columns": [], "metrics": []}],
        "charts": [{"id": 5, "name": "By team", "adhoc_filters": [
            {"subject": "region", "operator": "IN", "comparator": ["sensitive value"]}]}],
        "filters": [], "metrics": [], "layout": [], **changes,
    }


def test_import_snapshot_maps_unique_asset_and_excludes_query_and_filter_values():
    assets = [SimpleNamespace(id="asset_1", name="incidents", schema_name="public")]
    result = _safe_inspection(dashboard(), "ws_one", assets)
    assert result["workspace_mapping"] == [{"dataset_id": 4, "asset_id": "asset_1", "state": "mapped"}]
    assert result["datasets"][0]["has_virtual_query"] is True
    assert "secret" not in str(result)
    assert "sensitive value" not in str(result)
    assert result["charts"][0]["adhoc_filters"] == [
        {"subject": "region", "operator": "IN", "expressionType": None}]


def test_import_snapshot_marks_ambiguous_asset_without_guessing():
    assets = [SimpleNamespace(id=i, name="incidents", schema_name="public") for i in ("a", "b")]
    result = _safe_inspection(dashboard(), "ws_one", assets)
    assert result["workspace_mapping"][0] == {"dataset_id": 4, "asset_id": None, "state": "ambiguous"}


@pytest.mark.parametrize("changes", [
    {"slug": "aos-ws-foreign-operations"},
    {"datasets": [{"id": 4, "name": "aos_ws_foreign_incidents"}]},
    {"charts": [{"id": 5, "name": "aos_ws_foreign_chart"}]},
])
def test_import_snapshot_rejects_foreign_workspace_objects(changes):
    with pytest.raises(Forbidden):
        _safe_inspection(dashboard(**changes), "ws_one", [])


def test_import_persists_snapshot_and_links_only_unique_mapping(monkeypatch):
    snapshot = _safe_inspection(dashboard(), "ws_one", [SimpleNamespace(id="asset_1", name="incidents",
                                                                         schema_name="public")])
    calls = []
    user = SimpleNamespace(id="user_1")
    artifact = SimpleNamespace(id="art_1", version=2, platform=None, external_id=None, external_url=None)
    monkeypatch.setattr(existing_dashboard, "require_role", lambda *a: calls.append("role"))
    monkeypatch.setattr(existing_dashboard, "inspect", lambda *a, **k: snapshot)
    monkeypatch.setattr(existing_dashboard, "save_artifact", lambda *a, **k: artifact)
    monkeypatch.setattr(existing_dashboard, "link", lambda *a: calls.append(a[2:]))
    monkeypatch.setattr(existing_dashboard, "audit", lambda *a, **k: calls.append(k["details"]))

    result = existing_dashboard.import_dashboard(object(), user, "ws_one", 21)

    assert result is artifact
    assert ("artifact", "art_1") in calls[1]
    assert ("source_asset", "asset_1") in calls[1]
    assert artifact.external_id == "21" and artifact.platform == "superset"
    assert calls[2] == {"superset_id": 21, "version": 2}


def test_snapshot_discards_sql_expressions_even_inside_metrics():
    raw = dashboard(
        datasets=[{"id": 4, "name": "incidents", "schema": "public", "sql": "SELECT 'private'",
                   "metrics": [{"name": "count", "expression": "COUNT(CASE WHEN email = 'secret' THEN 1 END)"}]}],
        charts=[{"id": 5, "name": "By team", "metrics": ["count", "SUM(CASE WHEN name='secret' THEN 1 END)"]}],
        metrics=[{"name": "count", "expression": "COUNT(CASE WHEN email = 'secret' THEN 1 END)",
                  "dataset_id": 4}],
    )
    result = _safe_inspection(raw, "ws_one", [])
    assert "secret" not in str(result) and "private" not in str(result)
    assert result["charts"][0]["metrics"] == ["count", "[ad-hoc metric]"]


def test_approved_title_change_updates_same_dashboard_and_artifact_version(monkeypatch):
    before = _safe_inspection(dashboard(), "ws_one", [])
    after = {**before, "title": "New operations"}
    art = SimpleNamespace(id="art_1", workspace_id="ws_one", type="dashboard", platform="superset",
                          external_id="21", external_url=before["url"], name="Imported Superset dashboard 21",
                          content={"origin": "superset_import", "snapshot": before}, content_hash="hash_1", version=1)
    approval = SimpleNamespace(workspace_id="ws_one", action=existing_dashboard.TITLE_ACTION,
                               payload={"artifact_id": "art_1", "artifact_hash": "hash_1", "dashboard_id": "21",
                                        "previous_title": "Operations", "new_title": "New operations"})
    session = SimpleNamespace(get=lambda *a, **k: approval)
    called = []
    publisher = SimpleNamespace(rename_existing_dashboard=lambda *a: called.append(a))
    monkeypatch.setattr(existing_dashboard, "require_role", lambda *a: None)
    snapshots = iter([before, after])
    monkeypatch.setattr(existing_dashboard, "inspect", lambda *a, **k: next(snapshots))
    monkeypatch.setattr(existing_dashboard, "verify_for_execution", lambda *a, **k: called.append(k["payload"]))
    monkeypatch.setattr(existing_dashboard, "consume", lambda *a: called.append("consumed"))
    monkeypatch.setattr(existing_dashboard, "save_artifact", lambda *a, **k: SimpleNamespace(id="art_1", version=2))
    monkeypatch.setattr(existing_dashboard, "audit", lambda *a, **k: None)

    result = existing_dashboard.apply_title_change(session, SimpleNamespace(id="user_1"), art, "apr_1",
                                                    publisher=publisher)

    assert called[0]["artifact_hash"] == "hash_1"
    assert called[1:] == ["consumed", (21, "New operations")]
    assert result.version == 2


def editable_snapshot():
    return _safe_inspection(dashboard(
        revision="rev_1", datasets=[{"id": 4, "name": "incidents", "schema": "public", "database": "Customer",
                                     "columns": [{"name": "region"}], "metrics": [{"name": "count"}]}],
        charts=[{"id": 5, "name": "By team", "datasource": "4__table", "metrics": ["count"],
                 "groupby": ["team"]}], layout=[{"chart_id": 5, "width": 6, "height": 50}],
    ), "ws_one", [SimpleNamespace(id="asset_1", name="incidents", schema_name="public")])


@pytest.mark.parametrize(("proposal", "expected"), [
    ({"operation": "chart_metric", "chart_id": 5, "metric": "count"}, "count"),
    ({"operation": "chart_groupby", "chart_id": 5, "groupby": ["region"]}, ["region"]),
    ({"operation": "native_filter", "dataset_id": 4, "column": "region"}, "region"),
    ({"operation": "layout_size", "chart_id": 5, "width": 8, "height": 60}, 8),
])
def test_bounded_existing_dashboard_edits(proposal, expected):
    edit = existing_dashboard._validated_edit(editable_snapshot(), proposal)
    assert expected in edit.values()


@pytest.mark.parametrize("proposal", [
    {"operation": "chart_metric", "chart_id": 99, "metric": "count"},
    {"operation": "chart_metric", "chart_id": 5, "metric": "raw SQL expression"},
    {"operation": "chart_groupby", "chart_id": 5, "groupby": ["secret"]},
    {"operation": "native_filter", "dataset_id": 4, "column": "secret"},
    {"operation": "layout_size", "chart_id": 5, "width": 13, "height": 60},
])
def test_existing_dashboard_edit_rejects_unowned_or_unbounded_fields(proposal):
    with pytest.raises(InvalidInput):
        existing_dashboard._validated_edit(editable_snapshot(), proposal)


def test_existing_dashboard_edit_detects_superset_revision_conflict(monkeypatch):
    before = editable_snapshot()
    art = SimpleNamespace(id="art_1", workspace_id="ws_one", type="dashboard", platform="superset",
                          external_id="21", external_url="/superset/dashboard/21/", name="Imported Superset dashboard 21",
                          content={"origin": "superset_import", "snapshot": before}, content_hash="hash_1", version=1)
    edit = {"operation": "layout_size", "chart_id": 5, "width": 8, "height": 60}
    approval = SimpleNamespace(workspace_id="ws_one", action=existing_dashboard.EDIT_ACTION,
                               payload={"artifact_id": "art_1", "artifact_hash": "hash_1", "dashboard_id": "21",
                                        "revision": "rev_1", "edit": edit, "rationale": "Widen this chart"})
    monkeypatch.setattr(existing_dashboard, "require_role", lambda *a: None)
    monkeypatch.setattr(existing_dashboard, "verify_for_execution", lambda *a, **k: None)
    monkeypatch.setattr(existing_dashboard, "inspect", lambda *a, **k: {**before, "revision": "changed"})
    with pytest.raises(Conflict, match="changed in Superset"):
        existing_dashboard.apply_edit(SimpleNamespace(get=lambda *a, **k: approval), SimpleNamespace(id="user_1"),
                                      art, "apr_1", publisher=SimpleNamespace())
