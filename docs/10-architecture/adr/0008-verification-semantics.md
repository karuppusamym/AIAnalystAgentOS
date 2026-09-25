# ADR-0008 — "Verified" is deterministic

**Status:** accepted

**Decision.** A finding is verified iff method fit, sample size, Benjamini–Hochberg-adjusted
significance, effect size, reproducible re-run (result hash) and an independent second method all
pass. The independent-family model review and JEV second opinion adjust confidence and add caveats
only. Causal wording is rewritten to associational template text.

**Consequences.** Model agreement can never promote a finding; model disagreement cannot hide a
reproducible one but lowers its confidence visibly.
