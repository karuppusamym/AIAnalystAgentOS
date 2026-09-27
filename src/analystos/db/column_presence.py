"""Absent curated columns (P7-20): curation survives a transient discovery gap.

A column that a crawl (or a file/recipe reload) no longer sees is deleted, unless a person curated it (a
user business name, description or tags). Then the row is kept with `absent_since` set, and it is hidden
from every ORM select on `SourceColumn`: scopes, prompts, profiling and catalog listings never offer a
column the source does not have. The code that reconciles discovered columns reads them with
``execution_options(**INCLUDE_ABSENT)`` and clears the mark when the column reappears, curation intact.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

INCLUDE_ABSENT = {"include_absent_columns": True}


def is_curated(col: Any) -> bool:
    return "user" in (col.business_name_origin, col.description_origin, col.tags_origin)


def retire(session: Session, col: Any, now: datetime) -> str:
    """A discovered column went missing: keep a curated one as absent ("kept"), delete a clean one."""
    if is_curated(col):
        if col.absent_since is None:
            col.absent_since = now
        return "kept"
    session.delete(col)
    return "deleted"


def restore(col: Any) -> bool:
    """The column was seen again: True when it had been absent."""
    if col.absent_since is None:
        return False
    col.absent_since = None
    return True


@event.listens_for(Session, "do_orm_execute")
def _hide_absent_columns(state: ORMExecuteState) -> None:
    if not state.is_select or state.is_column_load or state.execution_options.get("include_absent_columns"):
        return
    from analystos.db.models import SourceColumn

    state.statement = state.statement.options(
        with_loader_criteria(SourceColumn, SourceColumn.absent_since.is_(None), include_aliases=True))
