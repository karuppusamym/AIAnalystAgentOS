# Agent contract (FND-006)

An agent is a `kind: Agent` capability manifest (spec v3 §3.4): `config/agents/*.yaml`, a pack, or an
entry point. The manifest is the contract's only source.

* The body is `capabilities/agents.py:AgentBody`; unknown fields fail the load, and so do unknown
  capabilities, tools, model purposes, output types, output schemas and knowledge sections
  (`capabilities/validation.py`). Its JSON Schema is `contracts/agent_manifest.schema.json`.
* `contracts/registry.py:AgentSpec` is a **view** of the manifest, made only by
  `capabilities/agents.py:to_agent_spec` (`contracts/agent.schema.json`). The runtime (`RunContext.agent`,
  the Ask/console `AdhocContext.agent`), the tool gate and `GET /api/agents` all use it.
* `agent_definition` rows cache the view and own one thing: the platform-wide `enabled` switch. Seeding and
  `POST /api/admin/capabilities/reload` rewrite the cached spec whenever it differs from the manifest,
  including when the version is unchanged. `get_agent_spec` and the artifact gate derive the contract from
  the manifest and never read the cached spec. The only exception is an orphan row whose manifest no longer
  exists.
* `GET /api/agents` lists every registered Agent manifest (pack agents included) as the view plus
  `enabled`. `POST /api/agents` and `PATCH /api/agents/{id}` with a `spec` are refused (`invalid_input`,
  `reason: manifest_only`); to change a contract, edit the manifest and reload. `PATCH` with `enabled` sets
  the switch; it creates the row for a pack agent that has none.
* A run uses the manifest versions it bound (`run.capabilities.manifests`), so a reload never changes the
  contract of a run already in flight.

`tests/unit/test_agent_contract.py::test_admin_api_and_rows_derive_from_the_manifest_never_the_row` checks
that every field the API shows equals the manifest, even when the row's spec has been edited by hand.

## Fields

| Field | Status | Where |
|---|---|---|
| `id`, `version` | Enforced: bound into the plan hash; the run keeps the bound version | `capabilities/binding.py` |
| `entry`, `behaviours` | Enforced: select the code a step runs (`builtin:generic` = the generic runtime) | `agents/dispatch.py` |
| `certification.status` | Enforced: autonomous runs use certified agents only | `capabilities/enablement.py` |
| `capabilities` | Enforced: validated at load, bound into the plan hash, the only actions the generic runtime accepts | `agents/generic.py` |
| `tools` | Enforced: tool gate binding (`bound=True`) | `tools/registry.py:ToolRuntime.authorize` |
| `model_purpose`, `model_purposes` | Enforced: the only purposes `llm_json` routes for the agent | `agents/common.py` |
| `budget` (`llm_calls`, `usd`, `queries`, `max_steps`) | Enforced per task execution | `runtime/context.py`, `agents/generic.py` |
| `policies` | Enforced: tighten `pii_access`, `max_iterations`, `max_rows_extract`; `approval_for_publish` cannot be false | `runtime/context.py`, `AgentBody` validator |
| `default_actions` | Enforced: the deterministic path of a generic agent | `agents/generic.py` |
| `output` | Enforced: where a generic agent persists its result; implies an `output_contract` entry | `agents/generic.py` |
| **`knowledge`** | **Enforced** (below) | `agents/common.py:knowledge_scope`, `context/compiler.py` |
| **`output_contract`** | **Enforced** (below) | `capabilities/agents.py:enforce_output` |
| `prompt_version` | Informational: recorded on every model call (provenance) | `RunContext.call_ctx` |
| `phase` | Informational after seeding: sets the initial `enabled` switch (non-`mvp` agents start disabled) | `tools/registry.py:sync_agent_definitions` |
| `role`, `goal` | Informational for Python agents (display name and description); the generic runtime sends them in its prompt | `agents/generic.py:propose` |
| `summary`, `tags`, `determinism`, `side_effect`, `cost_class` | Informational for agents: each action the agent takes is governed by *that* capability's manifest | — |

### Knowledge contract

```yaml
knowledge:
  sections: [glossary, business_rules, metrics]   # spec v3 spelling `purposes:` also loads
  budget_chars: 12000                              # optional
```

The context compiler is the only path from run context to a model prompt. `compile_for` intersects each purpose
profile's knowledge sections (`glossary`, `business_rules`, `metrics`, `prior_findings`, `negative_knowledge`,
`episodes`, `external`) with the calling agent's `knowledge.sections`. Undeclared sections are neither loaded
nor compiled. `budget_chars` caps the characters that all knowledge sections together may add; items past the
cap are listed as omitted, with reason `agent knowledge budget`.

An agent without a `knowledge` block receives no knowledge sections. The catalog is not knowledge: what an
agent may see of the data is its authorized scope. Callers without an agent contract (feedback
interpretation, the knowledge preview endpoint) use the purpose profile unchanged. Ask and the SQL console
carry the `sql` agent's contract.

### Output contract

```yaml
output_contract:
  - type: chart                          # an artifact type, or a run record: hypothesis | experiment | insight | publication
    schema: contract:bi.ChartSpec        # a Pydantic contract under analystos.contracts
  - type: quality_report
    schema: {type: object, required: [issues]}   # or an inline JSON Schema
  - type: profile                        # no schema: any JSON object
```

The check runs immediately before the write, so a refused output is never persisted:

* **Artifacts:** `save_artifact(..., run_id, creator_agent)` checks the contract of the version the run bound.
  An undeclared type, content that fails its schema, or an agent with no manifest raises
  `OutputContractViolation`. This is `policy_denied`/403 with `code: output_contract_violation` and
  `details.reason` set to `undeclared`, `schema` or `no_manifest`. The check follows the `artifact.write`
  tool gate. Artifacts a signed-in user writes (`creator_user`) are governed by their route instead.
* **Run records:** `RunContext.check_output(type, content)` runs before each insert. The investigator checks
  `hypothesis` (`contract:analysis.AnalysisSpec`); a spec that fails the schema is dropped, with its reason
  recorded as an agent decision. The data scientist and the critic check `experiment`, insight checks
  `insight`, and the publisher checks `publication` (`contract:bi.PublishBundle`, before the side effect).
* A generic agent's `output` declares `GENERIC_OUTPUT_SCHEMA` for its artifact type unless `output_contract`
  already lists that type.

Tests: `tests/unit/test_agent_contract.py` covers the view, the API, load-time validation, knowledge scoping
and budget, and output refusals for artifacts and run records, including the bound version. It uses SQLite
and makes no model calls.
