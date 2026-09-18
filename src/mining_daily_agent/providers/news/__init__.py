"""新闻数据源：真实 RSS 实现 + mock 降级实现。"""

from __future__ import annotations

import logging

from mining_daily_agent.providers.news.base import NewsProvider
from mining_daily_agent.providers.news.mock import MockNewsProvider
from mining_daily_agent.providers.news.rss import RssNewsProvider

logger = logging.getLogger(__name__)

__all__ = [
    "MockNewsProvider",
    "NewsProvider",
    "RssNewsProvider",
    "get_news_provider",
]


def get_news_provider() -> NewsProvider:
    """返回可用的新闻源：优先真实 RSS，任何异常都降级到 mock。

    这里只负责初始化阶段的降级；调用阶段（检索/抓取过程中）的降级由
    ``servers.news_server`` 负责，两层共同保证真实源失败时流程不中断。
    """
    try:
        return RssNewsProvider()
    except Exception as exc:  # 降级兜底刻意捕获一切初始化失败，不限于特定异常类型
        logger.warning(
            "真实新闻源初始化失败，降级到 MockNewsProvider：provider=RssNewsProvider error=%s: %s",
            type(exc).__name__,
            exc,
        )
        return MockNewsProvider()
