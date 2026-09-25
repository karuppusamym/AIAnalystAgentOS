"""P4-K10 paraphrase-retrieval benchmark: hashing vs a local sentence-transformer.

    python scripts/benchmark_embeddings.py [--model BAAI/bge-small-en-v1.5] [--allow-download]

Prints one JSON report. The sentence-transformer leg is skipped (and says why) when the optional
`embeddings` extra or the model is not available locally.
"""
from __future__ import annotations

import argparse
import json

from analystos.knowledge import benchmark
from analystos.knowledge import embeddings as emb


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="BAAI/bge-small-en-v1.5")
    p.add_argument("--allow-download", action="store_true")
    args = p.parse_args()
    report: dict = {"hashing": benchmark.run(emb.HashingProvider())}
    try:
        st = emb.sentence_transformer(args.model, allow_download=args.allow_download)
        report["sentence_transformers"] = benchmark.run(st)
    except Exception as exc:  # noqa: BLE001
        report["sentence_transformers"] = {"skipped": f"{type(exc).__name__}: {str(exc)[:200]}"}
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
