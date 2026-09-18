"""Agent 的状态定义与取数计划。"""

# 刻意**不**使用 `from __future__ import annotations`：LangGraph 构建图时会运行时解析
# 状态 schema 的注解（含 Annotated 里的 reducer），把模型导入放进 TYPE_CHECKING 会直接
# 抛 `NameError: name 'NewsItem' is not defined`——实测如此，不只是理论风险。
import operator
from typing import Annotated, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mining_daily_agent.models.news import NewsItem
from mining_daily_agent.models.prices import TrendSeries
from mining_daily_agent.models.resources import ResourceReport


class FetchPlan(BaseModel):
    """planner 产出的取数计划，由 LLM 的 JSON 输出校验而成。"""

    model_config = ConfigDict(frozen=True)

    keywords: str = Field(description="传给 news.search 的关键词。多个词会以 OR 连接")

    @field_validator("keywords", mode="before")
    @classmethod
    def _accept_keyword_list(cls, value: object) -> object:
        """接受 LLM 给出的关键词数组，用 OR 连成单个查询串。

        实测 LLM 很自然地返回 ``["Pilbara lithium mine", "Pilgangoora", ...]``；工具
        只收单个字符串，直接判为非法会让计划白白回退成默认值（真实踩过）。Google News
        的 ``q`` 参数支持 OR，所以串起来比只取第一个词覆盖面更好。
        """
        if isinstance(value, list):
            tokens = [str(item).strip() for item in value]
            return " OR ".join(token for token in tokens if token)
        return value

    days: int = Field(default=1, ge=1, le=30, description="新闻回溯天数（工具侧上限 30）")
    commodity: str = Field(default="lithium", description="传给价格工具的品种名")
    needs_pdf: bool = Field(default=True, description="是否需要抽取资源报告 PDF")
    rationale: str = Field(default="", description="LLM 给出的理由，仅用于日志与排错")


class BriefState(TypedDict):
    """每日简报流程的状态。

    除需求列出的字段外，另有两个字段是流程本身必需的：

    - ``plan``：planner 的产出必须传给 fetch_data，否则两个节点接不上；
    - ``highlights``：analyze 算出的价格统计与储量汇总，synthesize 要引用它们。

    ``article`` 取被选作主源的新闻条目（fetch_data 挑出来的那条）。
    """

    topic: str
    plan: FetchPlan
    news: list[NewsItem]
    article: NewsItem | None
    resource_report: ResourceReport | None
    price_trend: TrendSeries | None
    #: risk_notes 有三个写入者——planner 的兜底提示、fetch_data 的降级记录、analyze
    #: 的风险词命中。用 operator.add 归并，否则后写的节点会覆盖先写的。
    risk_notes: Annotated[list[str], operator.add]
    highlights: list[str]
    markdown: str
    citations: list[str]
