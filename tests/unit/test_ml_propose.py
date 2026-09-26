"""P5-01 contract, P5-04 method pack registration, P5-06 proposals, agents, playbooks and the rules-first
purpose; P5-03 parity and drift arithmetic. No services."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from analystos.contracts.work import MLSpec, WorkOrderSpec
from analystos.ml.propose import propose

COLUMNS = [
    {"name": "customer_id", "data_type": "text", "semantic_type": "id", "is_key": True, "distinct": 500},
    {"name": "region", "data_type": "text", "semantic_type": "categorical", "distinct": 4},
    {"name": "tenure_months", "data_type": "integer", "semantic_type": "numeric", "distinct": 59},
    {"name": "support_calls", "data_type": "integer", "semantic_type": "numeric", "distinct": 9},
    {"name": "email", "data_type": "text", "semantic_type": "text", "distinct": 500},
    {"name": "churned", "data_type": "boolean", "semantic_type": "boolean", "distinct": 2},
]


# ------------------------------------------------------------------------------------ the MLSpec contract
def test_mlspec_pins_everything_and_validates_allowlists():
    spec = MLSpec.model_validate({"task": "classify", "dataset": {"asset": "shop.customers"}, "target": "churned",
                                  "features": [{"column": "tenure_months"}], "entity_keys": ["customer_id"],
                                  "split": {"strategy": "group"}, "group_keys": ["customer_id"], "seed": 3,
                                  "estimators": ["logistic", "random_forest"], "objective_metric": "log_loss"})
    assert spec.baseline == "dummy_prior" and spec.candidates == ["logistic", "random_forest"] and spec.metric == "log_loss"
    assert spec.executable_problems() == []
    for bad in ({"estimators": ["xgboost"]}, {"objective_metric": "mae"}, {"split": {"strategy": "random"}},
                {"features": [{"column": "a"}, {"column": "a"}]}, {"k_range": [5, 2]}, {"unknown": 1}):
        with pytest.raises(ValidationError):
            MLSpec.model_validate({"task": "classify", **bad})
    assert "dataset" in " ".join(MLSpec(task="forecast").executable_problems())


def test_an_envelope_ml_work_order_still_validates_and_is_not_executable():
    wo = WorkOrderSpec.model_validate({"kind": "forecast", "objective": "Forecast weekly incident volume next month",
                                       "spec": {"type": "ml", "task": "forecast", "target_metric": "incident_count",
                                                "horizon": 4}})
    assert wo.executable is False and wo.spec.executable_problems()


# ------------------------------------------------------------------------------------ rules-first proposals
def test_rules_propose_a_valid_spec_from_the_objective_and_catalog():
    out = propose(COLUMNS, asset="shop.customers", row_count=500, objective="Which customers will churn next quarter?")
    p = out["proposal"]
    assert out["source"] == "rules" and out["problems"] == []
    assert p["task"] == "classify" and p["target"] == "churned" and p["entity_keys"] == ["customer_id"]
    feats = [f["column"] for f in p["features"]]
    assert "customer_id" not in feats and "churned" not in feats and "email" not in feats  # no keys, target, identifiers
    assert p["split"]["strategy"] == "random" and "one row per customer_id" in p["split"]["independence_justification"]
    MLSpec.model_validate(p)


def test_model_chosen_inputs_are_validated_not_trusted():
    out = propose(COLUMNS, asset="shop.customers", row_count=500, target="churned",
                  features=["tenure_months", "ghost_column"], estimators=["logistic", "deep_net"])
    assert out["proposal"]["estimators"] == ["logistic"] and [f["column"] for f in out["proposal"]["features"]] == ["tenure_months"]
    assert any("ghost_column" in p for p in out["problems"]) and any("deep_net" in p for p in out["problems"])
    bad = propose(COLUMNS, asset="shop.customers", target="not_a_column")
    assert bad["proposal"] is None and "not a visible column" in bad["problems"][0]


def test_repeated_keys_get_a_group_split():
    out = propose(COLUMNS, asset="shop.customers", row_count=2000, objective="predict churn")
    assert out["proposal"]["split"] == {"strategy": "group", "holdout_fraction": 0.2, "validation_folds": 3,
                                        "embargo_periods": 0} and out["proposal"]["group_keys"] == ["customer_id"]


# ------------------------------------------------------------------------------------ registration
def test_method_pack_agents_and_playbooks_register_and_stay_out_of_the_analysis_vocabulary():
    from analystos import methods
    from analystos.capabilities import registry

    snap = registry.load(entry_points=False)
    ml = [m for m in snap.list("Method") if (m.spec or {}).get("family") == "ml"]
    assert sorted(m.id for m in ml) == ["method.ml.anomaly", "method.ml.classify", "method.ml.cluster", "method.ml.forecast",
                                        "method.ml.regress"]
    assert all(m.requires == ["extra:ml"] and m.spec["baseline"] == m.spec["estimators"][0] for m in ml)
    assert not any(n.startswith("ml") for n in methods.names())
    agent = snap.get("agent.ml_practitioner")
    assert agent.entry == "builtin:generic" and agent.spec["capabilities"][0] == "skill.ml_propose_spec"
    assert agent.spec["default_actions"][0]["capability"] == "skill.ml_propose_spec"  # the off-mode path
    train, score = snap.get("playbook.train"), snap.get("playbook.score")
    assert [s["key"] for s in train.spec["steps"]] == ["ml_propose", "ml_train"]
    assert [s.get("type", "agent") for s in score.spec["steps"]] == ["approval_gate", "side_effect"]


def test_the_proposal_purpose_is_rules_first():
    from analystos.contracts.platform import DETERMINISTIC_CAPABLE
    from analystos.llm.config import load_models_config

    cfg = load_models_config()
    assert cfg.ladders["ml_spec_proposal"] == ["cache", "rules", "llm_small"] and "ml_spec_proposal" in DETERMINISTIC_CAPABLE


def test_ml_verdict_method_dependency_tracks_the_code():
    from analystos.ml.methods import code_digest, method_version

    assert method_version("ml.classify") and method_version("ml.nope") is None and len(code_digest()) == 64


# ------------------------------------------------------------------------------------ parity and drift arithmetic
def test_feature_parity_refuses_schema_drift():
    from analystos.services.ml import parity

    schema = [{"name": "tenure_months", "family": "numeric"}, {"name": "region", "family": "categorical"}]
    trained = {"tenure_months": "integer", "region": "text"}
    assert parity(schema, trained, {"tenure_months": "bigint", "region": "varchar", "customer_id": "text"}, ["customer_id"]) == []
    drift = parity(schema, trained, {"tenure_months": "text", "customer_id": "text"}, ["customer_id"])
    assert any("tenure_months was integer" in d for d in drift) and any("region is missing" in d for d in drift)


def test_population_stability_index():
    from analystos.ml.monitoring import feature_psi

    prof = {"family": "numeric", "edges": [1.0, 2.0, 3.0], "shares": [0.25, 0.25, 0.25, 0.25], "null_rate": 0.0}
    same = feature_psi(prof, [0.5, 1.5, 2.5, 3.5] * 25)
    shifted = feature_psi(prof, [3.5] * 90 + [0.5] * 10)
    assert same["psi"] < 0.01 and shifted["psi"] > 0.5
    cat = feature_psi({"family": "categorical", "shares": {"a": 0.5, "b": 0.5}, "other_share": 0.0}, ["c"] * 10)
    assert cat["psi"] > 1
