# ADR-0008 — "Verified" is deterministic

**Status:** accepted

**Decision.** A finding is verified iff method fit, sample size, Benjamini–Hochberg-adjusted
significance, effect size, reproducible re-run (result hash) and an independent second method all
pass. The independent-family model review and JEV second opinion adjust confidence and add caveats
only. Causal wording is rewritten to associational template text.

**Consequences.** Model agreement can never promote a finding; model disagreement cannot hide a
reproducible one but lowers its confidence visibly.

## Amendment (2026-09-28, P8-15): verified is not confirmed; discovery vs held-out rows; strength

Seen live: on generated retail data where order status was assigned at random, "unit price differs by
status" passed every REV check (rank-biserial 0.1175 against the 0.10 minimum, q = 0.0028). Verified
means the deterministic checks passed on the data the run chose its hypotheses from; it does not mean the
pattern holds elsewhere. Three rules now apply on top of this ADR:

* **Discovery rows only.** Each analysed table is split by a stable hash of its row key (the catalog's
  approved, declared or measured-unique key; the whole readable row when none is known) into a discovery
  part and a held-out part (`analysis.holdout_fraction`, default 0.3; 0 turns it off). The partition is
  compiled by `skills/sqlbuild` into every query of the method (Postgres, DuckDB, T-SQL) and goes through
  the one gateway like any other SQL. Primary tests, the second method and follow-up rounds read only the
  discovery rows. The salt is fixed, so the same data always splits the same way (no re-drawing a
  holdout until a claim passes).
* **Lock, then read once.** After REV verifies a finding, its claim (method, spec hash, top group,
  baseline, direction) is locked with a timestamp, and the same test runs once on the held-out rows; the
  held-out predicate does not compile without a lock time, and a repeat review reuses the first answer.
  A claim over more than two plain categorical groups is tested as the comparison it states (top vs
  baseline); a two-group claim is judged one-sided in its locked direction (the same test at 2 x alpha,
  counted only for the same top group). Only a pass of the existing `holdout_partition` rule makes a
  finding `confirmed`; everything else stays `exploratory`.
* **Strength.** A supported result is `weak` when its effect is under 1.5x the method's minimum effect
  or its adjusted p-value is within 10x of alpha, `strong` at 3x and q < alpha / 100, else `moderate`
  (`evidence/strength.py`, thresholds from `skills/stats.py`). A weak or unconfirmed finding says so in
  its own sentence and in the run summary; the UI shows "weak evidence" and "confirmed on held-out
  data" / "not confirmed".

Limits, stated plainly: numbers in a finding describe the discovery rows (70% by default), and its
caveats say so; a pattern that is a fluke of the whole table is present in both parts and can still be
confirmed (in a simulation on the retail data the random-status difference was confirmed in about a third
of random splits), which is why strength is reported beside confirmation; a small table
(rows x fraction under the minimum sample) is not split and its findings stay exploratory.
