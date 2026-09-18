"""价格领域模型。

``Field(description=...)`` 会进入 MCP 工具的输出 schema，供 LLM 理解字段含义
（见 CLAUDE.md「MCP 约定」）。

**关于 ``unit`` 的重要说明**：LME 金属现货行情没有免费 API，真实实现只能取
上市代理品种（ETF / 矿业公司）的行情，其单位是 **USD/share**，不是每吨金属价，
也不是伦敦现货报价。只有 mock 合成数据才用 ``USD/t``。调用方必须依据 ``unit``
与 ``source`` 判断数值含义，不要把代理品种的股价当成金属价格引用。
"""

# 刻意不使用 `from __future__ import annotations`：pydantic 需要在运行时求值注解。
# 也刻意用 `import datetime` 而非 `from datetime import date`：本模型有个字段就叫
# `date`，若注解写成 `date: date`，pydantic 会抛
# "field name clashing with a type annotation"。用 `datetime.date` 规避同名。
import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PricePoint(BaseModel):
    """某个品种在某一交易日的单个价格观测。"""

    model_config = ConfigDict(frozen=True)

    commodity: str = Field(description='Commodity key, e.g. "lithium", "nickel".')
    date: datetime.date = Field(description="Trading date in YYYY-MM-DD form.")
    price: float = Field(description="Closing price on that date.")
    currency: str = Field(default="USD", description='Currency of `price`, e.g. "USD".')
    unit: str = Field(
        description=(
            'Unit `price` is quoted in. "USD/t" means a metal price per tonne; '
            '"USD/share" means a listed proxy instrument\'s share price. Read '
            "`source` to tell which kind of figure this is."
        )
    )
    source: str = Field(
        description=(
            "Where the figure came from. Proxy quotes say so explicitly and name "
            "the instrument; synthesized fallback data is labelled as such."
        )
    )


class TrendSeries(BaseModel):
    """某品种一段区间内的价格走势与统计量。"""

    model_config = ConfigDict(frozen=True)

    commodity: str = Field(description='Commodity key, e.g. "lithium".')
    points: list[PricePoint] = Field(
        description="Price observations in chronological order (oldest first)."
    )
    change_pct: float = Field(
        description="Percentage change from the first to the last point, e.g. -4.2 means -4.2%."
    )
    # 字段名 min / max 由需求指定，确实会遮蔽同名内置函数；此处是数据字段，
    # 沿用它们比改名更贴近调用方预期（ruff 的 A003 只针对类属性赋值，不报 pydantic 字段）。
    min: float = Field(description="Lowest price in the window.")
    max: float = Field(description="Highest price in the window.")
    ma7: float | None = Field(
        default=None,
        description="Simple moving average of the last 7 points; null when fewer than 7.",
    )
    ma30: float | None = Field(
        default=None,
        description="Simple moving average of the last 30 points; null when fewer than 30.",
    )
    source: str = Field(description="Source label shared by every point in `points`.")

    @model_validator(mode="after")
    def _points_are_consistent(self) -> Self:
        """拦掉「有数据点却没有统计量」这类自相矛盾的结果。"""
        if not self.points:
            msg = "TrendSeries 至少需要一个数据点。"
            raise ValueError(msg)
        if self.min > self.max:
            msg = f"min ({self.min}) 不应大于 max ({self.max})。"
            raise ValueError(msg)
        return self
