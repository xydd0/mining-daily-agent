"""新闻数据源接口。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from mining_daily_agent.models.news import Article, NewsItem


@runtime_checkable
class NewsProvider(Protocol):
    """新闻数据源协议。

    真实实现与 mock 实现都必须满足该接口，以便真实源失败时可以无缝降级。
    """

    def search(self, query: str, days: int) -> list[NewsItem]:
        """检索最近 ``days`` 天内与 ``query`` 相关的新闻。

        Args:
            query: 关键词。
            days: 回溯天数。

        Returns:
            匹配的新闻摘要列表，可能为空。
        """
        raise NotImplementedError

    def fetch_article(self, url: str) -> Article:
        """抓取指定 URL 的正文。

        Args:
            url: 绝对 http(s) URL。

        Returns:
            解析后的正文。
        """
        raise NotImplementedError
