"""validate_bundle, PreviewPublisher and the publisher factory."""
from __future__ import annotations

import pytest

from analystos.contracts.bi import ChartSpec, DashboardSpec, DatasetDef, MetricDef, PublishBundle
from analystos.core.config import Settings
from analystos.core.errors import InvalidInput
from analystos.publishing import BIPublisher, PreviewPublisher, get_publisher, validate_bundle
from analystos.publishing.preview import ensure_valid
from analystos.publishing.superset import SupersetPublisher


def bundle() -> PublishBundle:
    return PublishBundle(
        workspace_id="ws_1",
        destination="preview",
        datasets=[DatasetDef(name="inc", sql="SELECT 1", time_column="opened_at",
                             columns=[{"name": "opened_at"}, {"name": "priority"}, {"name": "hours"}])],
        metrics=[MetricDef(name="n", display_name="N", definition="", sql_expression="COUNT(*)")],
        charts=[
            ChartSpec(key="k", title="K", chart_type="kpi", intent="kpi", dataset="inc", metric="n"),
            ChartSpec(key="t", title="T", chart_type="line", intent="trend", dataset="inc", metric="n"),
        ],
        dashboards=[DashboardSpec(key="d", title="D", audience="executive", charts=["k", "t"],
                                  layout=[{"chart": "k", "row": 0, "col": 0, "width": 4},
                                          {"chart": "t", "row": 0, "col": 1, "width": 8}],
                                  native_filters=["priority"])],
    )


def test_valid_bundle_has_no_errors():
    assert validate_bundle(bundle()) == []
    ensure_valid(bundle())


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda b: setattr(b.charts[0], "dataset", "zzz"), "references unknown dataset 'zzz'"),
        (lambda b: setattr(b.charts[0], "metric", "zzz"), "references unknown metric 'zzz'"),
        (lambda b: setattr(b.charts[0], "metric", None), "(kpi) needs a metric"),
        (lambda b: b.dashboards[0].charts.append("nope"), "references unknown chart 'nope'"),
        (lambda b: b.dashboards[0].layout.pop(), "layout does not place chart 't'"),
        (lambda b: b.dashboards[0].layout.append({"chart": "k", "row": 2}), "places chart 'k' more than once"),
        (lambda b: b.dashboards[0].layout.__setitem__(0, {"chart": "k", "width": 13}), "must be an int in 1..12"),
        (lambda b: b.charts.append(b.charts[0].model_copy()), "duplicate chart key 'k'"),
        (lambda b: b.metrics.append(b.metrics[0].model_copy()), "duplicate metric name 'n'"),
        (lambda b: b.datasets.append(b.datasets[0].model_copy()), "duplicate dataset name 'inc'"),
        (lambda b: b.dashboards.append(b.dashboards[0].model_copy()), "duplicate dashboard key 'd'"),
        (lambda b: setattr(b.datasets[0], "time_column", None), "(line) needs dataset 'inc' to declare a time_column"),
        (lambda b: setattr(b.charts[1], "dimension", "ghost"), "dimension 'ghost' is not a column"),
        (lambda b: b.dashboards[0].native_filters.append("ghost"), "native filter 'ghost'"),
        (lambda b: setattr(b.charts[0], "key", "bad key!"), "must match"),
        (lambda b: setattr(b.charts[1], "chart_type", "pie"), "(pie) needs a dimension"),
        (lambda b: setattr(b.charts[1], "chart_type", "histogram"), "(histogram) needs a numeric dimension"),
        (lambda b: b.dashboards.clear(), "bundle has no dashboards"),
    ],
)
def test_validation_errors(mutate, expected):
    b = bundle()
    mutate(b)
    errors = validate_bundle(b)
    assert any(expected in e for e in errors), errors


def test_ensure_valid_raises_with_details():
    b = bundle()
    b.charts[0].metric = "zzz"
    with pytest.raises(InvalidInput) as info:
        ensure_valid(b)
    assert info.value.details["errors"]


def test_preview_publish_and_rollback():
    p = PreviewPublisher()
    assert isinstance(p, BIPublisher)
    res = p.publish(bundle(), idempotency_key="k")
    assert res.status == "succeeded" and res.destination == "preview"
    assert res.external_ids["charts"] == {"k": "preview:ws_1:chart:k", "t": "preview:ws_1:chart:t"}
    assert res.external_ids["dashboards"] == {"d": "preview:ws_1:dashboard:d"}
    assert res.urls == {"d": "preview://preview:ws_1:dashboard:d"}
    again = p.publish(bundle(), idempotency_key="k")
    assert again.external_ids == res.external_ids  # deterministic ids
    assert p.export_artifact("dashboard", res.external_ids["dashboards"]["d"])
    deleted = p.rollback(res.external_ids)
    assert len(deleted) == 4 and p.rollback(res.external_ids) == []
    with pytest.raises(NotImplementedError):
        p.schedule_report("x", cron="* * * * *")


def test_preview_publish_rejects_invalid():
    b = bundle()
    b.dashboards[0].charts.append("ghost")
    res = PreviewPublisher().publish(b, idempotency_key="k")
    assert res.status == "failed" and res.errors


def test_factory():
    assert isinstance(get_publisher("preview"), PreviewPublisher)
    sp = get_publisher("superset", Settings(superset_url="http://x:8088/"))
    assert isinstance(sp, SupersetPublisher) and isinstance(sp, BIPublisher)
    assert sp.base_url == "http://x:8088"
    with pytest.raises(InvalidInput, match="Phase 3"):
        get_publisher("powerbi")
    with pytest.raises(InvalidInput):
        get_publisher("tableau")
