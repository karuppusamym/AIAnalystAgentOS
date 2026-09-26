# Ask accuracy benchmark — labelled question sets (P4-V02)

One file per domain (`itsm.yaml`, `sales.yaml`, `finance.yaml`), 54 questions each (162 in all),
over the V01 seeded datasets (`evaluation/datasets.py`, seed 1, with effects) plus one restricted
column per domain that the benchmark workspace's policy denies (`caller_email`, `customer_email`,
`vendor_iban`). The harness is `evaluation/ask.py`; the command is `scripts/benchmark_ask.py`.

## How the set was written

Written by hand on 2026-09-25 by the engineer building the harness (a Claude session), in this
order, before any tier had been run:

1. **The registry** (`registry:` in each file): nine verified queries per domain, written from the
   table alone as an analyst would have promoted them — plain aggregates by the obvious segments,
   a monthly series, and parameterised lookups (number, enum, date). It stands in for the
   workspace's verified-query registry; the benchmark seeds it directly (each rendered template is
   checked by the gateway validator first) instead of promoting it through Ask.
2. **The questions**, against that registry and the table, in four outcome classes:
   * `answer` (35 per domain) with a **gold SQL** in PostgreSQL over the staged table (`@table`).
     Tags say how each question relates to the registry: `registry_exact` (a registry phrasing),
     `paraphrase` (same meaning, other words), `parameter` (a value for a parameterised entry),
     `near_miss` (worded close to an entry but needing different SQL: an extra filter or group, a
     different aggregate, another parameter value where the entry hard-codes one) and `novel`
     (nothing in the registry covers it: the model's job).
   * `needs_input` (5): a required value is missing (which priority, which region, which dates).
     Four match a parameterised entry without its value; one is only recognisable by meaning.
   * `clarify` (5): nothing definite to measure ("Is it getting better?").
   * `decline` (9): `out_of_scope` (no such data), `write` (a change request) and `restricted`
     (the restricted column is the only way to answer). Each has a `probe_sql` — what a careless
     model might write — which the `fake` tier sends so the gateway's refusal is exercised.
3. A design-time pass checked only the structure (`evaluation.ask.problems`: ids, classes, gold
   present exactly for answers, probes read the restricted column) and that every gold SQL runs.
   No question, label or gold statement was changed after a tier's results were seen.

Gold SQL is the minimal answer: the columns the question asks for. Execution match allows extra
answer columns, compares rows order-insensitively with numeric tolerance, and counts a scale
difference (a percentage for a fraction) as wrong. Rows returned by a top-1 question were checked
for ties at write time; a tie would make the gold ambiguous.

## Rules for changing it

* The **pilot threshold is set by the owner before the pilot**, against a named version of this
  set (the git revision of this folder). The reports only propose placeholder values.
* Do not edit questions, labels or gold to make a tier pass. A wrong label found later is fixed in
  its own commit that says why, and every report after it names the new revision.
* Add questions by appending new ids; never reuse an id.
* The set is development data for the harness and the pilot; it is not a sealed hold-out (see
  `docs/60-delivery/05-evaluation-plan.md` §1). A sealed set for a release gate is written
  separately and kept out of prompts, fixtures and this repository's examples.
