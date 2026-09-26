# Approval hash format (version 1)

Proposed as a shared contract in [ADR-0017](../docs/10-architecture/adr/0017-cross-repo-contract-alignment.md).
Source of truth in AnalystOS: `core/ids.py` (`canonical_json`, `stable_hash`),
`governance/approvals.py` (`request_approval`, `verify_for_execution`) and `runtime/plan.py`
(`plan_hash`). `tests/unit/test_cross_repo_contracts.py` recomputes every vector below.

## 1. Canonical JSON

The bytes that are hashed are the UTF-8 encoding of the JSON text produced by Python's
`json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)`:

* object keys sorted by code point, recursively; no whitespace anywhere;
* every non-ASCII character escaped as `\uXXXX` (lower-case hex; UTF-16 surrogate pairs above U+FFFF),
  so the text is pure ASCII;
* integers as decimal digits; floats as Python `repr` (shortest round-trip), which keeps a trailing
  `.0` on an integral float (`1.0`, not `1`) and uses `1e+16`-style exponents;
* `true`, `false`, `null`; arrays keep their order.

**Portability rules for producers** (so another language can reproduce the bytes): put only JSON
types in a payload; write timestamps as ISO-8601 strings yourself (`default=str` would otherwise
emit Python's `2025-03-01 00:00:00+00:00` form); avoid NaN/Infinity; do not rely on the integral
float form — send an integer where the value is an integer. A JavaScript or Go producer needs a
custom serializer (sorted keys, ASCII escaping, the float rule); RFC 8785 (JCS) is **not**
byte-identical (it emits raw UTF-8 and `1` for `1.0`).

## 2. Hashes

| Name | Definition |
|---|---|
| `payload_hash` | `sha256_hex(canonical_json(payload))`, 64 lower-case hex characters |
| `plan_hash` | `sha256_hex(canonical_json({"plan", "constraints", "scope", "version"[, "capabilities"]}))` — `scope` is the scope hash, `capabilities` the sorted `id@version` list, omitted when empty |
| `inputs_hash` (decisions) | `sha256_hex(canonical_json({"purpose", "state", "facts", "question"}))` |

## 3. What an approval binds

An approval record carries: `action`, `destination`, `affected_assets`, the exact `payload` and its
`payload_hash`, `plan_hash` (or null), `policy_version`, `requested_by`, `risk_tier`, `expires_at`.
It is valid for execution only when, **immediately before the side effect**:

1. its status is `approved` and it has not expired;
2. `payload_hash` equals the hash of the payload about to be executed;
3. `plan_hash` equals the current plan hash (null only for actions outside a run);
4. the workspace `policy_version` is the one it was requested under;
5. the requester still holds the right to request it and the approver the right to approve it
   (and, where separation of duties applies, they differ).

Any mismatch invalidates the approval; a new one is required. There is no partial match.

## 4. Test vectors

```json
[
  {"payload": {}, "canonical": "{}",
   "sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"},
  {"payload": {"b": 1, "a": [3, 2, 1]}, "canonical": "{\"a\":[3,2,1],\"b\":1}",
   "sha256": "3ce2bce0ad5f581d642ebf7b59c400e6f60fa4416e9fb08e3061bb0a35f6fb2b"},
  {"payload": {"action": "publish_dashboard", "destination": "superset",
               "dashboard": {"title": "Incident SLA", "charts": ["chart_1", "chart_2"]}, "affected_assets": ["sn.incident"]},
   "canonical": "{\"action\":\"publish_dashboard\",\"affected_assets\":[\"sn.incident\"],\"dashboard\":{\"charts\":[\"chart_1\",\"chart_2\"],\"title\":\"Incident SLA\"},\"destination\":\"superset\"}",
   "sha256": "a2e60fab1a7b24282a43104d60cd6d836cd848235ee05f32d5dd938b6f616cdf"},
  {"payload": {"name": "Café Zürich", "amount": 12.5, "ratio": 1.0, "count": 3, "flag": true, "missing": null},
   "canonical": "{\"amount\":12.5,\"count\":3,\"flag\":true,\"missing\":null,\"name\":\"Caf\\u00e9 Z\\u00fcrich\",\"ratio\":1.0}",
   "sha256": "1b56ed0add8b2c9b801b8a1afa511a4e8f5d0333a38fbd1cda7efbfe9f1f3862"},
  {"payload": {"when": "2025-03-01T00:00:00+00:00", "tags": [], "nested": {"z": {"y": {"x": 0.1}}}},
   "canonical": "{\"nested\":{\"z\":{\"y\":{\"x\":0.1}}},\"tags\":[],\"when\":\"2025-03-01T00:00:00+00:00\"}",
   "sha256": "4418771eb4e66656fe5e77a9f6804c047d9780c7d232bc62ecf25ad4f050f61e"}
]
```

`plan_hash` vector: plan `{"tasks": [{"key": "profile", "agent": "profiler"}]}`, constraints
`{"filters": []}`, scope hash `"0" * 64`, version 1, capabilities
`["playbook.investigate@1", "agent.profiler@1"]` →
`1dca967fcb8f34d3f54e8168885d2a7e448fac50ac3d8d974e4c57f84a38c68b`.
