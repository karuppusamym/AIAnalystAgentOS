# P4-05 — live analyst workflow over HTTP (2026-09-27)

**Row:** P4-05 (tracker), the remaining "live analyst workflow". **Test:**
`tests/integration/test_semantic_analyst_workflow.py` (marked `integration`), FastAPI `TestClient` against the real
stack: Postgres 16 + pgvector (control plane `analystos_test_b1`, analytics plane `analystos_test_dp_b1_analytics`),
Redis, local orchestrator, no model key (deterministic paths only). Users are the seeded `analyst@`, `approver@` and
`admin@analystos.local` (`analystos seed`), each logged in through `POST /api/auth/login`.

## Flow and outcome (run 2026-09-27, 1 passed in 32 s)

| # | Step (HTTP) | Who | Outcome |
|---|---|---|---|
| 1 | `POST /semantic/model/proposals` — datasets `orders` (pk `order_id`) and `customers` (pk `customer_id`), entity and grain in each description | analyst | v1 `proposed`, origin `user_proposal`. A key column the table lacks (`order_no`) → **422**; the approver (below editor) → **403** |
| 2 | `GET /semantic/model/diff?version=1`, `POST /semantic/model/approve` | analyst, then approver | analyst → **403** (separation of duties); approver → `approved`, `decided_by` = approver |
| 3 | `POST /semantic/relationships/candidates` orders.customer_id → customers.customer_id | analyst | measured through the gateway: `many_to_one`, `pending` |
| 4 | accept the candidate (semantic route and `/api/approvals/{id}/approve`) | analyst, then approver | analyst → **403** on both doors; approver → `accepted`; structure v2 `approved` with the join `validated_by` = approver |
| 5 | `POST /semantic/metrics` `sales_amount = SUM(amount)` on `orders`, dimension `customers.segment`; approve | analyst, then approver | analyst → **403**; approver → v1 `approved`, owner = analyst |
| 6 | notebook step `semantic_query {metrics: [sales_amount], dimensions: [customers.segment]}` | analyst | governed compile, `ok`, verification record `ACTIVE`, dependency `semantic:<ws>/sales_amount` at v1 |
| 7 | propose + approve `sales_amount` v2 | analyst, approver | v1 `deprecated` (superseded), v2 `approved`; the v1 step's record **VOID (semantic)**; a re-run step depends on v2 |
| 8 | `POST /semantic/metrics/sales_amount/deprecate` | approver | every live version `deprecated`; the v2 step's record **VOID (semantic)**; a new step on the KPI fails: *"Every requested metric must have an approved definition."* |
| 9 | `POST /semantic/ownership` (model → admin), accept | analyst offers; approver, then admin | approvals inbox cannot approve the offer; the approver cannot accept (**403**, not the named owner); admin accepts → model `owner_id` = admin; a second accept → **409** (single use) |

## Fix made for this workflow

A person had no HTTP way to *propose* entities and grain for review: the only human structure paths (Ossie/dbt
import, relationship acceptance) save an `approved` version directly. Added `POST
/api/workspaces/{ws}/semantic/model/proposals` (`semantic/review.propose_structure`, editor role): each dataset must
read a catalogued, active table of the workspace, its primary/unique key columns must exist, fields without an
expression must be columns; joins are refused here (they go through the measured relationship queue, so no request sets
a cardinality). The version is `proposed` and is approved only through `/model/approve` by someone other than the
proposer (hash-bound approval, `verify_for_execution`).

## Summary written by the test (`ANALYSTOS_EVIDENCE_OUT`)

```json
{"structure": {"version": 1, "proposed_by": "analyst", "approved_by": "approver"},
 "join": {"relationship": "orders__customers__customer_id", "cardinality": "many_to_one", "structure_version": 2},
 "analysis_v1": {"verdict": "verified", "state": "ACTIVE"},
 "analysis_v2": {"v1_step_state": "VOID (semantic)"},
 "deprecation": {"rerun_status": "failed", "rerun_error": "invalid_input: Every requested metric must have an approved definition."},
 "ownership": {"subject": "model", "from": "analyst", "to": "admin"}}
```
(ids replaced by roles; the raw file carries the workspace, step and approval ids of the run.)
