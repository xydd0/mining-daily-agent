"""RSS 与 mock 新闻源的测试。

HTTP 层通过替换 ``rss._http_get`` 这一唯一接缝来 mock，不触网，
退避等待也被替换掉，测试不会真的 sleep（见 CLAUDE.md「测试」）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from mining_daily_agent.providers.news import mock as mock_module
from mining_daily_agent.providers.news import rss
from mining_daily_agent.providers.news.mock import MockNewsProvider
from mining_daily_agent.providers.news.rss import RssFetchError, RssNewsProvider, clean_summary

FEED_URL = "https://example.com/feed"


def _response(url: str, body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        content=body.encode("utf-8"),
        request=httpx.Request("GET", url),
    )


def _stub_get(body: str, status: int = 200) -> Callable[[str], httpx.Response]:
    """返回一个假的 _http_get，始终回同一份内容。"""

    def _get(url: str) -> httpx.Response:
        return _response(url, body, status)

    return _get


def _feed(
    *,
    age_days: float,
    title: str = "Pilbara lithium output rises",
    summary_html: str = "&lt;p&gt;Booming &lt;b&gt;lithium&lt;/b&gt; output&lt;/p&gt;",
) -> str:
    """构造一份含单条条目的 RSS。"""
    published = format_datetime(datetime.now(UTC) - timedelta(days=age_days))
    return (
        '<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>'
        "<title>Test feed</title><item>"
        f"<title>{title}</title>"
        "<link>https://example.com/article</link>"
        f"<description>{summary_html}</description>"
        f"<pubDate>{published}</pubDate>"
        "</item></channel></rss>"
    )


# --- clean_summary ----------------------------------------------------------


def test_clean_summary_strips_html_tags() -> None:
    assert clean_summary("<p>Hello <b>world</b></p>") == "Hello world"


def test_clean_summary_truncates_to_500_chars() -> None:
    result = clean_summary("x" * 900)

    assert len(result) == rss.SUMMARY_MAX_CHARS
    assert result.endswith("…")


# --- _fetch_bytes：超时、重试与退避 -----------------------------------------


def test_fetch_bytes_retries_with_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    def _get(url: str) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.ConnectError("flaky network")
        return _response(url, "<rss/>")

    monkeypatch.setattr(rss, "_http_get", _get)
    monkeypatch.setattr(time, "sleep", sleeps.append)

    assert rss._fetch_bytes(FEED_URL) == b"<rss/>"
    assert len(attempts) == 3
    assert sleeps == [0.5, 1.0], "退避应为 0.5s、1.0s"


def test_fetch_bytes_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    def _get(url: str) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(rss, "_http_get", _get)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    with pytest.raises(RssFetchError, match="连续失败"):
        rss._fetch_bytes(FEED_URL)

    assert len(attempts) == rss.MAX_ATTEMPTS == 3


def test_fetch_bytes_retries_on_http_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rss, "_http_get", _stub_get("unavailable", status=503))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    with pytest.raises(RssFetchError, match="连续失败"):
        rss._fetch_bytes(FEED_URL)


def test_http_get_sets_explicit_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def _fake(url: str, **kwargs: object) -> httpx.Response:
        seen.update(kwargs)
        return _response(url, "<rss/>")

    monkeypatch.setattr(httpx, "get", _fake)

    rss._http_get(FEED_URL)

    assert seen["timeout"] == rss.HTTP_TIMEOUT_SECONDS == 15.0


# --- RssNewsProvider.search -------------------------------------------------


def test_search_parses_feed_and_filters_by_days(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rss, "_http_get", _stub_get(_feed(age_days=0.1)))

    items = RssNewsProvider().search("pilbara lithium", days=1)

    assert len(items) == 1
    assert items[0].source == "Google News"
    assert items[0].summary == "Booming lithium output"


def test_search_drops_items_older_than_requested_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rss, "_http_get", _stub_get(_feed(age_days=10)))

    with pytest.raises(RssFetchError, match="全部 RSS 源"):
        RssNewsProvider().search("pilbara lithium", days=1)


def test_search_falls_through_to_next_source(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_urls: list[str] = []

    def _get(url: str) -> httpx.Response:
        seen_urls.append(url)
        if "news.google.com" in url:
            raise httpx.ConnectError("dns failure")
        return _response(url, _feed(age_days=0.1))

    monkeypatch.setattr(rss, "_http_get", _get)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    items = RssNewsProvider().search("pilbara lithium", days=1)

    assert len(items) == 1
    assert items[0].source == "Mining.com"
    assert any("news.google.com" in url for url in seen_urls)
    assert any("mining.com/feed" in url for url in seen_urls)


def test_search_applies_local_keyword_filter_for_feeds_without_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google News 无结果时落到 Mining.com，本地关键词过滤应生效。"""

    def _get(url: str) -> httpx.Response:
        if "news.google.com" in url:
            return _response(url, "<rss version='2.0'><channel/></rss>")
        return _response(
            url,
            _feed(
                age_days=0.1,
                title="Copper mine expands output",
                summary_html="&lt;p&gt;Copper concentrator ramp-up continues&lt;/p&gt;",
            ),
        )

    monkeypatch.setattr(rss, "_http_get", _get)

    with pytest.raises(RssFetchError, match="全部 RSS 源"):
        RssNewsProvider().search("pilbara lithium", days=1)


