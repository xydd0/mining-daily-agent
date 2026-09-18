"""价格数据源接口、品种词表与共享的走势计算。"""

from __future__ import annotations

from datetime import date
from typing import Final, Protocol, runtime_checkable

from mining_daily_agent.models.prices import PricePoint, TrendSeries

#: 短/长均线窗口。
MA_SHORT: Final = 7
MA_LONG: Final = 30

#: 支持的品种（规范名）。
SUPPORTED_COMMODITIES: Final[tuple[str, ...]] = ("lithium", "nickel", "copper", "cobalt")

#: 常见别名 → 规范名。LLM 常直接给元素符号，这里统一归一。
ALIASES: Final[dict[str, str]] = {
    "li": "lithium",
    "li2o": "lithium",
    "ni": "nickel",
    "cu": "copper",
    "co": "cobalt",
}


class UnsupportedCommodityError(ValueError):
    """请求的品种不在支持列表内。

    这是业务层的合法否定，**不是**数据源故障——降级到 mock 只会拿到另一个品种
    的合成数据，因此不应触发降级。
    """


class PriceUnavailableError(ValueError):
    """请求的品种在该交易日没有行情（例如非交易日）。

    同样是业务的合法否定：用合成数据顶替会凭空捏造一个价格，比直接报错更糟。
    """


def normalize_commodity(commodity: str) -> str | None:
    """把品种名归一成规范名；不支持的品种返回 None。"""
    key = commodity.strip().casefold()
    if key in SUPPORTED_COMMODITIES:
        return key
    return ALIASES.get(key)


def supported_commodities_text() -> str:
    """列出支持的品种与别名，供错误信息使用。"""
    return f"{'、'.join(SUPPORTED_COMMODITIES)}（也接受别名：{'、'.join(sorted(ALIASES))}）"


def parse_iso_date(raw: str) -> date:
    """解析 ``YYYY-MM-DD``。

    Raises:
        PriceUnavailableError: 格式不对。
    """
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        msg = f"日期格式应为 YYYY-MM-DD，收到 {raw!r}。"
        raise PriceUnavailableError(msg) from exc


def _moving_average(prices: list[float], window: int) -> float | None:
    """取最后 ``window`` 个价格的简单均值；点数不足时返回 None。"""
    if len(prices) < window:
        return None
    return round(sum(prices[-window:]) / window, 4)


def build_trend_series(commodity: str, points: list[PricePoint], source: str) -> TrendSeries:
    """由按时间升序排列的价格点构造走势序列。

    涨跌幅按区间首末价计算；``ma7`` / ``ma30`` 取**最后** 7 / 30 个点的简单均值，
    点数不足时为 None。真实实现与 mock 共用这段计算，避免两边口径漂移。

    Raises:
        PriceUnavailableError: ``points`` 为空。
    """
    if not points:
        msg = f"{commodity} 没有任何价格点，无法构造走势序列。"
        raise PriceUnavailableError(msg)

    prices = [point.price for point in points]
    first = prices[0]
    change_pct = 0.0 if first == 0 else (prices[-1] - first) / first * 100
    return TrendSeries(
        commodity=commodity,
        points=points,
        change_pct=round(change_pct, 4),
        min=min(prices),
        max=max(prices),
        ma7=_moving_average(prices, MA_SHORT),
        ma30=_moving_average(prices, MA_LONG),
        source=source,
    )


@runtime_checkable
class PriceProvider(Protocol):
    """价格数据源协议。

    真实实现与 mock 实现都必须满足该接口，以便真实源失败时可以无缝降级。
    """

    def get_price(self, commodity: str, date: str | None) -> PricePoint:
        """取某个品种在指定交易日的价格。

        Args:
            commodity: 品种名或其别名。
            date: ``YYYY-MM-DD``；为 None 时取最新交易日。

        Returns:
            该交易日的价格观测。

        Raises:
            UnsupportedCommodityError: 品种不受支持。
            PriceUnavailableError: 该交易日没有行情。
        """
        raise NotImplementedError

    def get_trend(self, commodity: str, days: int = 30) -> TrendSeries:
        """取某个品种最近 ``days`` 个交易日的走势。

        Args:
            commodity: 品种名或其别名。
            days: 回看的交易日数量。

        Returns:
            含区间涨跌幅、极值与均线的走势序列。

        Raises:
            UnsupportedCommodityError: 品种不受支持。
        """
        raise NotImplementedError
