"""基于 RSS 的真实新闻数据源。

按固定优先级依次尝试多个公开 RSS 源，第一个产出可用结果的源即被采用。
所有 HTTP 请求都经由 :func:`_http_get` 这一唯一接缝发出，因此超时与重试
策略只有一处实现（见 CLAUDE.md「可靠性」）。
"""

from __future__ import annotations

import html
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Final, override
from urllib.parse import quote_plus, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup, Tag

from mining_daily_agent.models.news import Article, NewsItem
from mining_daily_agent.providers import BROWSER_USER_AGENT, net
from mining_daily_agent.providers.news.base import NewsProvider

logger = logging.getLogger(__name__)

#: 单次 HTTP 请求的超时秒数。
HTTP_TIMEOUT_SECONDS: Final = 15.0
#: 指数退避重试的最大尝试次数（含首次）。
MAX_ATTEMPTS: Final = 3
#: 退避基数：第 n 次失败后等待 BACKOFF_BASE_SECONDS * 2**(n-1) 秒。
BACKOFF_BASE_SECONDS: Final = 0.5
#: 摘要截断长度。
SUMMARY_MAX_CHARS: Final = 500
#: ``fetch_article`` 返回正文的字符上限。真实文章可达数十万字符，原样返回会把调用方
#: （尤其 LLM）的上下文撑爆。
ARTICLE_TEXT_MAX_CHARS: Final = 8000
#: 正文短于这个长度即视为**没抓到**（JS 壳、登录墙、反爬页都会返回一个空壳页面）。
#: 宁可报失败走降级路径，也不要返回一个看着成功的空正文。
MIN_ARTICLE_CHARS: Final = 50

_TAG_RE: Final = re.compile(r"<[^>]+>")
_WHITESPACE_RE: Final = re.compile(r"\s+")
#: 关键词之间的连接符。``FetchPlan`` 把关键词数组拼成 ``"A OR B"`` 再交给工具，
#: 本地过滤时必须按它切回**原始关键词列表**（见 :func:`query_terms`）。
_QUERY_SEPARATOR_RE: Final = re.compile(r"\s+OR\s+", re.IGNORECASE)


class RssFetchError(RuntimeError):
    """RSS 源不可用：网络失败、解析失败，或全部源都没有可用结果。"""


@dataclass(frozen=True, slots=True)
class RssSource:
    """一个 RSS 源。

    Attributes:
        name: 展示与日志用的来源名。
        url: feed 地址；``query_in_url`` 为 True 时含 ``{query}`` 占位符。
        query_in_url: URL 是否已按关键词做了服务端过滤。为 False 时，
            抓回来的条目需要在本地按关键词再过滤一遍。
    """

    name: str
    url: str
    query_in_url: bool


#: 按优先级排列的 RSS 源，顺序即尝试顺序，任一命中即返回。
#:
#: **Bing News 在首位，原因是正文**：Google News 的条目 ``link`` 是
#: ``news.google.com/rss/articles/…`` 的中转页（JS 壳），``fetch_article`` 抓回来
#: 正文 **0 字符**——摘要能用，正文永远拿不到（见 docs/architecture.md 的已知取舍）。
#: Bing 返回的是**发布方直链**，正文抓得到。
SOURCES: Final[tuple[RssSource, ...]] = (
    RssSource(
        name="Bing News",
        url="https://www.bing.com/news/search?q={query}&format=RSS",
        query_in_url=True,
    ),
    RssSource(
        name="Google News",
        url="https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en",
        query_in_url=True,
    ),
    RssSource(
        name="Mining.com",
        url="https://www.mining.com/feed/",
        query_in_url=False,
    ),
    RssSource(
        name="Yahoo Finance",
        url="https://finance.yahoo.com/news/rssindex",
        query_in_url=False,
    ),
)


