"""Paraphrase-retrieval benchmark for embedding providers (P4-K10).

Corpus: the platform pack exactly as `analystos seed` builds it from the installed domain packs,
one vector per document section with the index's own `search_text`. Questions: a hand-labelled
set of paraphrases (tests/fixtures/okf/paraphrase-benchmark.json). Metric: the vector leg alone
(cosine), so providers are compared on what they change — recall@1, recall@3 and MRR over
documents. Pure and in memory: no database.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from analystos.knowledge import embeddings as emb
from analystos.knowledge import okf

QUESTIONS = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "okf" / "paraphrase-benchmark.json"


def corpus() -> list[tuple[str, str]]:
    """(document title, section search text) for every section of the platform pack."""
    from analystos.capabilities import packs
    from analystos.knowledge.index import _search_text
    from analystos.knowledge.platform import domain_files

    out = []
    for path, data in sorted(domain_files(packs.load_packs(entry_points=False)).items()):
        d = okf.parse_document(path, data)
        out.extend((d.title, _search_text(d, s)) for s in d.sections)
    return out


def run(provider: emb.EmbeddingProvider, questions: Path = QUESTIONS) -> dict[str, Any]:
    labelled = json.loads(questions.read_text())["questions"]
    docs = corpus()
    started = time.perf_counter()
    dvecs = provider.embed([t for _, t in docs])
    qvecs = provider.embed([q["q"] for q in labelled])
    r1 = r3 = 0
    rr = 0.0
    misses = []
    for q, qv in zip(labelled, qvecs, strict=True):
        best: dict[str, float] = {}
        for (title, _), dv in zip(docs, dvecs, strict=True):
            sim = sum(a * b for a, b in zip(qv, dv, strict=True))
            best[title] = max(best.get(title, -2.0), sim)
        ranking = [t for t, _ in sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))]
        rank = ranking.index(q["expected"]) + 1 if q["expected"] in ranking else None
        r1 += rank == 1
        r3 += bool(rank and rank <= 3)
        rr += 1.0 / rank if rank else 0.0
        if rank != 1:
            misses.append({"q": q["q"], "expected": q["expected"], "rank": rank, "top": ranking[0]})
    n = len(labelled)
    return {"provider": emb.describe(provider), "questions": n, "documents": len({t for t, _ in docs}),
            "sections": len(docs), "recall_at_1": round(r1 / n, 4), "recall_at_3": round(r3 / n, 4),
            "mrr": round(rr / n, 4), "seconds": round(time.perf_counter() - started, 3), "misses": misses}
