"""Grounding suite (P7-07, spec v4 §13): published numbers are bound to computed facts, fabricated
values are never accepted, and findings are voided when the data they were computed on changes.

Deterministic, no services. Cases come from the component tier of the analytical benchmark (seeded
ITSM / sales / finance datasets -> DuckDB -> every valid proposal tested, evaluation/analytical.py):

* **bound share**: every numeric mention of every method's deterministic finding text (title +
  finding, `agents.insight.template_text`) must bind to a typed fact (`evidence.facts.bind_finding`);
* **fabricated values**: each bound number, one at a time, is replaced by a nearby wrong value (x1.37,
  never within 2% of any computed fact in any unit); the altered text must NOT bind. `fabricated_accepted`
  counts the ones that did (gate: 0);
* **swaps**: the top and baseline groups exchanged, or the direction word reversed (higher/lower,
  increase/decrease, rose/fell), must not bind either (`swapped_accepted`, gate: 0);
* **void on change**: data-version cases (content changed, rows removed, a legacy load re-staged)
  must void the finding; identical content re-staged, another asset changing and a pushdown source
  must not. Each registered checker is scored: `staleness` (P4-03 `evidence.manifest.freshness`) today;
  verification-record voiding (P7-01) plugs in with `register_void_checker` and is gated the same way.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SEEDS = (1, 2)
DOMAINS = ("itsm", "sales", "finance")
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_DIRECTIONS = {"higher": "lower", "lower": "higher", "increase": "decrease", "decrease": "increase",
               "increased": "decreased", "decreased": "increased", "rose": "fell", "fell": "rose",
               "increasing": "decreasing", "decreasing": "increasing"}
_DIR_RE = re.compile(r"\b(" + "|".join(_DIRECTIONS) + r")\b", re.I)


# ------------------------------------------------------------------------------------ void on change
@dataclass(frozen=True)
class VoidCase:
    name: str
    recorded: dict[str, Any]  # snapshot the finding was computed on (entry_from's `snapshot`)
    current: dict[str, Any] | None  # the asset's snapshot now (None = unchanged)
    must_void: bool
    mode: str = "staged"
    asset: str = "bench.incident"
    changed_asset: str | None = None  # a change to another asset than the finding's


_BASE = {"rows_staged": 20000, "content_fingerprint": "c" * 64, "sampling_method": "full", "load_id": "load_1",
         "staged_at": "2026-09-26T10:00:00"}
_LEGACY = {"rows_staged": 5, "staged_at": "t1", "load_id": "l1"}
VOID_CASES: tuple[VoidCase, ...] = (
    VoidCase("content changed", _BASE, {**_BASE, "content_fingerprint": "d" * 64, "load_id": "load_2"}, True),
    VoidCase("rows removed", _BASE, {**_BASE, "content_fingerprint": "e" * 64, "rows_staged": 19000}, True),
    VoidCase("legacy load re-staged", _LEGACY, {**_LEGACY, "staged_at": "t2", "load_id": "l2"}, True),
    VoidCase("identical content re-staged", _BASE, {**_BASE, "load_id": "load_9", "staged_at": "2026-09-27T10:00:00"}, False),
    VoidCase("another asset changed", _BASE, {**_BASE, "content_fingerprint": "f" * 64}, False,
             changed_asset="bench.change_request"),
    VoidCase("pushdown source", {}, {}, False, mode="pushdown"),
)

VoidChecker = Callable[[VoidCase], bool]


def _staleness(case: VoidCase) -> bool:
    """P4-03: a finding whose recorded data version is no longer current is stale (needs re-verification)."""
    from analystos.evidence import manifest as M

    recorded = M.entry_from(case.asset, "s1", case.mode, snapshot=case.recorded)
    bundle = {"data": {"manifest": {"entries": [recorded.model_dump(mode="json")]}}}
    asset = case.changed_asset or case.asset
    now = M.entry_from(asset, "s1", case.mode, snapshot=case.current if case.current is not None else case.recorded)
    return M.freshness(bundle, {asset: now}).state == "stale"


VOID_CHECKERS: dict[str, VoidChecker] = {"staleness": _staleness}


def register_void_checker(name: str, checker: VoidChecker) -> None:
    """P7-01 (VerificationRecord voiding) registers here; every registered checker is gated."""
    VOID_CHECKERS[name] = checker


# ------------------------------------------------------------------------------------ results
@dataclass
class GroundingResult:
    findings: int = 0
    findings_bound: int = 0
    numeric_mentions: int = 0
    numeric_bound: int = 0
    fabricated_variants: int = 0
    fabricated_accepted: int = 0
    swapped_variants: int = 0
    swapped_accepted: int = 0
    narratives: int = 0
    narratives_correct: int = 0
    adversarial_accepted: int = 0
    methods: dict[str, int] = field(default_factory=dict)
    void: dict[str, dict[str, int]] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)  # examples, for the report

    def metrics(self) -> dict[str, Any]:
        void_recall = {n: (v["voided"] / v["must_void"]) if v["must_void"] else 1.0 for n, v in self.void.items()}
        return {
            "findings": self.findings,
            "findings_bound_share": _share(self.findings_bound, self.findings),
            "numeric_mentions": self.numeric_mentions,
            "bound_numeric_share": _share(self.numeric_bound, self.numeric_mentions),
            "fabricated_variants": self.fabricated_variants,
            "fabricated_accepted": self.fabricated_accepted,
            "swapped_variants": self.swapped_variants,
            "swapped_accepted": self.swapped_accepted,
            "narratives": self.narratives,
            "narratives_correct_share": _share(self.narratives_correct, self.narratives),
            "adversarial_accepted": self.adversarial_accepted,
            "methods_covered": len(self.methods),
            "void_on_change_recall": min(void_recall.values()) if void_recall else 0.0,
            "void_false_positives": sum(v["false_void"] for v in self.void.values()),
            "void_checkers": sorted(self.void),
        }


def _share(a: int, b: int) -> float:
    return round(a / b, 6) if b else 0.0


# ------------------------------------------------------------------------------------ variants
def _near(value: float, facts: list[Any]) -> bool:
    for f in facts:
        v = getattr(f, "value", None)
        if not isinstance(v, int | float):
            continue
        for scaled in (v, v * 100, v * 24, v / 24, v * 60):
            if abs(value - scaled) <= 0.02 * max(abs(scaled), 1e-9):
                return True
    return False


def _perturb(token: str) -> tuple[str, float] | None:
    m = _NUM.search(token)
    if m is None:
        return None
    raw = m.group(0)
    value = float(raw.replace(",", ""))
    decimals = len(raw.split(".")[1]) if "." in raw else 0
    wrong = value * 1.37 + (1 if value == 0 else 0)
    text = f"{wrong:,.{decimals}f}" if "," in raw else f"{wrong:.{decimals}f}"
    if float(text.replace(",", "")) == value:
        return None
    return token[:m.start()] + text + token[m.end():], wrong


def number_variants(text: str, binding: Any, facts: list[Any]) -> list[str]:
    """One variant per bound number: that number replaced by a wrong one, everything else unchanged."""
    out = []
    for mention in binding.mentions:
        if not mention.fact_id or mention.text not in text:
            continue
        altered = _perturb(mention.text)
        if altered is None or _near(altered[1], facts):
            continue
        out.append(text.replace(mention.text, altered[0], 1))
    return out


def swap_variants(text: str, stat: dict[str, Any]) -> list[str]:
    """The two compared groups exchanged, and the direction word reversed (each on its own)."""
    out = []
    hl = stat.get("highlights") or {}
    top, base = hl.get("top_segment"), hl.get("baseline_segment")
    if top and base and str(top) != str(base):
        a, b = str(top), str(base)
        pattern = re.compile(rf"(?<![\w.]){re.escape(a)}(?![\w])|(?<![\w.]){re.escape(b)}(?![\w])")
        if len(pattern.findall(text)) >= 2 and a in text and b in text:
            out.append(pattern.sub(lambda m: b if m.group(0) == a else a, text))
    m = _DIR_RE.search(text)
    if m is not None:
        word = m.group(0)
        flipped = _DIRECTIONS[word.lower()]
        out.append(text[:m.start()] + (flipped.capitalize() if word[0].isupper() else flipped) + text[m.end():])
    return out


# ------------------------------------------------------------------------------------ the suite
CASES_FILE = Path(__file__).resolve().parent / "grounding_cases.yaml"


def _score(result: GroundingResult, spec: dict[str, Any], stat: dict[str, Any], bind: Callable[..., Any]) -> None:
    """One finding: its deterministic text must bind; its fabricated and swapped variants must not."""
    from analystos import methods
    from analystos.agents.insight import template_text

    facts = methods.get(spec["method"]).facts(spec, stat)
    title, finding = template_text(stat, spec)
    text = f"{title}\n{finding}"
    b = bind(spec, stat, facts, text)
    result.findings += 1
    result.findings_bound += int(b.ok)
    result.numeric_mentions += len(b.mentions)
    result.numeric_bound += sum(1 for m in b.mentions if m.fact_id)
    result.methods[spec["method"]] = result.methods.get(spec["method"], 0) + 1
    if not b.ok:
        result.failures.append(f"unbound {spec['method']}: {text!r}: {b.problems[:2]}")
    for variant in number_variants(text, b, facts):
        result.fabricated_variants += 1
        if bind(spec, stat, facts, variant).ok:
            result.fabricated_accepted += 1
            result.failures.append(f"fabricated accepted {spec['method']}: {variant!r}")
    for variant in swap_variants(text, stat):
        result.swapped_variants += 1
        if bind(spec, stat, facts, variant).ok:
            result.swapped_accepted += 1
            result.failures.append(f"swap accepted {spec['method']}: {variant!r}")


def run(seeds: tuple[int, ...] = SEEDS, domains: tuple[str, ...] = DOMAINS, *, n: int | None = None,
        bind: Callable[..., Any] | None = None, cases_file: Path | None = None) -> GroundingResult:
    """`bind` defaults to the platform's binder; a test passes a broken one to prove the gate fails."""
    from analystos import methods
    from analystos.evidence.facts import bind_finding
    from evaluation.analytical import component_tests

    bind = bind or bind_finding
    result = GroundingResult()
    for domain in domains:
        for seed in seeds:
            _, _, tested = component_tests(domain, seed, n=n)
            for t in tested:
                if t["stat"].supported:  # only supported results become findings with a narrative
                    _score(result, t["spec"].model_dump(mode="json"), t["stat"].model_dump(mode="json"), bind)
    cases = yaml.safe_load((cases_file or CASES_FILE).read_text())
    for fixed in cases["results"].values():
        _score(result, fixed["spec"], fixed["stat"], bind)
    for item in cases["narratives"]:
        fixed = cases["results"][item["result"]]
        facts = methods.get(fixed["spec"]["method"]).facts(fixed["spec"], fixed["stat"])
        ok = bind(fixed["spec"], fixed["stat"], facts, item["text"]).ok
        result.narratives += 1
        if ok == (item["expect"] == "bound"):
            result.narratives_correct += 1
        elif ok:
            result.adversarial_accepted += 1
            result.failures.append(f"adversarial narrative accepted ({item.get('why')}): {item['text']!r}")
        else:
            result.failures.append(f"correct narrative refused: {item['text']!r}")
    for name, checker in VOID_CHECKERS.items():
        score = {"must_void": 0, "voided": 0, "false_void": 0}
        for case in VOID_CASES:
            voided = bool(checker(case))
            score["must_void"] += int(case.must_void)
            score["voided"] += int(case.must_void and voided)
            score["false_void"] += int(voided and not case.must_void)
            if voided != case.must_void:
                result.failures.append(f"void checker {name}: {case.name}: voided={voided}, expected {case.must_void}")
        result.void[name] = score
    return result
