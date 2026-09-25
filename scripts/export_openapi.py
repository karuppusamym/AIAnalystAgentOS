"""Write the FastAPI OpenAPI schema to web/openapi.json (P4-U01 generated client).

The web client's TypeScript types are generated from this file (`npm run gen:api`), and CI fails
when the committed copy is stale. Importing the app must not need running services: settings are
read lazily, and only safe dummy values are set here for anything the import path reads.

Usage: PYTHONPATH=src python scripts/export_openapi.py [--out web/openapi.json] [--check]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "web" / "openapi.json"


def build_schema() -> dict:
    # Dummy values only: the schema does not depend on them, and nothing connects at import time.
    os.environ.setdefault("OPENROUTER_API_KEY", "")
    os.environ.setdefault("ANALYSTOS_ORCHESTRATOR", "local")
    sys.path.insert(0, str(ROOT / "src"))
    from analystos.api.app import app

    return app.openapi()


def render(schema: dict) -> str:
    # Stable output: sorted keys and a trailing newline so `git diff --exit-code` is meaningful.
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--check", action="store_true", help="exit 1 when the file on disk is stale")
    args = parser.parse_args()
    schema = build_schema()
    text = render(schema)
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if current != text:
            print(f"{args.out} is stale: run scripts/export_openapi.py", file=sys.stderr)
            return 1
        print(f"{args.out} is current")
        return 0
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out} ({len(text)} bytes, {len(schema.get('paths', {}))} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
