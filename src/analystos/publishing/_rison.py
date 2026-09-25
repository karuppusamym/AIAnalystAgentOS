"""Minimal Rison encoder/decoder for Superset's `?q=` list-query parameter.

Superset (Flask-AppBuilder) encodes list filters as Rison, e.g.
``(filters:!((col:database_name,opr:eq,value:'AnalystOS Analytics (ws_1)')),page:0,page_size:100)``.
Strings are always quoted on encode; that is valid Rison and avoids id-character edge cases.
"""
from __future__ import annotations

from typing import Any


def dumps(value: Any) -> str:
    if value is None:
        return "!n"
    if value is True:
        return "!t"
    if value is False:
        return "!f"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    if isinstance(value, str):
        return "'" + value.replace("!", "!!").replace("'", "!'") + "'"
    if isinstance(value, (list, tuple)):
        return "!(" + ",".join(dumps(v) for v in value) + ")"
    if isinstance(value, dict):
        return "(" + ",".join(f"{_key(k)}:{dumps(v)}" for k, v in value.items()) + ")"
    raise TypeError(f"cannot rison-encode {type(value).__name__}")


def _key(key: str) -> str:
    return key if key.replace("_", "").isalnum() and not key[0].isdigit() else dumps(key)


_ID_STOP = set(" '!:(),*@$")


def loads(text: str) -> Any:
    value, pos = _parse(text, 0)
    if pos != len(text):
        raise ValueError(f"trailing rison data at {pos}")
    return value


def _parse(s: str, i: int) -> tuple[Any, int]:
    c = s[i]
    if c == "!":
        n = s[i + 1]
        if n == "n":
            return None, i + 2
        if n == "t":
            return True, i + 2
        if n == "f":
            return False, i + 2
        if n == "(":
            i += 2
            out: list[Any] = []
            while s[i] != ")":
                v, i = _parse(s, i)
                out.append(v)
                if s[i] == ",":
                    i += 1
            return out, i + 1
        raise ValueError(f"bad rison escape at {i}")
    if c == "(":
        i += 1
        obj: dict[str, Any] = {}
        while s[i] != ")":
            k, i = _parse(s, i)
            if s[i] != ":":
                raise ValueError(f"expected ':' at {i}")
            v, i = _parse(s, i + 1)
            obj[str(k)] = v
            if s[i] == ",":
                i += 1
        return obj, i + 1
    if c == "'":
        i += 1
        buf: list[str] = []
        while s[i] != "'":
            if s[i] == "!":
                buf.append(s[i + 1])
                i += 2
            else:
                buf.append(s[i])
                i += 1
        return "".join(buf), i + 1
    j = i
    while j < len(s) and s[j] not in _ID_STOP:
        j += 1
    token = s[i:j]
    if not token:
        raise ValueError(f"unexpected {c!r} at {i}")
    try:
        return (float(token) if any(ch in token for ch in ".eE") else int(token)), j
    except ValueError:
        return token, j
