"""P7-16/P7-17 (ADR-0025) without services: the lite profile's defaults, features and extras reported as
"unavailable with a reason", capability manifests that require a profile or an extra, the numpy
replacements that keep core methods free of the `ml` extra (checked against statsmodels, which `dev`
installs), report formats and publishing without their extras/profiles."""
from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pytest

from analystos.core import profiles
from analystos.core.config import Settings
from analystos.core.errors import FeatureUnavailable


# ------------------------------------------------------------------------------------ profile defaults
def test_lite_fills_only_the_settings_the_environment_left_unset(monkeypatch):
    for var in ("ANALYSTOS_ORCHESTRATOR", "ANALYSTOS_REDIS_URL", "ANALYSTOS_SUPERSET_URL", "ANALYSTOS_LOCAL_WORKERS"):
        monkeypatch.delenv(var, raising=False)
    lite = Settings(_env_file=None, profile="lite")
    assert (lite.orchestrator, lite.redis_url, lite.superset_url) == ("local", "", "")
    assert lite.local_worker_count == 4 and lite.resume_local_runs and lite.run_inprocess_scheduler
    assert lite.spend_store == "postgres"
    assert profiles.active_features(lite) == {"demo"}  # no standard, no bi: preview publishing, local runs
    with_bi = Settings(_env_file=None, profile="lite", superset_url="http://superset:8088", redis_url="redis://r:6379/0")
    assert "bi" in profiles.active_features(with_bi) and with_bi.spend_store == "redis"


