"""Analysis methods as plugins (spec v3 §3.5, P4-X04).

Everything the platform says or checks about the closed analysis vocabulary is derived here from the
method registry, never hand-edited elsewhere: the vocabulary list, the prompt vocabulary block, the
`AnalysisSpec` method enum and JSON Schema, method-specific validation, the insight template, the chart
intent, the run-diff claim key and the hypothesis identity. Adding a method is one module plus one
`kind: Method` manifest (built in: `src/analystos/methods/<name>.{py,yaml}`; third party: an entry point).
"""
from __future__ import annotations

from typing import Any

from analystos.methods.base import AnalysisMethod, AnalysisOutcome, ChartIntent, Method, Rows, column_type
from analystos.methods.registry import current, reset

__all__ = ["AnalysisMethod", "AnalysisOutcome", "ChartIntent", "Method", "Rows", "all_methods", "column_type", "current",
           "for_playbook", "get", "manifests", "names", "refs", "reset", "schema_extra", "vocabulary_block"]


def names() -> list[str]:
    """The closed vocabulary: every registered method name, in vocabulary order."""
    return current().names()


def get(name: str) -> Method:
    return current().get(name)


def all_methods() -> list[Method]:
    return [get(n) for n in names()]


def manifests() -> dict[str, Any]:
    return dict(current().manifests)


def refs() -> list[str]:
    """id@version of every method (what a plan binding the vocabulary would hash)."""
    return [m.ref for m in current().manifests.values()]


def for_playbook(role: str) -> Method | None:
    """The method that fills a role of the domain-neutral hypothesis playbook (first in vocabulary order)."""
    return next((m for m in all_methods() if m.playbook == role), None)


def vocabulary_block() -> str:
    """The `methods:` block of the hypothesis prompt."""
    return "methods:\n" + "\n".join(f"- {m.name}: {m.vocabulary}" for m in all_methods())


def schema_extra(schema: dict[str, Any]) -> None:
    """JSON Schema of `AnalysisSpec.method`: the registered names as an enum, each described."""
    schema["enum"] = names()
    schema["description"] = "A registered analysis method:\n" + "\n".join(
        f"{n}: {current().manifests[n].summary}" for n in names())