def test_search_raises_when_all_sources_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    def _get(url: str) -> httpx.Response:
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(rss, "_http_get", _get)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    with pytest.raises(RssFetchError, match="全部 RSS 源"):
        RssNewsProvider().search("pilbara lithium", days=1)


# --- RssNewsProvider.fetch_article ------------------------------------------


def test_fetch_article_extracts_text_and_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    page = (
        "<html><head><title>Pilbara lithium output rises</title>"
        '<meta property="article:published_time" content="2026-09-17T08:00:00Z">'
        "</head><body><article><p>Output <b>rose</b> sharply.</p>"
        "<script>var tracker = 1;</script></article></body></html>"
    )
    monkeypatch.setattr(rss, "_http_get", _stub_get(page))

    article = RssNewsProvider().fetch_article("https://www.mining.com/a/")

    assert article.title == "Pilbara lithium output rises"
    assert article.source == "www.mining.com"
    assert article.published_at == datetime(2026, 9, 17, 8, 0, tzinfo=UTC)
    assert "Output rose sharply." in article.text
    assert "var tracker" not in article.text, "script 内容不应进入正文"


def test_fetch_article_falls_back_to_now_without_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rss, "_http_get", _stub_get("<html><body><p>Body</p></body></html>"))
    before = datetime.now(UTC)

    article = RssNewsProvider().fetch_article("https://example.com/no-title")

    assert article.title == "https://example.com/no-title"
    assert article.published_at >= before


# --- MockNewsProvider -------------------------------------------------------


def test_mock_provider_has_exactly_eight_items() -> None:
    assert len(mock_module._SPECS) == 8


def test_mock_provider_marks_items_as_degraded() -> None:
    """条目带真实的标题、来源与域名，不标记就与真实报道无从分辨。"""
    items = MockNewsProvider().search("anything", days=7)

    assert items
    assert all(item.degraded for item in items)


def test_mock_provider_marks_articles_as_degraded() -> None:
    provider = MockNewsProvider()
    known = provider.search("anything", days=7)[0]

    assert provider.fetch_article(known.url).degraded
    assert provider.fetch_article("https://example.com/unknown").degraded


def test_real_provider_results_are_not_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实解析出的条目绝不能带降级标记，否则简报会误标可信数据。"""
    monkeypatch.setattr(rss, "_http_get", _stub_get(_feed(age_days=0.1)))

    items = RssNewsProvider().search("pilbara lithium", days=1)

    assert items
    assert not any(item.degraded for item in items)


def test_mock_provider_filters_by_days() -> None:
    provider = MockNewsProvider()

    assert len(provider.search("anything", days=1)) == 1
    assert len(provider.search("anything", days=7)) == 8


def test_mock_provider_timestamps_stay_within_last_seven_days() -> None:
    cutoff = datetime.now(UTC) - timedelta(days=7)

    assert all(item.published_at >= cutoff for item in MockNewsProvider().search("x", days=7))


def test_mock_provider_fetch_article_returns_matching_item() -> None:
    provider = MockNewsProvider()
    known = provider.search("anything", days=7)[0]

    article = provider.fetch_article(known.url)

    assert article.title == known.title
    assert article.text == known.summary


def test_mock_provider_fetch_article_returns_placeholder_for_unknown_url() -> None:
    article = MockNewsProvider().fetch_article("https://example.com/not-in-fixture")

    assert article.url == "https://example.com/not-in-fixture"
    assert article.source == "example.com"
    assert article.text
