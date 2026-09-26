"""File-by-file diff of a generated dbt project against an earlier build job (P4-U05).

What a reviewer reads before approving a build: which files the new project adds, removes or changes
compared with the previous job for the same target, with a unified diff per changed file. Pure and
deterministic; the approval still binds the full project hash, so the diff is an aid, never the gate.
"""
from __future__ import annotations

import difflib
from typing import Any

MAX_DIFF_LINES = 400  # per file; a longer diff is cut and flagged, the full files stay on the job


def _lines(text: str) -> list[str]:
    return text.splitlines()


def file_diff(before: dict[str, str] | None, after: dict[str, str], *, context: int = 3,
              max_lines: int = MAX_DIFF_LINES) -> dict[str, Any]:
    """Compare two project file maps (path -> content). `before=None` means there is no earlier job:
    every file is reported as added."""
    before = dict(before or {})
    files: list[dict[str, Any]] = []
    totals = {"added": 0, "removed": 0, "modified": 0, "unchanged": 0, "lines_added": 0, "lines_removed": 0}
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old == new:
            status = "unchanged"
        elif old is None:
            status = "added"
        elif new is None:
            status = "removed"
        else:
            status = "modified"
        entry: dict[str, Any] = {"path": path, "status": status, "lines_added": 0, "lines_removed": 0, "diff": "", "truncated": False}
        if status != "unchanged":
            diff = list(difflib.unified_diff(_lines(old or ""), _lines(new or ""), fromfile=f"a/{path}" if old is not None else "/dev/null",
                                             tofile=f"b/{path}" if new is not None else "/dev/null", n=context, lineterm=""))
            entry["lines_added"] = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
            entry["lines_removed"] = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
            entry["truncated"] = len(diff) > max_lines
            entry["diff"] = "\n".join(diff[:max_lines])
            totals["lines_added"] += entry["lines_added"]
            totals["lines_removed"] += entry["lines_removed"]
        totals[status] += 1
        files.append(entry)
    return {"files": files, "summary": totals}
