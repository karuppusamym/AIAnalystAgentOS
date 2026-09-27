"""A reusable business lens over one or more sources.

The context is versioned separately from the workspace semantic model. A run pins the
published context version and keeps its own question and resolved data scope.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AnalysisContextSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: str = Field(min_length=10, max_length=500)
    business_description: str = Field(default="", max_length=2000)
    question_template: str = Field(min_length=10, max_length=1000)
    source_ids: list[str] = Field(min_length=1, max_length=10)
    metric_names: list[str] = Field(default_factory=list, max_length=50)
