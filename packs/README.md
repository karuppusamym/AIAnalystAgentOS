# Domain packs

A domain pack holds what AnalystOS knows about one business domain, as data (spec v3 §3.6,
ADR-0011). The core engine is domain-neutral: it names no table or column of any domain, and
`tests/unit/test_domain_packs.py` fails if a ServiceNow column name reappears in `src/analystos`.

| Pack | Domain | Auto-enables for tables named |
|---|---|---|
| `itsm` | IT service management (ServiceNow-shaped incidents, changes, CMDB) | `incident`, `change_request`, `problem`, `cmdb_ci*`, ... |
| `sales` | Retail and B2B sales orders | `order(s)`, `sales_order(s)`, `order_line(s)`, `sales`, ... |

`analystos packs` lists the installed packs.

## Layout

```
packs/<name>/
  pack.yaml            kind: KnowledgePack manifest (id pack.<name>); `spec` points at the files below
  hints.yaml           naming vocabulary merged into the deterministic skills
  templates.yaml       hypothesis templates: AnalysisSpec patterns over column slots
  kpis.yaml            starter KPIs over the templates' outcomes
  knowledge/*.md       glossary terms, metrics and business rules (OKF-like: YAML frontmatter + body)
  benchmark/           dataset generator, planted effects and null controls
```

Only `pack.yaml` carries `apiVersion: analystos/v1`, so the capability registry validates it like any
other manifest. A `spec` value may be inline data or a path relative to the pack directory (paths
cannot leave it). A pack installed as a Python entry point (`analystos.capabilities`) inlines its data.

## Two scopes

* **Hints** come from every installed pack. They only add vocabulary to a core default, and the
  crawler needs them before any workspace has enabled anything:
  `acronyms` (labels), `key_columns` (surrogate keys: join targets and must-be-unique keys),
  `display_columns` (name columns of referenced tables), `event_start` (words naming a record's start
  time), `lifecycle_pairs` (timestamps that must be ordered), `domain_keywords` (table-domain
  classification), `person_nouns` (a `<noun>_name` column names a person: PII).
* **Templates and knowledge** come from the packs a run enables: the workspace policy's
  `domain_packs` list when set (an empty list disables packs), otherwise every pack whose
  `applies_when` matches the run's selected tables or columns (`{tables: [regex], columns: [regex]}`,
  full match, case-insensitive). The investigator records the enabled pack versions on its decision.

## Templates

A template never names a column. Slots bind columns by semantic type, crawler role, name pattern and
profile bounds; outcomes and segments are derivations over slots; a template combines them into
`AnalysisSpec`s with question and statement text. The full format is in the docstring of
`src/analystos/skills/hypothesis_templates.py`. Example (from `sales`):

```yaml
slots:
  amount: {type: numeric, role: amount, not_name: count, limit: 1}
  dims: {type: categorical, distinct: [2, 30], exclude_text: true, not_role: [identifier, foreign_key, text]}
  customer_dim: {from: dims, name: "(segment|tier|customer_type)", limit: 1}
outcomes:
  order_value: {kind: column, slot: amount}
templates:
  - id: order_value_by_customer_type
    method: numeric_by_segment
    outcome: order_value
    segment: {slot: customer_dim}
    priority: high
    question: "Does {outcome} differ by {segment}?"
    statement: "{Outcome} differs materially across {segment}."
```

Template proposals go through the same validation as a model's (`investigator.validate_spec`:
authorized scope, denied columns, method/type fit), so a pack cannot widen what a run may read. The
core role-driven playbook (measures and flags by segment, a trend, matrix follow-ups) still runs after
the pack templates, and covers any database with no pack at all.

## Knowledge documents

```markdown
---
kind: term            # term | metric | rule
name: "SLA breach"
synonyms: ["missed SLA", "SLA violation"]
maps_to: [incident.made_sla]
---
An incident that did not meet its service-level agreement target (made_sla = false).
```

`analystos seed` loads every installed pack's documents into the global glossary (`origin: pack:<name>`),
skipping names that already exist.

## Benchmark

`benchmark/benchmark.yaml` names a generator (`python:<module>:<callable>` or `<file>.py:<callable>`
in the benchmark folder), optional `prepare` SQL, the tables to analyse, the planted effects (a spec
pattern plus the expected top segment) and null controls (columns that must never yield a finding,
plus explicit specs that must be rejected). `tests/benchmarks/test_pack_benchmarks.py` runs every
pack's benchmark through the deterministic pipeline with no model and no services, so CI runs it on
every push. `tests/integration/test_pack_sales_run.py` runs the full local orchestrator on the sales
benchmark as a SQLite source.
