"""价格数据源：真实行情实现 + mock 合成序列降级实现。"""

from __future__ import annotations

import logging

from mining_daily_agent.providers.prices.base import PriceProvider
from mining_daily_agent.providers.prices.mock import MockPriceProvider
from mining_daily_agent.providers.prices.stooq import StooqPriceProvider

logger = logging.getLogger(__name__)

__all__ = [
    "MockPriceProvider",
    "PriceProvider",
    "StooqPriceProvider",
    "get_price_provider",
]


def get_price_provider() -> PriceProvider:
    """返回可用的价格源：优先真实行情，任何异常都降级到 mock。

    这里只负责初始化阶段的降级；调用阶段的降级由 ``servers.price_server``
    负责，两层共同保证真实源失败时流程不中断（见 CLAUDE.md「可靠性」）。
    """
    try:
        return StooqPriceProvider()
    except Exception as exc:  # 降级兜底刻意捕获一切初始化失败，不限定异常类型
        logger.warning(
            "真实价格源初始化失败，降级到 MockPriceProvider：provider=StooqPriceProvider "
            "error=%s: %s",
            type(exc).__name__,
            exc,
        )
        return MockPriceProvider()
