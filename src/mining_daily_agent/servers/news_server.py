"""mining-news-mcp：向 LLM 暴露新闻检索与正文抓取两个工具。

启动方式（stdio 传输）::

    uv run python -m mining_daily_agent.servers.news_server

工具的 docstring 会被 MCP 作为工具描述下发给 LLM，LLM 据此选择工具，
因此这里统一用英文撰写，并写清用途与每个参数（见 CLAUDE.md「MCP 约定」）。
"""

# 刻意**不**使用 `from __future__ import annotations`：MCP 注册工具时需要解析
# 真实的返回类型注解来生成 JSON schema，NewsItem / Article 必须在运行时可用。
# 关掉注解延迟求值后，这一约束由解释器本身强制保证，而不是靠注释约定。
import logging
import time
from typing import Final
from urllib.parse import urlparse

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mining_daily_agent.models.news import Article, NewsItem
from mining_daily_agent.providers import net
from mining_daily_agent.providers.news import get_news_provider
from mining_daily_agent.providers.news.mock import MockNewsProvider

logger = logging.getLogger(__name__)

SERVER_NAME: Final = "mining-news-mcp"

#: days 参数的默认值与合法区间；越界会被静默钳制而不是报错。
DEFAULT_DAYS: Final = 1
MIN_DAYS: Final = 1
MAX_DAYS: Final = 30

mcp = MCPServer(SERVER_NAME)


def _clamp_days(days: int) -> int:
    """把 days 钳制到 [MIN_DAYS, MAX_DAYS]。"""
    return max(MIN_DAYS, min(MAX_DAYS, days))


class InvalidArticleUrlError(ValueError, ToolError):
    """文章 URL 非法。

    刻意同时继承两个基类：

    - ``ValueError`` 让校验本身与 MCP 解耦，是常规的入参错误语义；
    - ``ToolError`` 是 MCP 唯一会**保留原始 message** 转发给调用方的异常类型。
      SDK 对普通异常只回一句 "Error executing tool xxx"，异常自身的文本留在
      服务端（见 mcp/server/mcpserver/tools/base.py 的 except 分支）。
      不继承它，下面精心写的中文提示就到不了 LLM 那里。
    """


def _validate_url(url: str) -> str:
    """校验 URL 是绝对 http(s) 地址，且不通向本机 / 私网 / 云元数据地址。

    文章 URL 直接来自检索结果，是完全不可信的外部输入。DNS 解析那道检查放在下载
    路径（``net.get_capped``），入参校验不做网络调用。

    Raises:
        InvalidArticleUrlError: URL 非法（同时是 ValueError 与 ToolError）。
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        msg = (
            f"非法的文章 URL：{url!r}。"
            "需要一个绝对的 http(s) 地址，例如 https://www.mining.com/some-article/。"
        )
        raise InvalidArticleUrlError(msg)
    try:
        net.ensure_public_url(url)
    except net.UnsafeUrlError as exc:
        raise InvalidArticleUrlError(f"拒绝访问该文章 URL：{exc}") from exc
    return url


def _search_with_fallback(query: str, days: int) -> tuple[list[NewsItem], bool]:
    """检索新闻，返回 ``(结果, 是否降级)``。

    调用阶段刻意捕获一切异常并回退到 mock：真实源失败不得中断上层流程
    （见 CLAUDE.md「可靠性」）。
    """
    provider = get_news_provider()
    try:
        return provider.search(query, days), False
    except Exception as exc:  # 降级兜底：任何真实源故障都回退，不限定异常类型
        logger.warning(
            "检索失败，降级到 mock：server=%s tool=search provider=%s "
            "query=%r days=%d degraded=true error=%s: %s",
            SERVER_NAME,
            type(provider).__name__,
            query,
            days,
            type(exc).__name__,
            exc,
        )
        return MockNewsProvider().search(query, days), True


@mcp.tool()
def search(query: str, days: int = DEFAULT_DAYS) -> list[NewsItem]:
    """Search recent mining-industry news by keyword.

    Use this to find out what happened recently in mining, metals and
    critical-minerals markets (for example "Pilbara lithium", "copper price",
    "iron ore"). Prefer English keywords: the upstream feeds are English.

    Args:
        query: Keywords to search for, e.g. "Pilbara lithium".
        days: How far back to look, in days. Defaults to 1. Values below 1 are
            treated as 1; values above 30 are capped at 30 (no error is raised).

    Returns:
        News items, each with title, url, source, published_at (ISO-8601 with
        timezone) and a plain-text summary of at most 500 characters. An empty
        list means nothing matched. This tool never raises on upstream failure:
        it falls back to a built-in offline dataset instead.
    """
    clamped = _clamp_days(days)
    started = time.perf_counter()
    items, degraded = _search_with_fallback(query, clamped)
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "工具调用完成：server=%s tool=search query=%r days=%d "
        "count=%d degraded=%s duration_ms=%.1f",
        SERVER_NAME,
        query,
        clamped,
        len(items),
        degraded,
        elapsed_ms,
    )
    return items


@mcp.tool()
def fetch_article(url: str) -> Article:
    """Fetch and extract the full text of a single news article.

    Use this when a summary returned by `search` is not enough and you need the
    article body, for example to quote details such as production figures,
    price levels or company guidance.

    Args:
        url: Absolute http(s) URL of the article, normally the `url` field of a
            `search` result.

    Returns:
        The article with title, url, source, published_at (ISO-8601 with
        timezone) and the extracted plain-text body in `text`.

    Raises:
        InvalidArticleUrlError: If `url` is not an absolute http(s) URL. It is
            both a ValueError and an MCP ToolError, so the explanatory message
            reaches the caller instead of a generic "Error executing tool"
            string.
    """
    valid_url = _validate_url(url)
    started = time.perf_counter()
    provider = get_news_provider()
    try:
        article = provider.fetch_article(valid_url)
        degraded = False
    except Exception as exc:  # 降级兜底：任何真实源故障都回退，不限定异常类型
        logger.warning(
            "正文抓取失败，降级到 mock：server=%s tool=fetch_article provider=%s "
            "url=%s degraded=true error=%s: %s",
            SERVER_NAME,
            type(provider).__name__,
            valid_url,
            type(exc).__name__,
            exc,
        )
        article = MockNewsProvider().fetch_article(valid_url)
        degraded = True

    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "工具调用完成：server=%s tool=fetch_article url=%s "
        "text_len=%d degraded=%s duration_ms=%.1f",
        SERVER_NAME,
        valid_url,
        len(article.text),
        degraded,
        elapsed_ms,
    )
    return article


if __name__ == "__main__":
    mcp.run(transport="stdio")
