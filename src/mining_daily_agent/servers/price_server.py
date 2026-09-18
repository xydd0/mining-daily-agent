"""lme-price-mcp：向 LLM 暴露矿产品价格与走势查询工具。

启动方式（stdio 传输）::

    uv run python -m mining_daily_agent.servers.price_server

工具的 docstring 会被 MCP 作为工具描述下发给 LLM，LLM 据此选择工具，
因此这里用英文撰写，并写清用途与每个参数（见 CLAUDE.md「MCP 约定」）。
"""

# 刻意**不**使用 `from __future__ import annotations`：MCP 注册工具时需要解析
# 真实的返回类型注解来生成 JSON schema，PricePoint / TrendSeries 必须在运行时可用。
import logging
import time
from collections.abc import Callable
from typing import Final

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mining_daily_agent.models.prices import PricePoint, TrendSeries
from mining_daily_agent.providers.prices import get_price_provider
from mining_daily_agent.providers.prices.base import (
    PriceProvider,
    PriceUnavailableError,
    UnsupportedCommodityError,
    normalize_commodity,
    supported_commodities_text,
)
from mining_daily_agent.providers.prices.mock import MockPriceProvider

logger = logging.getLogger(__name__)

SERVER_NAME: Final = "lme-price-mcp"

#: days 参数的默认值与合法区间；越界会被静默钳制而不是报错。
DEFAULT_DAYS: Final = 30
MIN_DAYS: Final = 1
MAX_DAYS: Final = 90

mcp = MCPServer(SERVER_NAME)


class UnsupportedCommodityToolError(UnsupportedCommodityError, ToolError):
    """品种不受支持。

    同时继承 ``ValueError``（框架无关的入参语义）与 ``ToolError``（MCP 唯一会
    **原样转发 message** 的异常类型；普通异常的文本会被 SDK 丢弃）。
    """


class PriceNotAvailableToolError(PriceUnavailableError, ToolError):
    """请求的日期没有行情。

    同为双继承：这属于业务的合法否定，需要把说明原样送到 LLM 那里。
    """


def _resolve_commodity(commodity: str) -> str:
    """把品种名归一成规范名。

    Raises:
        UnsupportedCommodityToolError: 品种不受支持。
    """
    normalized = normalize_commodity(commodity)
    if normalized is None:
        msg = f"不支持的品种：{commodity!r}。当前支持：{supported_commodities_text()}。"
        raise UnsupportedCommodityToolError(msg)
    return normalized


def _clamp_days(days: int) -> int:
    """把 days 钳制到 [MIN_DAYS, MAX_DAYS]。"""
    return max(MIN_DAYS, min(MAX_DAYS, days))


def _with_fallback[T](
    *,
    commodity: str,
    tool: str,
    real: Callable[[PriceProvider], T],
    fallback: Callable[[MockPriceProvider], T],
) -> tuple[T, bool]:
    """调用真实价格源，返回 ``(结果, 是否降级)``。

    只有**行情源故障**才降级到合成序列。「品种不支持」与「该日期没有行情」属于
    业务的合法否定——用合成数据顶替只会凭空捏造一个价格，比直接报错更糟，因此
    这两种情况原样抛出（见 CLAUDE.md「可靠性」）。
    """
    provider = get_price_provider()
    try:
        return real(provider), False
    except UnsupportedCommodityError as exc:
        raise UnsupportedCommodityToolError(str(exc)) from exc
    except PriceUnavailableError as exc:
        raise PriceNotAvailableToolError(str(exc)) from exc
    except Exception as exc:  # 降级兜底：真正的源故障才回退，不限定异常类型
        logger.warning(
            "价格请求失败，降级到 mock：server=%s tool=%s provider=%s commodity=%s "
            "degraded=true error=%s: %s",
            SERVER_NAME,
            tool,
            type(provider).__name__,
            commodity,
            type(exc).__name__,
            exc,
        )
        try:
            return fallback(MockPriceProvider()), True
        except PriceUnavailableError as mock_exc:
            # 合成序列也覆盖不到这个日期：如实报错，不要返回编造的数值。
            raise PriceNotAvailableToolError(str(mock_exc)) from mock_exc


