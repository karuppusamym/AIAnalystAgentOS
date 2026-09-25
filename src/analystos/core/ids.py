from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime
from typing import Any


def new_id(prefix: str) -> str:
    """Readable, sortable-enough ids: ws_1f3a9c0b2d4e."""
    return f"{prefix}_{secrets.token_hex(6)}"


def utcnow() -> datetime:
    return datetime.now(UTC)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def stable_hash(value: Any) -> str:
    """SHA-256 over canonical JSON. Used for plan hashes, approval payload binding and query fingerprints."""
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()
