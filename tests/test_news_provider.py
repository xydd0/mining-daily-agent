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

from mining_daily_agent.models.news import NewsItem
from mining_daily_agent.providers import BROWSER_USER_AGENT, net
from mining_daily_agent.providers.news import mock as mock_module
from mining_daily_agent.providers.news import rss
from mining_daily_agent.providers.news.mock import MockNewsProvider
from mining_daily_agent.providers.news.rss import (
    RssFetchError,
    RssNewsProvider,
    clean_summary,
)

FEED_URL = "https://example.com/feed"


def _response(url: str, body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        content=body.encode("utf-8"),
        request=httpx.Request("GET", url),
    )


def _stub_get(
    body: str, status: int = 200, seen: list[str] | None = None
) -> Callable[[str], httpx.Response]:
    """返回一个假的 _http_get，始终回同一份内容；``seen`` 可记录请求过的 URL。"""

    def _get(url: str) -> httpx.Response:
        if seen is not None:
            seen.append(url)
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


def test_http_get_sets_explicit_timeout_and_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """超时与大小上限都必须显式给，不能靠库的默认行为。"""
    seen: dict[str, object] = {}

    def _fake(url: str, **kwargs: object) -> httpx.Response:
        seen.update(kwargs)
        return _response(url, "<rss/>")

    monkeypatch.setattr(net, "get_capped", _fake)

    rss._http_get(FEED_URL)

    assert seen["timeout"] == rss.HTTP_TIMEOUT_SECONDS == 15.0
    assert seen["max_bytes"] == net.HTML_MAX_BYTES


def test_http_get_sends_a_browser_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """必须带浏览器 UA：Mining.com 对非浏览器 UA 直接 403（实测）。

    它是降级链里少数给**发布方直链**的源，拿不到它就等于拿不到正文。
    """
    seen: dict[str, object] = {}

    def _fake(url: str, **kwargs: object) -> httpx.Response:
        seen.update(kwargs)
        return _response(url, "<rss/>")

    monkeypatch.setattr(net, "get_capped", _fake)

    rss._http_get(FEED_URL)

    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["User-Agent"] == BROWSER_USER_AGENT
    assert "Mozilla" in str(headers["User-Agent"])


# --- RssNewsProvider.search -------------------------------------------------


def test_search_parses_feed_and_filters_by_days(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rss, "_http_get", _stub_get(_feed(age_days=0.1)))

    items = RssNewsProvider().search("pilbara lithium", days=1)

    assert len(items) == 1
    assert items[0].source == "Bing News"
    assert items[0].summary == "Booming lithium output"


def test_search_drops_items_older_than_requested_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rss, "_http_get", _stub_get(_feed(age_days=10)))

    with pytest.raises(RssFetchError, match="全部 RSS 源"):
        RssNewsProvider().search("pilbara lithium", days=1)


def test_source_order_is_bing_google_mining_yahoo() -> None:
    """Bing 在首位、Google News 第二：正文能不能抓到就看这个顺序。"""
    assert [source.name for source in rss.SOURCES] == [
        "Bing News",
        "Google News",
        "Mining.com",
        "Yahoo Finance",
    ]


def test_search_falls_through_to_next_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """前两个源都挂了就落到 Mining.com——降级链逐级往下走，任一命中即返回。"""
    seen_urls: list[str] = []

    def _get(url: str) -> httpx.Response:
        seen_urls.append(url)
        if "bing.com" in url or "news.google.com" in url:
            raise httpx.ConnectError("dns failure")
        return _response(url, _feed(age_days=0.1))

    monkeypatch.setattr(rss, "_http_get", _get)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    items = RssNewsProvider().search("pilbara lithium", days=1)

    assert len(items) == 1
    assert items[0].source == "Mining.com"
    assert any("bing.com" in url for url in seen_urls)
    assert any("news.google.com" in url for url in seen_urls)
    assert any("mining.com/feed" in url for url in seen_urls)


def test_search_asks_bing_with_a_url_encoded_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """关键词要 URL 编码后填进模板，空格与 OR 都不能原样丢进 query string。"""
    seen_urls: list[str] = []
    monkeypatch.setattr(rss, "_http_get", _stub_get(_feed(age_days=0.1), seen=seen_urls))

    RssNewsProvider().search("Pilbara Minerals OR Pilgangoora", days=1)

    assert seen_urls, "应当请求过"
    assert "bing.com/news/search" in seen_urls[0]
    assert "q=Pilbara+Minerals+OR+Pilgangoora" in seen_urls[0]
    assert "format=RSS" in seen_urls[0]