@mcp.tool()
def get_price(commodity: str, date: str | None = None) -> PricePoint:
    """Get the price of a mined commodity on a given day.

    IMPORTANT - read this before quoting any number: there is no free API for
    LME spot prices, so this tool returns the price of a **listed proxy
    instrument** (an ETF or a nickel-exposed miner) quoted in USD **per
    share**. It is NOT a metal price per tonne and NOT an LME quote. Always
    report the `unit` and `source` fields you receive: `source` names the
    instrument and states explicitly that it is a proxy.

    Supported commodities: lithium, nickel, copper, cobalt. Common aliases
    such as "Li", "Ni", "Cu" and "Co" are accepted too.

    Args:
        commodity: Commodity key, e.g. "lithium".
        date: Trading date as YYYY-MM-DD. Omit it to get the most recent
            trading day. Non-trading days are never synthesised: if the date
            has no quote, the error names the nearest available date.

    Returns:
        One price point with commodity, date, price, currency, unit and source.

    Raises:
        UnsupportedCommodityToolError: The commodity is not supported; the
            message lists the supported ones.
        PriceNotAvailableToolError: There is no quote for the requested date.
    """
    resolved = _resolve_commodity(commodity)
    started = time.perf_counter()
    point, degraded = _with_fallback(
        commodity=resolved,
        tool="get_price",
        real=lambda provider: provider.get_price(resolved, date),
        fallback=lambda provider: provider.get_price(resolved, date),
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "工具调用完成：server=%s tool=get_price commodity=%s date=%s price=%.4f "
        "degraded=%s duration_ms=%.1f",
        SERVER_NAME,
        resolved,
        point.date.isoformat(),
        point.price,
        degraded,
        elapsed_ms,
    )
    return point


@mcp.tool()
def get_trend(commodity: str, days: int = DEFAULT_DAYS) -> TrendSeries:
    """Get the recent price trend of a mined commodity.

    Use this to answer "how has X moved lately" questions. It returns a series
    of price points plus the range change, the low/high and the 7- and 30-point
    moving averages.

    IMPORTANT - the same proxy caveat as `get_price` applies: these are prices
    of a **listed proxy instrument** in USD per share, NOT metal prices per
    tonne and NOT LME quotes. Always report `unit` and `source` alongside any
    figure you quote.

    Supported commodities: lithium, nickel, copper, cobalt. Common aliases
    such as "Li", "Ni", "Cu" and "Co" are accepted too.

    Args:
        commodity: Commodity key, e.g. "copper".
        days: How many most recent trading days to cover. Defaults to 30.
            Values below 1 are treated as 1; values above 90 are capped at 90
            (no error is raised).

    Returns:
        A trend series with commodity, points (oldest first), change_pct
        (percentage change from first to last point), min, max, ma7, ma30
        (null when there are fewer than 7 or 30 points) and source. If the
        upstream quote source is unavailable the tool falls back to a
        synthesized series and labels it as such in `source`.

    Raises:
        UnsupportedCommodityToolError: The commodity is not supported; the
            message lists the supported ones.
    """
    resolved = _resolve_commodity(commodity)
    clamped = _clamp_days(days)
    started = time.perf_counter()
    series, degraded = _with_fallback(
        commodity=resolved,
        tool="get_trend",
        real=lambda provider: provider.get_trend(resolved, clamped),
        fallback=lambda provider: provider.get_trend(resolved, clamped),
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "工具调用完成：server=%s tool=get_trend commodity=%s days=%d points=%d "
        "change_pct=%.4f degraded=%s duration_ms=%.1f",
        SERVER_NAME,
        resolved,
        clamped,
        len(series.points),
        series.change_pct,
        degraded,
        elapsed_ms,
    )
    return series


if __name__ == "__main__":
    mcp.run(transport="stdio")
