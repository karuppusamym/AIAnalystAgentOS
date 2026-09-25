"""Just-in-time secret resolution for source credentials.

A source row stores only a *reference* (``env:NAME`` or ``file:/path``), never a value. Values
are resolved at the moment a connection is opened and are never logged or persisted.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from analystos.core.errors import InvalidInput

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def resolve_secret(ref: str | None) -> str | None:
    """Resolve ``env:NAME`` / ``file:/abs/path``. ``None``/empty -> ``None``.

    Raises InvalidInput for unknown schemes, a missing variable or an unreadable file. Error
    messages name the reference, never the value.
    """
    if ref is None or not str(ref).strip():
        return None
    ref = str(ref).strip()
    scheme, sep, rest = ref.partition(":")
    if not sep:
        raise InvalidInput("secret_ref must look like 'env:NAME' or 'file:/path'")
    scheme = scheme.lower()
    if scheme == "env":
        if not _ENV_NAME.match(rest):
            raise InvalidInput("secret_ref env name is not a valid environment variable name")
        value = os.environ.get(rest)
        if value is None:
            raise InvalidInput(f"secret_ref env:{rest} is not set")
        return value
    if scheme == "file":
        path = Path(rest).expanduser()
        if not path.is_absolute():
            raise InvalidInput("secret_ref file path must be absolute")
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise InvalidInput(f"secret_ref file {path} is not readable: {exc.__class__.__name__}") from None
    raise InvalidInput(f"secret_ref scheme '{scheme}' is not supported (use env: or file:)")
