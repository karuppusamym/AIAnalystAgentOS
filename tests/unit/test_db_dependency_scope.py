"""The request session commits before the response is sent. With FastAPI's default ("request")
scope a yield dependency exits after the reply: a 200 for a revoked MCP client could reach the
caller before the revocation committed (seen in CI), and a failed commit would be lost silently."""
from __future__ import annotations

import re
from pathlib import Path

API = Path(__file__).resolve().parents[2] / "src" / "analystos" / "api"


def test_every_db_dependency_is_function_scoped():
    unscoped = [f"{p.relative_to(API)}:{n}" for p in API.rglob("*.py")
                for n, line in enumerate(p.read_text().splitlines(), 1)
                if re.search(r"Depends\(db\s*(\)|,(?![^)]*scope=\"function\"))", line)]
    assert not unscoped, "Depends(db) must be scope=\"function\": " + ", ".join(unscoped)
