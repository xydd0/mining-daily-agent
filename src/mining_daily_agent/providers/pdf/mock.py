"""内置 Pilgangoora 风格资源表的降级实现。

数据是手工编写的演示夹具：项目形态与量级参考公开的 Pilgangoora 锂矿资源量
公告，但**具体数字并非真实披露值**，不可用于任何实际判断。为了不误导下游，
``raw_snippets`` 的首条会明确标注这是降级数据。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, override

from mining_daily_agent.models.resources import ResourceCategory, ResourceItem, ResourceReport
from mining_daily_agent.providers.pdf.base import PdfProvider

PROJECT_NAME: Final = "Pilgangoora (mock fixture)"
COMMODITY: Final = "Li2O"
GRADE_UNIT: Final = "%"

#: 降级数据声明，置于 raw_snippets 首位，避免被误当成真实披露值。
MOCK_NOTICE: Final = (
    "[降级提示] 以下为内置示例数据，并未从 source_url 实际下载或解析 PDF，"
    "数值不可用于任何实际判断。"
)


@dataclass(frozen=True, slots=True)
class _MockSpec:
    """一条内置资源量记录。"""

    category: ResourceCategory
    tonnage_mt: float
    grade: float

    @property
    def snippet(self) -> str:
        """按真实公告的书写形式给出原文摘录。"""
        return (
            f"{self.category.value} Mineral Resource: "
            f"{self.tonnage_mt:g} Mt at {self.grade:g}{GRADE_UNIT} {COMMODITY}"
        )


#: Indicated 合计 300 Mt（3 亿吨），品位落在 1.0-1.2% Li2O。
_SPECS: Final[tuple[_MockSpec, ...]] = (
    _MockSpec(ResourceCategory.INDICATED, 214.0, 1.15),
    _MockSpec(ResourceCategory.INDICATED, 86.0, 1.08),
    _MockSpec(ResourceCategory.INFERRED, 89.0, 1.05),
    _MockSpec(ResourceCategory.INFERRED, 42.0, 1.02),
)


def build_mock_report(pdf_url: str, now: datetime | None = None) -> ResourceReport:
    """构造内置资源报告。"""
    reference = now if now is not None else datetime.now(UTC)
    return ResourceReport(
        project_name=PROJECT_NAME,
        source_url=pdf_url,
        fetched_at=reference,
        resources=[
            ResourceItem(
                category=spec.category,
                commodity=COMMODITY,
                tonnage_t=spec.tonnage_mt * 1e6,
                grade=spec.grade,
                grade_unit=GRADE_UNIT,
            )
            for spec in _SPECS
        ],
        raw_snippets=[MOCK_NOTICE, *(spec.snippet for spec in _SPECS)],
        # 结构化标记：上层据此判断数据是不是编造的，比在 raw_snippets 里找文案可靠。
        degraded=True,
    )


class MockPdfProvider(PdfProvider):
    """降级实现：返回内置资源表，绝不触网。"""

    @override
    def extract_resources(self, pdf_url: str) -> ResourceReport:
        """忽略 ``pdf_url`` 的内容，返回内置资源表。

        与关键词过滤同理，这里不按 URL 区分数据——它是兜底数据集，按 URL
        匹配只会让绝大多数请求拿到空结果，失去兜底意义。``source_url`` 仍记录
        调用方实际请求的地址，配合 ``raw_snippets`` 首条的降级声明可追溯。
        """
        return build_mock_report(pdf_url)