def _http_get(url: str) -> httpx.Response:
    """发出单次 GET。

    这是本模块唯一的 HTTP 接缝：超时、请求头、大小上限与安全护栏都在这里统一设置，
    测试也在这里替换。正文走 5 MB 上限——HTML 正文不该有 PDF 那么大。

    **必须带浏览器 UA**：Mining.com 对非浏览器 UA 直接 403（实测裸请求 403、带 UA 拿到
    完整 RSS）。这一条直接影响降级链的可用性——Mining.com 是少数给**发布方直链**的源，
    拿不到它就等于拿不到正文。
    """
    return net.get_capped(
        url,
        max_bytes=net.HTML_MAX_BYTES,
        source_name="新闻页",
        timeout=HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": BROWSER_USER_AGENT, "Accept": "application/rss+xml,text/html,*/*"},
    )


def _fetch_bytes(url: str) -> bytes:
    """抓取 URL 内容，失败时按指数退避重试，最多 ``MAX_ATTEMPTS`` 次。"""
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = _http_get(url)
            response.raise_for_status()
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            last_error = exc
            will_retry = attempt < MAX_ATTEMPTS
            logger.warning(
                "HTTP 请求失败：url=%s attempt=%d/%d retrying=%s error=%s: %s",
                url,
                attempt,
                MAX_ATTEMPTS,
                will_retry,
                type(exc).__name__,
                exc,
            )
            if will_retry:
                time.sleep(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
        else:
            return response.content

    msg = (
        f"请求 {url} 连续失败 {MAX_ATTEMPTS} 次，"
        f"最后一次错误：{type(last_error).__name__}: {last_error}"
    )
    raise RssFetchError(msg) from last_error


def truncate_text(text: str, limit: int) -> str:
    """截断到 ``limit`` 字符；被截断时以省略号结尾，总长仍是 ``limit``。"""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def clean_summary(raw: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    """清洗摘要：去 HTML 标签、反转义实体、压缩空白，并截断到 ``limit`` 字符。"""
    text = _WHITESPACE_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", raw))).strip()
    return truncate_text(text, limit)


def _text(value: object) -> str:
    """把任意值安全转成去空白的字符串。"""
    return value.strip() if isinstance(value, str) else ""


def _parse_datetime(value: object) -> datetime | None:
    """尽力把时间字段解析成带时区的 datetime，失败返回 None。"""
    if isinstance(value, time.struct_time):
        return datetime(
            value.tm_year,
            value.tm_mon,
            value.tm_mday,
            value.tm_hour,
            value.tm_min,
            value.tm_sec,
            tzinfo=UTC,
        )
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _entry_datetime(entry: Mapping[str, object]) -> datetime | None:
    """从 RSS entry 中取出发布时间，兼容 feedparser 解析结果与原始字符串。"""
    for key in ("published_parsed", "updated_parsed", "published", "updated"):
        parsed = _parse_datetime(entry.get(key))
        if parsed is not None:
            return parsed
    return None


def _to_news_item(entry: object, source_name: str) -> NewsItem | None:
    """把 feedparser 的 entry 转成 NewsItem；缺标题/链接/时间时返回 None。"""
    if not isinstance(entry, Mapping):
        return None
    title = _text(entry.get("title"))
    url = _text(entry.get("link"))
    published_at = _entry_datetime(entry)
    if not title or not url or published_at is None:
        return None
    summary = _text(entry.get("summary")) or _text(entry.get("description"))
    return NewsItem(
        title=title,
        url=url,
        source=source_name,
        published_at=published_at,
        summary=clean_summary(summary),
    )


def query_terms(query: str) -> list[str]:
    """把检索串还原成**原始关键词列表**：``"Pilbara OR lithium"`` → ``["Pilbara", "lithium"]``。

    实测的坑：早先直接 ``query.split()``，把拼好的 ``"A OR B"`` 按空白切开，**"or" 也成了
    一个关键词**——于是任何标题里带 "or" 的词（Exploration、Resources、Report…）都被判为
    命中，备用源的本地过滤形同虚设：抓回来的东西跟查询词毫无关系。
    """
    return [term.strip() for term in _QUERY_SEPARATOR_RE.split(query) if term.strip()]


def _matches_query(item: NewsItem, query: str) -> bool:
    """本地关键词过滤：任一**关键词**出现在标题或摘要中即算命中。

    匹配的是原始关键词（可以是词组），不是拆散的单字——拆开会让 "mine"、"or" 这类词
    把无关条目放进来。
    """
    haystack = f"{item.title} {item.summary}".casefold()
    return any(term.casefold() in haystack for term in query_terms(query))


def _meta_published(soup: BeautifulSoup) -> datetime | None:
    """从常见 meta 标签中取发布时间。"""
    for key in ("article:published_time", "og:published_time", "datePublished"):
        tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        if not isinstance(tag, Tag):
            continue
        parsed = _parse_datetime(tag.get("content"))
        if parsed is not None:
            return parsed
    return None


class RssNewsProvider(NewsProvider):
    """按优先级尝试多个 RSS 源的真实实现。"""

    @override
    def search(self, query: str, days: int) -> list[NewsItem]:
        """检索最近 ``days`` 天内的新闻，第一个产出结果的源即被采用。

        Raises:
            RssFetchError: 全部源都请求失败或都没有匹配结果。
        """
        cutoff = datetime.now(UTC) - timedelta(days=days)
        attempts: list[str] = []
        for source in SOURCES:
            try:
                items = self._search_source(source, query, cutoff)
            except RssFetchError as exc:
                attempts.append(f"{source.name}: {exc}")
                continue
            if items:
                logger.info(
                    "新闻检索命中：source=%s query=%r days=%d count=%d",
                    source.name,
                    query,
                    days,
                    len(items),
                )
                return items
            attempts.append(f"{source.name}: 无符合条件的结果")

        msg = f"全部 RSS 源均未返回结果（query={query!r}, days={days}）：" + "；".join(attempts)
        raise RssFetchError(msg)

    @override
    def fetch_article(self, url: str) -> Article:
        """抓取并解析指定 URL 的正文。

        Raises:
            RssFetchError: 网络请求失败。
        """
        soup = BeautifulSoup(_fetch_bytes(url), "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        title = soup.title.get_text(strip=True) if soup.title else ""
        body = soup.find("article") or soup.body or soup
        # 正文必须截断：真实页面动辄十几万字符，原样返回会撑爆调用方上下文。
        text = truncate_text(
            _WHITESPACE_RE.sub(" ", body.get_text(separator=" ", strip=True)).strip(),
            ARTICLE_TEXT_MAX_CHARS,
        )
        if len(text) < MIN_ARTICLE_CHARS:
            # 抓回来的不是一篇正文（JS 壳、登录墙、反爬页）。**当成失败**而不是返回
            # 一个空壳：上层据此走既有的降级路径，与其它抓取失败一视同仁。
            msg = f"正文过短（{len(text)} 字符 < {MIN_ARTICLE_CHARS}），多半是 JS 壳或反爬页：{url}"
            raise RssFetchError(msg)

        return Article(
            title=title or url,
            url=url,
            source=urlparse(url).netloc,
            published_at=_meta_published(soup) or datetime.now(UTC),
            text=text,
        )

    def _search_source(self, source: RssSource, query: str, cutoff: datetime) -> list[NewsItem]:
        """抓取单个源并按关键词与时间过滤。"""
        url = _build_url(source, query)
        parsed = feedparser.parse(_fetch_bytes(url))
        if not parsed.entries:
            msg = f"feed 无条目（url={url}）"
            raise RssFetchError(msg)

        items: list[NewsItem] = []
        for entry in parsed.entries:
            item = _to_news_item(entry, source.name)
            if item is None or item.published_at < cutoff:
                continue
            if not source.query_in_url and not _matches_query(item, query):
                continue
            items.append(item)
        return items


def _build_url(source: RssSource, query: str) -> str:
    """把关键词填入 URL 模板；不含占位符的源原样返回。"""
    if source.query_in_url:
        return source.url.format(query=quote_plus(query))
    return source.url
