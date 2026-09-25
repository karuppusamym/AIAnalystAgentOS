---
name: e2e-demo
description: Run the v1 §62 MVP definition-of-done scenario end to end through the HTTP API (ServiceNow → findings → dashboards → approval → Superset) and write a dated evidence report. Use when asked to prove, demo, or re-verify the MVP.
---

Prerequisites: the stack from the `run-stack` skill, `OPENROUTER_API_KEY` in the environment.

```bash
.venv/bin/python scripts/e2e_demo.py --api http://localhost:8000
```

The script logs in as analyst/approver, creates a workspace, registers the ServiceNow source,
selects incident + change_request (+ lookup tables), starts a run, injects a redirect before
publication, approves as the approver, waits for Superset publication, checks every DoD item and
writes `docs/60-delivery/evidence/e2e-<timestamp>.md` + `.json`. Record the file in the
capability register; do not paste results into the tracker without the evidence link.
