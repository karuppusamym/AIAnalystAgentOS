"""Evaluation harnesses (spec v3 §10): the analytical benchmark (P4-V01).

Kept outside the `analystos` package on purpose: the benchmark's ground truth names domain columns
(ServiceNow-style ITSM fields) and analysis methods outright, which the core must never do (the
tests in tests/unit/test_domain_packs.py and test_methods_registry.py guard that). It is a dev/CI
harness run from a checkout (scripts/benchmark_analytical.py), not part of the application image.
"""
