"""Held-out evaluation corpus (P4-08, evaluation plan §1-§5).

`corpus.yaml` is the versioned task list with sealed rubrics; `generators.py` makes each task's data;
`runner.py` scores AnalystOS end to end on it (accepted outputs, confident-wrong, abstention
correctness, cost and latency per accepted output); `baseline.py` pairs those results with the
practitioner baseline (docs/60-delivery/06-practitioner-baseline-protocol.md). Disjoint from the
development benchmarks by construction; `tests/unit/test_heldout_corpus.py` guards that and the freeze.
"""