def test_bing_entries_are_parsed_like_any_other_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bing 的 RSS 同样是 title/link/description/pubDate 结构，沿用同一套解析。"""
    monkeypatch.setattr(
        rss,
        "_http_get",
        _stub_get(
            _feed(
                age_days=0.1,
                title="Pilbara Minerals lifts output guidance",
                summary_html="&lt;p&gt;Spodumene &lt;b&gt;output&lt;/b&gt; rose.&lt;/p&gt;",
            )
        ),
    )

    items = RssNewsProvider().search("Pilbara Minerals", days=7)

    assert len(items) == 1
    assert items[0].source == "Bing News"
    assert items[0].title == "Pilbara Minerals lifts output guidance"
    assert items[0].summary == "Spodumene output rose.", "description 要走同一套清洗与截断"
    assert len(items[0].summary) <= rss.SUMMARY_MAX_CHARS


def test_search_applies_local_keyword_filter_for_feeds_without_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """带查询的源无结果时落到 Mining.com，本地关键词过滤应生效。"""

    def _get(url: str) -> httpx.Response:
        if "bing.com" in url or "news.google.com" in url:
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


def test_query_terms_splits_on_the_or_joiner() -> None:
    """拼好的 "A OR B" 要按连接符切回原始关键词，不能按空白切。"""
    assert rss.query_terms("Pilbara OR lithium") == ["Pilbara", "lithium"]
    assert rss.query_terms("Pilbara lithium mine OR Pilgangoora") == [
        "Pilbara lithium mine",
        "Pilgangoora",
    ]
    assert rss.query_terms("Pilbara Minerals") == ["Pilbara Minerals"]


def test_or_is_not_a_keyword() -> None:
    """实测的坑：把 "A OR B" 按空白切开后，"or" 也成了关键词。

    于是任何标题里带 "or" 的词（Exploration、Resources、Report…）都被判为命中，
    备用源的本地过滤形同虚设。关键词是 ``["Pilbara", "lithium"]`` 时，一条只讲
    铜矿勘探的新闻不该被放进来。
    """
    item = NewsItem(
        title="Copper exploration report",
        url="https://example.com/a",
        source="Example",
        published_at=datetime.now(UTC),
        summary="Drilling results and resource estimates.",
    )

    assert not rss._matches_query(item, "Pilbara OR lithium")


def test_local_filter_matches_any_keyword() -> None:
    item = NewsItem(
        title="Pilbara spodumene shipments rise",
        url="https://example.com/a",
        source="Example",
        published_at=datetime.now(UTC),
        summary="Lithium volumes up.",
    )

    assert rss._matches_query(item, "Pilbara OR lithium"), "命中第一个关键词"
    assert rss._matches_query(item, "Greenbushes OR lithium"), "命中第二个关键词"
    assert not rss._matches_query(item, "Greenbushes OR Wodgina"), "都没有才不命中"


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
        "</head><body><article><p>Pilbara Minerals said output rose sharply "
        "in the September quarter, beating guidance.</p>"
        "<script>var tracker = 1;</script></article></body></html>"
    )
    monkeypatch.setattr(rss, "_http_get", _stub_get(page))

    article = RssNewsProvider().fetch_article("https://www.mining.com/a/")

    assert article.title == "Pilbara lithium output rises"
    assert article.source == "www.mining.com"
    assert article.published_at == datetime(2026, 9, 17, 8, 0, tzinfo=UTC)
    assert "output rose sharply" in article.text
    assert "var tracker" not in article.text, "script 内容不应进入正文"


def test_fetch_article_rejects_a_body_that_is_too_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """抓回来的不是正文（JS 壳、登录墙、反爬页）时**当成失败**，不返回空壳。

    返回一个看着成功的空正文会让上层以为抓到了：实测 Google News 的中转页就是
    这般——200、正文 0 字符。报失败才能走既有的降级路径。
    """
    monkeypatch.setattr(rss, "_http_get", _stub_get("<html><body><p>Short body.</p></body></html>"))

    with pytest.raises(RssFetchError, match="正文过短"):
        RssNewsProvider().fetch_article("https://www.mining.com/short/")


def test_fetch_article_truncates_very_long_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实页面可达十几万字符，原样返回会撑爆调用方（尤其 LLM）的上下文。"""
    huge = "<html><body><article><p>" + ("锂矿正文。" * 5000) + "</p></article></body></html>"
    monkeypatch.setattr(rss, "_http_get", _stub_get(huge))

    article = RssNewsProvider().fetch_article("https://www.mining.com/huge/")

    assert len(article.text) == rss.ARTICLE_TEXT_MAX_CHARS == 8000
    assert article.text.endswith("…")


def test_fetch_article_keeps_a_just_long_enough_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """刚好过线的正文照常返回——阈值不能把正常短文也挡在外面。"""
    body = "Pilbara Minerals reported quarterly output of 1.2 Mt of spodumene concentrate."
    assert len(body) >= rss.MIN_ARTICLE_CHARS
    monkeypatch.setattr(rss, "_http_get", _stub_get(f"<html><body><p>{body}</p></body></html>"))

    article = RssNewsProvider().fetch_article("https://www.mining.com/brief/")

    assert article.text == body


def test_fetch_article_falls_back_to_now_without_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    body = "Pilbara Minerals said quarterly spodumene output rose above guidance."
    monkeypatch.setattr(rss, "_http_get", _stub_get(f"<html><body><p>{body}</p></body></html>"))
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
