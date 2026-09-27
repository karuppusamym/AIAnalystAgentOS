"""P5-07: `improved` needs a confirmed win. The holdout gain and its 95% interval are not enough: the search's
paired out-of-fold cross-validation must agree (majority of folds and on average) and the holdout interval at
the confirmation level must exclude zero. The rule is part of the spec (safe default, explicit opt-out), and a
coin-flip target that the unconfirmed rule promoted is no longer promoted."""
from __future__ import annotations

import numpy as np
import pytest

from analystos.contracts.work import ImprovementConfirmation, MLSpec
from analystos.ml import evaluation as V

pytest.importorskip("sklearn")

CONFIRM = ImprovementConfirmation()
PASSING_BOOT = {"ci_low": 0.02, "ci_high": 0.2, "confirm_low": 0.01, "confirm_level": 0.99}


def test_the_default_spec_confirms_and_the_old_rule_is_an_explicit_opt_out():
    spec = MLSpec.model_validate({"task": "classify"})
    assert spec.confirmation.cross_validation is True and spec.confirmation.holdout_level == 0.99
    assert V.confirm_level(spec) == 0.99
    old = MLSpec.model_validate({"task": "classify", "confirmation": {"cross_validation": False, "holdout_level": 0.95}})
    assert V.confirm_level(old) is None
    with pytest.raises(ValueError):
        MLSpec.model_validate({"task": "classify", "confirmation": {"holdout_level": 0.9}})  # never looser than 95%


def test_cross_validation_must_agree_in_a_majority_of_folds_and_on_average():
    ok = V.cv_confirmation("roc_auc", [0.62, 0.55, 0.61], [0.5, 0.5, 0.5])
    assert ok["passed"] and ok["positive"] == 3 and ok["folds"] == 3
    minority = V.cv_confirmation("roc_auc", [0.7, 0.45, 0.48], [0.5, 0.5, 0.5])
    assert not minority["passed"] and minority["positive"] == 1
    mean_negative = V.cv_confirmation("roc_auc", [0.51, 0.51, 0.2], [0.5, 0.5, 0.5])
    assert not mean_negative["passed"] and mean_negative["mean_gain"] < 0
    lower = V.cv_confirmation("mae", [9.0, 8.0, 12.0], [10.0, 10.0, 10.0])  # lower is better: 2 of 3 folds better
    assert lower["passed"] and lower["positive"] == 2
    assert not V.cv_confirmation("mae", [None, None], [1.0, 1.0])["passed"]  # nothing to confirm on


def test_a_holdout_win_the_folds_contradict_is_no_improvement():
    d = V.decide("roc_auc", 0.66, 0.5, PASSING_BOOT, 0.0, confirmation=CONFIRM, folds=([0.49, 0.48, 0.52], [0.5, 0.5, 0.5]))
    assert d["improved"] is False and "not confirmed" in d["reason"] and d["confirmation"]["passed"] is False
    assert d["confirmation"]["cross_validation"]["passed"] is False and d["confirmation"]["holdout"]["passed"] is True
    # without confirmation (the pre-P5-07 rule) the same evidence was an improvement
    assert V.decide("roc_auc", 0.66, 0.5, PASSING_BOOT, 0.0)["improved"] is True


def test_the_holdout_interval_at_the_confirmation_level_must_exclude_zero():
    boot = {**PASSING_BOOT, "confirm_low": -0.003}
    d = V.decide("roc_auc", 0.66, 0.5, boot, 0.0, confirmation=CONFIRM, folds=([0.6, 0.6, 0.6], [0.5, 0.5, 0.5]))
    assert d["improved"] is False and d["confirmation"]["holdout"] == {"level": 0.99, "ci_low": -0.003, "passed": False}
    good = V.decide("roc_auc", 0.66, 0.5, PASSING_BOOT, 0.0, confirmation=CONFIRM, folds=([0.6, 0.6, 0.6], [0.5, 0.5, 0.5]))
    assert good["improved"] is True and "confirmed" in good["reason"]


def test_the_bootstrap_reads_the_confirmation_bound_from_enough_resamples():
    rng = np.random.default_rng(3)
    c, b = rng.normal(1.0, 1.0, 300), rng.normal(0.0, 1.0, 300)

    def pair(idx):
        return float(c[idx].mean()), float(b[idx].mean())
    plain = V.paired_bootstrap("r2", pair, 300, seed=1)
    confirmed = V.paired_bootstrap("r2", pair, 300, seed=1, confirm_level=0.99)
    assert "confirm_low" not in plain and plain["resamples"] == V.BOOTSTRAP
    assert confirmed["resamples"] == V.CONFIRM_BOOTSTRAP and confirmed["confirm_level"] == 0.99
    assert confirmed["confirm_low"] < confirmed["ci_low"] < confirmed["ci_high"]


def test_a_coin_flip_target_the_old_rule_promoted_is_no_longer_promoted():
    """Seed 8076 of the held-out generator ended `improved` under the unconfirmed rule (P5-07 measurement)."""
    from evaluation.ml_null import UNCONFIRMED, _train

    old = _train("ml_pumps", "null_target", 8076, UNCONFIRMED)
    new = _train("ml_pumps", "null_target", 8076)
    assert old["verdict"] == "improved"
    assert new["verdict"] == "no_improvement" and "not confirmed" in new["reason"]
