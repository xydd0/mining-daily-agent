"""矿产资源报告模型。

``Field(description=...)`` 会进入 MCP 工具的输出 schema，供 LLM 理解字段含义
（见 CLAUDE.md「MCP 约定」）。
"""

# 刻意不使用 `from __future__ import annotations`：pydantic 需要在运行时求值注解。
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ResourceCategory(StrEnum):
    """资源量类别，取值限于 JORC / NI 43-101 的口径。"""

    MEASURED = "Measured"
    INDICATED = "Indicated"
    INFERRED = "Inferred"


class ResourceItem(BaseModel):
    """一条资源量记录。"""

    model_config = ConfigDict(frozen=True)

    category: ResourceCategory = Field(
        description="Resource category: Measured, Indicated or Inferred."
    )
    commodity: str = Field(
        description='Commodity the grade refers to, e.g. "Li2O", "Cu", "Au". '
        'Use "unknown" when the source does not state it.'
    )
    tonnage_t: float = Field(
        description="Tonnage in metric tonnes. Megatonnes/kilotonnes are already converted."
    )
    grade: float | None = Field(
        default=None,
        description="Grade value as stated; null when the source gives no grade.",
    )
    grade_unit: str = Field(
        default="",
        description='Unit of the grade, e.g. "%" or "g/t". Empty when grade is null.',
    )

    @model_validator(mode="after")
    def _grade_and_unit_agree(self) -> Self:
        """避免产出「有品位却没单位」这种下游无法解释的记录。"""
        if self.grade is not None and not self.grade_unit:
            msg = "grade 非空时必须同时给出 grade_unit。"
            raise ValueError(msg)
        return self


class ResourceReport(BaseModel):
    """一份 PDF 资源报告的结构化抽取结果。"""

    model_config = ConfigDict(frozen=True)

    project_name: str = Field(description="Project or deposit name.")
    source_url: str = Field(description="URL of the PDF these data came from.")
    fetched_at: datetime = Field(description="When the PDF was fetched, timezone-aware (ISO-8601).")
    resources: list[ResourceItem] = Field(
        default_factory=list,
        description="Extracted resource lines. Empty when nothing could be parsed.",
    )
    raw_snippets: list[str] = Field(
        default_factory=list,
        description=(
            "Verbatim text blocks around resource keywords, for traceability. "
            "Inspect these when `resources` is empty to judge why nothing matched."
        ),
    )
    degraded: bool = Field(
        default=False,
        description=(
            "True when these figures are synthesized fallback data rather than a "
            "parsed report. NEVER present degraded figures as real: say so explicitly."
        ),
    )
    self_reported_total_t: float | None = Field(
        default=None,
        description=(
            "Total tonnage the report states for itself (its 'Total' row), in tonnes. "
            "Use it to cross-check a sum over `resources` — JORC tables list both "
            "sub-block subtotals and the project total, so adding every row double "
            "counts. Null when the report has no recognisable total row."
        ),
    )
