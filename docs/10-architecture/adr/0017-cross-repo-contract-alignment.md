# ADR-0017 — Cross-repository contract alignment (AnalystOS, Atlas, DataPilot)

**Status:** Proposed (2026-09-25, tracker P4-G01). A decision for the owners of the three
repositories; nothing is merged across repositories by this record. It becomes Accepted when each
owner has signed off below.

**Context.** Three products from the same owners now exchange artifacts or will:

| Repository | Product | Exchanges |
|---|---|---|
| `karuppusamym/AIAnalystAgentOS` (this one) | AnalystOS | OKF knowledge bundles (import from and export to Atlas), Atlas `get_knowledge_context` over MCP, decision records, hash-bound approvals |
| `karuppusamym/AIDataAnalyst` | Atlas | OKF bundles (the original exporter), knowledge context over MCP |
| `karuppusamym/AienginnerAgentOs` | DataPilot | decisions routed to JEV / a local scorer, plan-bound approvals |

Each repository grew its own version of three things that must agree if an artifact made by one is
to be trusted by another: which OKF specification text a bundle conforms to, what a model-assisted
decision is allowed to change, and how an approval is bound to what it approves. A read-only
survey on 2026-09-25 (local clones; file references below) found:

* **OKF pin — already aligned, but only by copy.** AnalystOS `src/analystos/knowledge/okf.py` and
  Atlas `src/aida/okf_export.py` both pin `SPEC.md` of
  `GoogleCloudPlatform/open-knowledge-format` at `0b87c52c6ef999286c745e19998fdfcd03d5dbee`
  (SHA-256 `26aa5da0…1030101`, OKF 0.2, status `SELF_CHECKED_AGAINST_PINNED_SPEC_CLAUSES`).
  Nothing makes a future bump in one repo visible to the other. DataPilot has no OKF support (its
  "OKF" row is Frictionless data packages, a different format).
* **Decision authority — same principle, different vocabularies.** AnalystOS has five authority
  classes enforced in code (`src/analystos/decisions/authority.py`, ADR-0015) and eleven purposes
  in `config/models.yaml`. DataPilot states the same rule in prose (`docs/AGENTS_TOOLS_AND_JEV.md`:
  "risk and approval stay deterministic, and Jev can never lower them") over purposes
  `decision_routing`, `risk_check`, `tool_selection`, `sql_candidate_judge`, without a machine-
  readable class per purpose. Atlas has no decision-model purposes.
* **Approval hash — compatible idiom, incompatible details.** AnalystOS binds an approval to
  `sha256(canonical_json(payload))` plus the plan hash and policy version
  (`governance/approvals.py`, `core/ids.py`). Atlas's canonical JSON (`src/aida/answer_provenance.py`
  `_canonical_json`) is byte-identical for JSON values, but no approval payload hash was found.
  DataPilot binds approvals to a plan hash over `{objective, steps[(agent, action)]}` with
  `ensure_ascii=False` (`apps/api/app/temporal_activities.py`), which differs byte-for-byte from
  AnalystOS for any non-ASCII text, and uses SHA-1 for grounding digests (`apps/api/app/grounding.py`).

**Decision (proposed).**

1. **One OKF profile pin, published in each repo's `contracts/` folder.** The pin is the tuple
   `{repository, path: SPEC.md, revision, sha256, okf_version, conformance_status}` with the
   values above. Each repository keeps its code constant and adds a test that the constant equals
   its `contracts/` copy (AnalystOS: `contracts/okf_profile_pin.json`, tested). A bump is one
   change proposed in all three repositories at once, with the new `SPEC.md` digest fetched from
   upstream; an importer refuses a bundle whose manifest names another revision (AnalystOS already
   does, `docs/10-architecture/okf-profile.md`).
2. **A shared decision-purpose schema:** `contracts/decision_purpose.schema.json` (JSON Schema
   2020-12). A purpose declares `kind` (choice | probability | scores), one `authority` class
   (`rank`, `choose_presentation`, `route`, `escalate_only`, `bounded_stop` — definitions in the
   schema), and ordered `backends` that must include `rules`; `escalate_only` and `bounded_stop`
   put `rules` first. No class can grant access, lower a risk, skip a gate or suppress an alert.
   The schema's `$defs.decision_record` is what each decision persists (enforced value, raw
   proposal, backend, `inputs_hash`, what enforcement changed). Purpose names stay per product;
   the classes and the record are shared, so a decision from one product can be audited by another.
3. **A shared approval-hash format:** `contracts/approval_hash.md` (version 1) — canonical JSON as
   AnalystOS produces it today (sorted keys, no whitespace, ASCII-escaped, Python float form), SHA-256
   lower-case hex, and the binding an execution must re-verify immediately before the side effect
   (payload hash, plan hash, policy version, both parties' current rights, expiry). Five test
   vectors and a `plan_hash` vector are included; every repo tests its implementation against them.
   RFC 8785 (JCS) was considered and not chosen now: it would change every stored AnalystOS hash
   and is not byte-identical to the current form (raw UTF-8, `1` for `1.0`). A move to JCS would be
   format version 2, with both versions accepted during a migration window.

**What each repository would need to adopt.**

| | AnalystOS (this repo) | Atlas | DataPilot |
|---|---|---|---|
| OKF pin | Done: `contracts/okf_profile_pin.json` + test against `knowledge/okf.py` | Add `contracts/okf_profile_pin.json` (the same file) and a test against `okf_export.py`'s constants | Nothing until it imports or exports OKF; then the same file and refusal rule |
| Decision purposes | Done: schema + test that every purpose in `config/models.yaml` validates and the enums equal the code's | Nothing today (no decision-model purposes). Adopt the schema when one is added | Declare `decision_routing` (route), `risk_check` (escalate_only, rules first), `tool_selection` (route), `sql_candidate_judge` (rank) in a machine-readable file validated by the schema; persist decisions in the `decision_record` shape |
| Approval hash | Done: `contracts/approval_hash.md` + vector test against `core/ids.py` and `runtime/plan.py` | Bind approvals that authorise a side effect (e.g. OKF import review, tool/product version approval) to `payload_hash` v1; its `_canonical_json` already matches | Switch `compute_plan_hash` to `ensure_ascii=True` (re-hash pending approvals once), add a `payload_hash` for each approval's executable payload, replace SHA-1 grounding digests with SHA-256 where they are compared across products |

**Consequences.** Artifacts that cross repositories (bundles, decision records, approval evidence)
can be verified by the receiver without trusting the sender's code. Each change to a shared
contract becomes a three-repository change, which is deliberate: the contracts are small and rarely
change. Nothing in this record moves code between repositories or makes one repository depend on
another at build or run time.

**Open questions for the owners.** (a) Where the canonical copy of `contracts/` lives (proposal:
each repo carries its own copy, with the three tested against the same vectors; no shared package).
(b) Whether DataPilot's plan-bound approval should also bind a policy version, as AnalystOS does.
(c) Whether Atlas wants decision records at all before it has a decision-model purpose.

**Sign-off.**

| Owner | Repository | Decision | Date |
|---|---|---|---|
| | AnalystOS | | |
| | Atlas | | |
| | DataPilot | | |