def test_standard_keeps_its_behaviour(monkeypatch):
    for var in ("ANALYSTOS_ORCHESTRATOR", "ANALYSTOS_REDIS_URL", "ANALYSTOS_SUPERSET_URL", "ANALYSTOS_LOCAL_WORKERS",
                "ANALYSTOS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    std = Settings(_env_file=None)
    assert std.profile == "standard" and std.orchestrator == "temporal" and std.redis_url
    assert std.local_worker_count is None and not std.resume_local_runs and not std.run_inprocess_scheduler
    assert std.spend_store == "redis"
    assert Settings(_env_file=None, spend_counter_store="postgres").spend_store == "postgres"


# ------------------------------------------------------------------------------------ extras and features
def test_a_missing_extra_is_reported_with_the_remedy(monkeypatch):
    monkeypatch.setattr(profiles, "_importable", lambda m: m not in ("sklearn", "statsmodels"))
    assert not profiles.has_extra("ml") and profiles.has_extra("reports")
    reason = profiles.extra_reason("ml")
    assert "`ml` extra" in reason and "analystos[ml]" in reason and "sklearn, statsmodels" in reason
    with pytest.raises(FeatureUnavailable) as exc:
        profiles.require_extra("ml", "logistic regression")
    assert exc.value.http_status == 501 and exc.value.details["missing"] == ["sklearn", "statsmodels"]
    assert profiles.summary(Settings(_env_file=None))["extras"]["ml"] is False


def test_requirement_reasons():
    lite = SimpleNamespace(orchestrator="local", superset_url="", graph_enabled=False, servicenow_mock_url="",
                           sandbox_isolation="auto", db_transaction_pooler=False)
    assert "`bi` profile" in profiles.requirement_reason("profile:bi", lite)
    assert "docker compose --profile bi" in profiles.requirement_reason("profile:bi", lite)
    assert profiles.requirement_reason("profile:bi", SimpleNamespace(**{**vars(lite), "superset_url": "http://s"})) is None
    assert profiles.requirement_reason("skill.row_count", lite) is None  # not an installation requirement
    assert "unknown profile" in profiles.requirement_reason("profile:nope", lite)


# ------------------------------------------------------------------------------------ capabilities
def test_capabilities_that_need_a_missing_profile_or_extra_are_unavailable_with_the_reason(monkeypatch):
    from analystos.capabilities import enablement, registry

    snap = registry.current()
    publish = snap.get("tool.superset_publish")
    driver = snap.get("method.driver_model")
    assert "profile:bi" in publish.requires and "extra:ml" in driver.requires
    monkeypatch.setattr(profiles, "_importable", lambda m: m not in ("sklearn", "statsmodels"))
    lite = SimpleNamespace(orchestrator="local", superset_url="", graph_enabled=False, servicenow_mock_url="",
                           sandbox_isolation="auto", db_transaction_pooler=False)
    monkeypatch.setattr("analystos.core.config.get_settings", lambda: lite)
    assert "`bi` profile" in registry.install_reason(publish)
    assert "`ml` extra" in registry.install_reason(driver)
    assert registry.install_reason(snap.get("skill.row_count")) is None
    reason = enablement.usable(driver, snap, {}, autonomous_run=False)
    assert reason.startswith("capability method.driver_model@1.0.0 is unavailable on this installation")


def test_an_unknown_install_requirement_fails_the_load():
    from analystos.capabilities import registry

    bad = {"apiVersion": "analystos/v1", "kind": "Skill", "id": "skill.needs_nothing_real", "summary": "x",
           "side_effect": "none", "requires": ["profile:mainframe"]}
    snap = registry.load(legacy=False, connectors=False, entry_points=False, packs_dir=None, strict=False,
                         extra=[("test", bad)])
    assert any("requires unknown profile:mainframe" in p for p in snap.problems)


def test_the_capability_api_lists_availability(monkeypatch):
    from analystos.api.routers import capabilities as api
    from analystos.capabilities import registry

    lite = SimpleNamespace(orchestrator="local", superset_url="", graph_enabled=False, servicenow_mock_url="",
                           sandbox_isolation="auto", db_transaction_pooler=False)
    monkeypatch.setattr("analystos.core.config.get_settings", lambda: lite)
    out = api._out(registry.current().get("tool.superset_publish"), True)
    assert out["available"] is False and "`bi` profile" in out["unavailable_reason"]
    assert api._out(registry.current().get("skill.row_count"), True)["available"] is True


def test_an_ml_method_is_refused_at_spec_validation_without_the_extra(monkeypatch):
    from analystos.agents import investigator

    monkeypatch.setattr(profiles, "_importable", lambda m: m not in ("sklearn", "statsmodels"))
    assert "needs the `ml` extra" in investigator._method_unavailable("driver_model")
    assert investigator._method_unavailable("rate_by_segment") is None


# ------------------------------------------------------------------------------------ numpy in place of statsmodels
def test_binomial_glm_matches_statsmodels():
    import statsmodels.api as sm

    from analystos.skills import stats as st

    rng = np.random.default_rng(3)
    n = rng.integers(50, 3000, size=6).astype(float)
    pos = np.floor(n * rng.uniform(0.02, 0.6, size=6))
    X = np.column_stack([np.ones(6), np.eye(6)[:, 1:]])
    params, cov, llf = st.binomial_glm(X, pos, n - pos)
    ref = sm.GLM(np.column_stack([pos, n - pos]), X, family=sm.families.Binomial()).fit()
    assert params == pytest.approx(ref.params, rel=1e-8, abs=1e-10)
    # statsmodels stops IRLS at a deviance tolerance of 1e-8, so its standard errors lag the converged fit slightly
    assert np.sqrt(np.diag(cov)) == pytest.approx(ref.bse, rel=1e-4)
    null = sm.GLM(np.column_stack([pos, n - pos]), np.ones((6, 1)), family=sm.families.Binomial()).fit()
    _, _, llf0 = st.binomial_glm(np.ones((6, 1)), pos, n - pos)
    assert 2 * (llf - llf0) == pytest.approx(2 * (ref.llf - null.llf), rel=1e-9)


def test_grouped_logistic_matches_the_statsmodels_fit():
    import statsmodels.api as sm

    from analystos.skills import stats as st

    groups = [{"segment": "0", "n": 2000, "positives": 240}, {"segment": "3+", "n": 1500, "positives": 540},
              {"segment": "1", "n": 1500, "positives": 190}, {"segment": "2", "n": 900, "positives": 150}]
    gl = st.grouped_logistic(groups, baseline="0", alpha=0.05)
    pos = np.array([g["positives"] for g in groups], float)
    neg = np.array([g["n"] - g["positives"] for g in groups], float)
    X = np.array([[1, 0, 0, 0], [1, 1, 0, 0], [1, 0, 1, 0], [1, 0, 0, 1]], float)
    ref = sm.GLM(np.column_stack([pos, neg]), X, family=sm.families.Binomial()).fit()
    ci = np.asarray(ref.conf_int(alpha=0.05))
    for i, seg in enumerate(("3+", "1", "2"), start=1):
        row = gl["odds_ratios"][seg]
        assert row["odds_ratio"] == pytest.approx(math.exp(ref.params[i]), rel=1e-7)
        assert row["ci_low"] == pytest.approx(math.exp(ci[i][0]), rel=1e-7)
        assert row["ci_high"] == pytest.approx(math.exp(ci[i][1]), rel=1e-7)
        assert row["p_value"] == pytest.approx(ref.pvalues[i], rel=1e-6, abs=1e-15)


def test_mantel_haenszel_matches_statsmodels_stratified_table():
    from statsmodels.stats.contingency_tables import StratifiedTable

    from analystos.skills import stats as st

    tables = [np.array([[30, 70], [20, 80]], float), np.array([[12, 40], [9, 51]], float),
              np.array([[55, 145], [40, 160]], float) + 0.5]
    mh = st.mantel_haenszel(tables, alpha=0.05)
    ref = StratifiedTable(tables)
    res = ref.test_null_odds(correction=False)
    lo, hi = ref.oddsratio_pooled_confint(alpha=0.05)
    assert mh["statistic"] == pytest.approx(res.statistic, rel=1e-10)
    assert mh["p_value"] == pytest.approx(res.pvalue, rel=1e-8)
    assert mh["odds_ratio"] == pytest.approx(ref.oddsratio_pooled, rel=1e-12)
    assert (mh["ci_low"], mh["ci_high"]) == (pytest.approx(lo, rel=1e-10), pytest.approx(hi, rel=1e-10))


def test_durbin_watson_matches_statsmodels():
    from statsmodels.stats.stattools import durbin_watson

    from analystos.skills import stats as st

    e = np.random.default_rng(9).normal(size=40)
    assert st.durbin_watson(e) == pytest.approx(float(durbin_watson(e)), rel=1e-12)


def test_core_methods_import_no_ml_library():
    """The investigate playbook's methods (rate, numeric, trend, pareto, correlation, cohort,
    contribution) must not import scikit-learn or statsmodels at module level or in their verification."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "analystos"
    offenders = []
    for path in [*sorted((root / "methods").glob("*.py")), root / "skills" / "stats.py"]:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for n in names:
                if n.split(".")[0] in ("sklearn", "statsmodels"):
                    offenders.append(f"{path.name}:{node.lineno}:{n}")
    # only the `ml`-extra functions (logistic_regression, feature_importance) import them, after require_extra
    assert all(o.startswith("stats.py") for o in offenders), offenders
    assert len(offenders) <= 5, offenders


# ------------------------------------------------------------------------------------ reports and publishing
def test_report_formats_that_need_the_reports_extra(monkeypatch):
    from analystos import reports

    monkeypatch.setattr(profiles, "_importable", lambda m: m not in ("matplotlib", "fpdf", "openpyxl"))
    missing = reports.unavailable_formats(("html", "pdf", "xlsx"))
    assert set(missing) == {"pdf", "xlsx"} and "`reports` extra" in missing["pdf"]
    with pytest.raises(FeatureUnavailable, match="PDF reports is unavailable"):
        reports.render(SimpleNamespace(), "pdf")


def test_publishing_defaults_to_preview_without_superset():
    from analystos.publishing.base import default_destination

    assert default_destination(["superset"], SimpleNamespace(superset_url="")) == "preview"
    assert default_destination(["superset", "preview"], SimpleNamespace(superset_url="http://s")) == "superset"
    assert default_destination([], SimpleNamespace(superset_url="http://s")) == "preview"


def test_spend_store_follows_configuration_not_runtime_outages():
    from analystos.runtime.budget_counters import BudgetCounters

    assert BudgetCounters("", "t:").spend_store == "postgres"
    down = BudgetCounters("redis://127.0.0.1:1/0", "t:")  # configured but down: redis, and it fails closed
    assert down.spend_store == "redis" and down.pg is None and not down.spend_available


def test_every_setting_is_documented_in_a_tier():
    """P7-17: the environment is documented in three tiers; a new setting must be placed in one."""
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[2] / "docs" / "30-runbooks" / "04-lite-and-profiles.md").read_text()
    missing = [f"ANALYSTOS_{name.upper()}" for name in Settings.model_fields if f"`ANALYSTOS_{name.upper()}`" not in doc]
    assert not missing, missing
    tier1 = doc.split("**Tier 1, required (3).**", 1)[1].split("**Tier 2", 1)[0]
    assert [v for v in ("ANALYSTOS_DATABASE_URL", "ANALYSTOS_JWT_SECRET", "ANALYSTOS_BOOTSTRAP_ADMIN_PASSWORD") if v in tier1] == [
        "ANALYSTOS_DATABASE_URL", "ANALYSTOS_JWT_SECRET", "ANALYSTOS_BOOTSTRAP_ADMIN_PASSWORD"]
