"""mining-news-mcp 的行为测试。

网络层全部被替换为假实现，不触网（见 CLAUDE.md「测试」）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import override

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mining_daily_agent.models.news import Article, NewsItem
from mining_daily_agent.providers import news as news_pkg
from mining_daily_agent.providers.news.base import NewsProvider
from mining_daily_agent.providers.news.mock import MockNewsProvider
from mining_daily_agent.providers.news.rss import RssNewsProvider
from mining_daily_agent.servers import news_server


def _item(title: str = "Pilbara lithium output rises") -> NewsItem:
    return NewsItem(
        title=title,
        url="https://example.com/article",
        source="Example",
        published_at=datetime.now(UTC),
        summary="A summary.",
    )


class _RecordingProvider(NewsProvider):
    """记录调用参数的假新闻源。"""

    def __init__(self, items: list[NewsItem] | None = None) -> None:
        self.items: list[NewsItem] = items if items is not None else []
        self.search_calls: list[tuple[str, int]] = []
        self.article_calls: list[str] = []

    @override
    def search(self, query: str, days: int) -> list[NewsItem]:
        self.search_calls.append((query, days))
        return self.items

    @override
    def fetch_article(self, url: str) -> Article:
        self.article_calls.append(url)
        return Article(
            title="Recorded",
            url=url,
            source="Example",
            published_at=datetime.now(UTC),
            text="body",
        )


class _FailingProvider(NewsProvider):
    """模拟真实源在调用阶段整体不可用。"""

    @override
    def search(self, query: str, days: int) -> list[NewsItem]:
        raise RuntimeError("upstream 503")

    @override
    def fetch_article(self, url: str) -> Article:
        raise RuntimeError("upstream 503")


class _BrokenRssProvider(RssNewsProvider):
    """模拟真实源在初始化阶段就失败。"""

    def __init__(self) -> None:
        raise RuntimeError("dns failure to news.google.com")


# --- 场景 1：search 正常返回 -------------------------------------------------


def test_search_returns_provider_items(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [_item()]
    provider = _RecordingProvider(items)
    monkeypatch.setattr(news_server, "get_news_provider", lambda: provider)

    result = news_server.search(query="pilbara lithium", days=3)

    assert result == items
    assert provider.search_calls == [("pilbara lithium", 3)]


def test_search_defaults_to_one_day(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _RecordingProvider()
    monkeypatch.setattr(news_server, "get_news_provider", lambda: provider)

    news_server.search(query="copper")

    assert provider.search_calls == [("copper", 1)]


# --- 场景 2：days 越界被钳制 -------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(30, 30), (1, 1), (31, 30), (999, 30), (0, 1), (-5, 1)],
)
def test_search_clamps_days(monkeypatch: pytest.MonkeyPatch, requested: int, expected: int) -> None:
    provider = _RecordingProvider()
    monkeypatch.setattr(news_server, "get_news_provider", lambda: provider)

    news_server.search(query="q", days=requested)

    assert provider.search_calls == [("q", expected)]


# --- 场景 3：真实源失败自动降级 mock -----------------------------------------


def test_factory_falls_back_to_mock_when_rss_init_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(news_pkg, "RssNewsProvider", _BrokenRssProvider)

    provider = news_pkg.get_news_provider()

    assert isinstance(provider, MockNewsProvider)


def test_factory_logs_warning_with_exception_summary(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(news_pkg, "RssNewsProvider", _BrokenRssProvider)

    with caplog.at_level(logging.WARNING):
        news_pkg.get_news_provider()

    assert "降级" in caplog.text
    assert "dns failure to news.google.com" in caplog.text


def test_search_falls_back_to_mock_when_provider_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(news_server, "get_news_provider", _FailingProvider)

    result = news_server.search(query="pilbara lithium", days=1)

    assert result, "降级后应返回内置数据而不是空列表或异常"
    assert any("Pilbara" in item.title for item in result)


def test_search_fallback_logs_degraded_event(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(news_server, "get_news_provider", _FailingProvider)

    with caplog.at_level(logging.INFO):
        news_server.search(query="pilbara lithium", days=1)

    assert "degraded=true" in caplog.text
    assert "server=mining-news-mcp" in caplog.text
    assert "tool=search" in caplog.text


def test_fetch_article_falls_back_to_mock_when_provider_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(news_server, "get_news_provider", _FailingProvider)

    article = news_server.fetch_article(url="https://www.mining.com/unknown/")

    assert article.url == "https://www.mining.com/unknown/"
    assert article.source == "www.mining.com"


# --- 场景 4：fetch_article 非法 URL 报错 -------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    ["", "not-a-url", "example.com/article", "ftp://example.com/a", "file:///etc/passwd"],
)
def test_fetch_article_rejects_invalid_url(bad_url: str) -> None:
    with pytest.raises(ValueError, match="URL"):
        news_server.fetch_article(url=bad_url)


def test_invalid_url_error_is_a_tool_error_so_message_reaches_the_llm() -> None:
    """锁定「提示信息必须能抵达 LLM」这一行为。

    MCP 只原样转发 ToolError 的 message；其他异常会被 SDK 替换成
    "Error executing tool xxx"，异常文本留在服务端。若有人把这里改回裸
    ValueError，提示信息就会静默丢失，本测试会失败。
    """
    with pytest.raises(ToolError) as excinfo:
        news_server.fetch_article(url="not-a-url")

    assert "not-a-url" in str(excinfo.value)


def test_fetch_article_does_not_touch_provider_for_invalid_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _RecordingProvider()
    monkeypatch.setattr(news_server, "get_news_provider", lambda: provider)

    with pytest.raises(ValueError, match="URL"):
        news_server.fetch_article(url="example.com/article")

    assert provider.article_calls == []


def test_fetch_article_returns_article_for_valid_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _RecordingProvider()
    monkeypatch.setattr(news_server, "get_news_provider", lambda: provider)

    article = news_server.fetch_article(url="https://example.com/article")

    assert article.url == "https://example.com/article"
    assert provider.article_calls == ["https://example.com/article"]
