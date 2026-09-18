"""内置合成价格序列的降级实现。

四个品种各生成 90 个交易日的**合成**序列：固定随机 seed 保证同一品种每次
生成完全一致，走势 = 每日趋势项 + 高斯波动。

这些数字不是任何市场的真实报价。为了不误导下游，系列里每个 ``PricePoint.source``
都明确标注为合成数据。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final, override

from mining_daily_agent.models.prices import PricePoint, TrendSeries
from mining_daily_agent.providers.prices.base import (
    PriceProvider,
    PriceUnavailableError,
    UnsupportedCommodityError,
    build_trend_series,
    normalize_commodity,
    parse_iso_date,
    supported_commodities_text,
)

#: 合成序列的长度（交易日）。
SERIES_DAYS: Final = 90
#: 固定 seed：同一品种每次都生成同一条序列，便于复现与测试。
SEED: Final = 20260918
#: 合成数据的单位是每吨金属价（与真实源的 USD/share 不同，见 models/prices.py）。
MOCK_UNIT: Final = "USD/t"
#: 写进每个数据点的溯源标签，明确这是合成数据。
MOCK_SOURCE: Final = "mock: synthesized series, not real market data"
#: 价格下限，避免随机游走走到不合常理的低位。
FLOOR_RATIO: Final = 0.4


@dataclass(frozen=True, slots=True)
class _Spec:
    """一个品种的合成长参数。

    Attributes:
        base_price: 起始量级（USD/t），仅用于让合成数据看起来合理。
        daily_drift: 每日趋势项。
        daily_volatility: 每日波动幅度，相对 ``base_price`` 的比例。
    """

    base_price: float
    daily_drift: float
    daily_volatility: float


_SPECS: Final[dict[str, _Spec]] = {
    "lithium": _Spec(base_price=12_500.0, daily_drift=-18.0, daily_volatility=0.010),
    "nickel": _Spec(base_price=16_800.0, daily_drift=12.0, daily_volatility=0.008),
    "copper": _Spec(base_price=9_400.0, daily_drift=6.0, daily_volatility=0.006),
    "cobalt": _Spec(base_price=33_000.0, daily_drift=-25.0, daily_volatility=0.011),
}


def _weekdays_ending(end: date, count: int) -> list[date]:
    """取以 ``end`` 结尾的、按时间升序排列的 ``count`` 个工作日。

    跳过多末两天，让合成序列的形态与真实交易日序列一致——否则查询周末会
    在 mock 上意外成功、在真实源上失败。
    """
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))


def build_mock_series(commodity: str, today: date | None = None) -> list[PricePoint]:
    """生成某品种的 90 个交易日合成价格序列。

    Raises:
        UnsupportedCommodityError: 品种不受支持。
    """
    normalized = normalize_commodity(commodity)
    spec = _SPECS.get(normalized or "")
    if normalized is None or spec is None:
        msg = f"不支持的品种：{commodity!r}。当前支持：{supported_commodities_text()}。"
        raise UnsupportedCommodityError(msg)

    end = today if today is not None else datetime.now(UTC).date()
    # 用 "seed:品种" 做种子：字符串种子经 sha512 转换，跨进程稳定。
    rng = random.Random(f"{SEED}:{normalized}")
    price = spec.base_price
    points: list[PricePoint] = []
    for day in _weekdays_ending(end, SERIES_DAYS):
        shock = rng.gauss(0.0, spec.base_price * spec.daily_volatility)
        price = max(price + spec.daily_drift + shock, spec.base_price * FLOOR_RATIO)
        points.append(
            PricePoint(
                commodity=normalized,
                date=day,
                price=round(price, 2),
                currency="USD",
                unit=MOCK_UNIT,
                source=MOCK_SOURCE,
                degraded=True,
            )
        )
    return points


class MockPriceProvider(PriceProvider):
    """降级实现：返回合成价格序列，绝不触网。"""

    @override
    def get_price(self, commodity: str, date: str | None) -> PricePoint:
        """取合成序列中某交易日的价格；``date`` 为 None 时取最新交易日。

        Raises:
            UnsupportedCommodityError: 品种不受支持。
            PriceUnavailableError: 该交易日不在合成序列内。
        """
        normalized = normalize_commodity(commodity)
        points = build_mock_series(commodity)
        if date is None:
            return points[-1]

        target = parse_iso_date(date)
        for point in points:
            if point.date == target:
                return point
        msg = (
            f"{normalized} 在 {target.isoformat()} 没有行情（可能是非交易日）。"
            f"注意：这是合成数据，覆盖 {points[0].date.isoformat()} 起的 "
            f"{len(points)} 个交易日。"
        )
        raise PriceUnavailableError(msg)

    @override
    def get_trend(self, commodity: str, days: int = 30) -> TrendSeries:
        """取合成序列最近 ``days`` 个交易日的走势。

        Raises:
            UnsupportedCommodityError: 品种不受支持。
        """
        normalized = normalize_commodity(commodity)
        points = build_mock_series(commodity)[-days:]
        if normalized is None:  # build_mock_series 已会抛错，这里只为类型收窄
            msg = f"不支持的品种：{commodity!r}。当前支持：{supported_commodities_text()}。"
            raise UnsupportedCommodityError(msg)
        return build_trend_series(normalized, points, MOCK_SOURCE)
