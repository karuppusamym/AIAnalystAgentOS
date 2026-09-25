# ADR-0015 — Decision service, authority classes and JEV calibration

**Status:** proposed (2026-09-25, review C9). Amends ADR-0006.

**Context.** JEV (`typesafe/jev-1.13` via the OpenRouter Decisions API, `/api/alpha/`) is cheap and
typed, but it does more than escalate:

- `stop_check` ends investigations;
- `chart_selection` overrides charts;
- `feedback_classification` routes replans.

Some calls are wasted or harmful. `risk_check` at publish has no possible effect, and
`alert_triage` escalated noise to critical.

Probabilities are not persisted, and model calls cannot be replayed. The endpoint is alpha, with a
single vendor and no published calibration; one third party reports about 15% timeouts
[unverified]. Air-gapped self-hosted installs cannot reach it at all.

Increment 3 (ADR-0010) added a `rules` path for `hypothesis_priority` and `stop_check` under
`auto` mode. That is the first non-JEV backend; this ADR formalises it.

**Decision.**
1. **`DecisionService.decide(purpose, state, question)`** with backends `jev`, `rules`,
   `local_classifier` and `llm_structured`. Each purpose declares its ordered backends, a timeout
   (default 3 s), a circuit breaker and a fallback.
2. **Authority classes per purpose:**
   - `rank` — order valid options; no calibration needed.
   - `choose_presentation` — pick among rule-valid presentations; no calibration needed.
   - `escalate_only` — raise risk or ask the user; no calibration needed.
   - `route` — needs calibration.
   - `bounded_stop` — only after the minimum work; needs calibration.

   No backend may grant, lower a risk, skip a gate or suppress an alert.
3. **Every decision is persisted**: options, answer, probabilities, backend, latency, cost and
   trusted-state hash. `model_call` also keeps a redacted prompt and response hash, plus stored
   payloads, so decisions can be replayed.
4. **Calibration.** A nightly Brier/ECE calculation per purpose and backend, over labelled
   outcomes: accepted or rejected findings, acknowledged or dismissed alerts, and feedback
   classes corrected by users. A purpose below its threshold drops to the next backend, visibly.
5. **Changes to purposes.**
   - Drop `risk_check` at publish.
   - `chart_selection` → rules; JEV only as a tie-break.
   - `stop_check` → rule first; JEV only after the minimum number of rounds.
   - `alert_triage` → deterministic materiality rules first (completeness, minimum effect,
     deduplication).
   - Add `ask_route`, `clarify_needed`, `metric_match` and `join_path_choice`.

**Consequences.** JEV remains the default backend where it is cheap and typed. The product no
longer depends on an alpha endpoint for correctness, and air-gapped installs work on rules or a
local classifier. Calibration needs labelled outcomes, so the UI has to capture accept, reject and
dismiss signals (P4-U03, P4-U06).

**Implementation (P4-T08, P4-T09; 2026-09-25).**

- `src/analystos/decisions/`: `service.py` (`DecisionService.decide(purpose, state, question, facts=…)`),
  `authority.py` (the five classes, enforced in code), `backends.py` (`jev`, `rules`,
  `local_classifier`, `llm_structured`), `rules.py`, `breaker.py`, `store.py`, `calibration.py`.
  Purposes, classes, backend order and timeouts are declared under `decisions:` in
  `config/models.yaml`; admin knobs (backend order override, thresholds, pins) are
  `PlatformSettings.decisions`.
- `state` goes to models; `facts` (rounds, materiality inputs, match scores) stay in the platform and
  feed rules and authority checks only.
- `escalate_only` = `max(rule baseline, model proposal)` on the purpose's level scale.
  `rev_second_opinion` is `escalate_only`: a model may add doubt (−0.1 confidence) but no longer adds
  credit (+0.05 removed). `alert_triage`: an immaterial signal (too few periods, effect below
  `monitors.min_material_effect`) is not sent to JEV; a JEV "not material" answer no longer blocks
  auto-investigation.
- `local_classifier` is a keyword/bigram logistic–softmax model with fixed weights in
  `config/decisions/local_classifier.yaml` (no network, no ML dependency). It is available per
  purpose through the admin backend order; the default chains do not use it except `ask_route`.
- `risk_check` at publish is left to P4-T07 (removal); the feedback path uses the service.
- Calibration: labels from finding accept/reject/dismiss, alert acknowledge/investigate/dismiss and
  feedback-class corrections (`decision_outcome`); nightly in the scheduler (advisory lock, once per
  24 h) or `analystos calibrate`; report at `GET /api/admin/decisions/calibration`; downgrade/restore
  rows in `decision_calibration` (audited). `rules` is the last resort and is never downgraded.
