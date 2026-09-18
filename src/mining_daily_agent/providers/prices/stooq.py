"""真实价格数据源：Yahoo Finance 优先，Stooq 兜底。

**为什么是两个源**：LME 金属现货行情没有免费 API，只能用上市代理品种代替
（见 ``PROXY_SYMBOLS``）。而 Stooq 的免费 CSV 端点已被 JavaScript 工作量证明
反爬挡住——实测默认 UA 返回 404、浏览器 UA 返回挑战页，任何非 JS 客户端都拿不到
数据。因此改用 Yahoo Finance 作为首选，Stooq 降为兜底保留（其策略调整后仍可用）。

**代理品种的含义**：取到的是上市工具（ETF / 矿业公司）的**份额价格**，
不是每吨金属价，也不是伦敦现货报价。这一点会写进 ``PricePoint.unit`` 与
``PricePoint.source``，调用方必须据此判断数值含义。
"""

from __future__ import annotations

import csv
import io
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final, override

import httpx

from mining_daily_agent.models.prices import PricePoint, TrendSeries
from mining_daily_agent.providers import BROWSER_USER_AGENT
from mining_daily_agent.providers.prices.base import (
    PriceProvider,
    PriceUnavailableError,
    UnsupportedCommodityError,
    build_trend_series,
    normalize_commodity,
    parse_iso_date,
    supported_commodities_text,
)

logger = logging.getLogger(__name__)

#: 单次 HTTP 请求的超时秒数。
HTTP_TIMEOUT_SECONDS: Final = 15.0
#: 指数退避重试的最大尝试次数（含首次）。
MAX_ATTEMPTS: Final = 3
#: 退避基数：第 n 次失败后等待 BACKOFF_BASE_SECONDS * 2**(n-1) 秒。
BACKOFF_BASE_SECONDS: Final = 0.5

STOOQ_URL_TEMPLATE: Final = "https://stooq.com/q/d/l/?s={symbol}&i=d"
YAHOO_URL_TEMPLATE: Final = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


#: Yahoo 上回看几天的日线；90 个交易日需要留出余量，取 6 个月。
YAHOO_RANGE: Final = "6mo"


class PriceFetchError(RuntimeError):
    """行情源不可用：网络失败、响应格式不对，或全部源都没有数据。"""


@dataclass(frozen=True, slots=True)
class ProxySymbol:
    """一个品种的代理工具。

    Attributes:
        commodity: 规范品种名。
        stooq: Stooq 代码。
        yahoo: Yahoo Finance 代码。
        label: 人类可读的代理工具说明，会出现在 ``PricePoint.source`` 里。
    """

    commodity: str
    stooq: str
    yahoo: str
    label: str


#: 品种 → 代理工具。**这些都不是 LME 报价**，只是与对应金属价格有相关性的
#: 上市工具；LME 金属现货没有免费 API，故以此代理。
#: nickel 原本计划用 JJN.US（iPath 镍 ETN），实测该品种已无数据，改用 VALE。
PROXY_SYMBOLS: Final[tuple[ProxySymbol, ...]] = (
    ProxySymbol(
        commodity="lithium",
        stooq="lit.us",
        yahoo="LIT",
        label="Global X Lithium & Battery Tech ETF",
    ),
    ProxySymbol(
        commodity="nickel",
        stooq="vale.us",
        yahoo="VALE",
        label="Vale SA, nickel-exposed miner",
    ),
    ProxySymbol(
        commodity="copper",
        stooq="copx.us",
        yahoo="COPX",
        label="Global X Copper Miners ETF",
    ),
    ProxySymbol(
        commodity="cobalt",
        stooq="remx.us",
        yahoo="REMX",
        label="VanEck Rare Earth & Strategic Metals ETF",
    ),
)


def resolve_proxy(commodity: str) -> ProxySymbol:
    """把品种名解析成代理工具。

    Raises:
        UnsupportedCommodityError: 品种不受支持。
    """
    normalized = normalize_commodity(commodity)
    for proxy in PROXY_SYMBOLS:
        if proxy.commodity == normalized:
            return proxy
    msg = f"不支持的品种：{commodity!r}。当前支持：{supported_commodities_text()}。"
    raise UnsupportedCommodityError(msg)


def source_label(source_name: str, symbol: str, proxy: ProxySymbol) -> str:
    """构造写进 ``PricePoint.source`` 的溯源标签。"""
    return (
        f"{source_name}: {symbol} ({proxy.label}) — proxy quote, "
        f"not an LME {proxy.commodity} spot price"
    )


def _http_get(url: str, headers: Mapping[str, str] | None = None) -> httpx.Response:
    """发出单次 GET。

    这是本模块唯一的 HTTP 接缝：超时在此统一设置，测试也在这里替换。
    """
    return httpx.get(url, timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True, headers=headers)


def _get_response(
    url: str, source_name: str, headers: Mapping[str, str] | None = None
) -> httpx.Response:
    """带超时与指数退避重试的 GET。

    Raises:
        PriceFetchError: 连续失败 ``MAX_ATTEMPTS`` 次。
    """
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = _http_get(url, headers)
            response.raise_for_status()
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            last_error = exc
            will_retry = attempt < MAX_ATTEMPTS
            logger.warning(
                "行情请求失败：source=%s url=%s attempt=%d/%d retrying=%s error=%s: %s",
                source_name,
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
            return response

    msg = (
        f"{source_name} 请求 {url} 连续失败 {MAX_ATTEMPTS} 次，"
        f"最后一次错误：{type(last_error).__name__}: {last_error}"
    )
    raise PriceFetchError(msg) from last_error


def parse_stooq_csv(text: str) -> list[tuple[date, float]]:
    """解析 Stooq 的 ``Date,Open,High,Low,Close,Volume`` CSV，取收盘价。

    Raises:
        PriceFetchError: 内容不是行情 CSV，或没有可用行。
    """
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    if "Close" not in fieldnames or "Date" not in fieldnames:
        # 反爬挑战页、错误页都会走到这里。
        msg = f"Stooq 返回的不是行情 CSV（表头：{fieldnames[:6]}）"
        raise PriceFetchError(msg)

    rows: list[tuple[date, float]] = []
    for row in reader:
        raw_date = _text(row.get("Date"))
        raw_close = _text(row.get("Close"))
        if not raw_date or not raw_close:
            continue
        try:
            rows.append((date.fromisoformat(raw_date), float(raw_close)))
        except ValueError:
            continue

    if not rows:
        msg = "Stooq CSV 中没有可用的收盘价行"
        raise PriceFetchError(msg)
    return rows


def _text(value: object) -> str:
    """把任意值安全转成去空白的字符串。"""
    return value.strip() if isinstance(value, str) else ""


def _dig(node: object, *keys: str) -> object:
    """逐层读取嵌套 mapping 的字段；任一层缺失或类型不符就返回 None。"""
    current = node
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def parse_yahoo_chart(payload: object) -> list[tuple[date, float]]:
    """解析 Yahoo chart API：``chart.result[0].timestamp`` 配 ``indicators.quote[0].close``。

    Raises:
        PriceFetchError: 结构不符或没有可用收盘价。
    """
    results = _dig(payload, "chart", "result")
    if not isinstance(results, list) or not results:
        msg = "Yahoo 返回中没有 chart.result"
        raise PriceFetchError(msg)

    first = results[0]
    timestamps = _dig(first, "timestamp")
    quotes = _dig(first, "indicators", "quote")
    if not isinstance(timestamps, list) or not isinstance(quotes, list) or not quotes:
        msg = "Yahoo 返回中缺少 timestamp 或 quote"
        raise PriceFetchError(msg)

    closes = _dig(quotes[0], "close")
    if not isinstance(closes, list):
        msg = "Yahoo 返回中缺少收盘价序列"
        raise PriceFetchError(msg)

    rows: list[tuple[date, float]] = []
    for stamp, close in zip(timestamps, closes, strict=False):
        # 停牌日 Yahoo 用 null 占位，跳过。
        if not isinstance(stamp, int | float) or not isinstance(close, int | float):
            continue
        rows.append((datetime.fromtimestamp(stamp, tz=UTC).date(), float(close)))

    if not rows:
        msg = "Yahoo 返回中没有有效的收盘价"
        raise PriceFetchError(msg)
    return rows


def _fetch_from_stooq(proxy: ProxySymbol) -> list[tuple[date, float]]:
    """从 Stooq 取日线。当前环境下必然失败，保留以备其反爬策略调整。"""
    url = STOOQ_URL_TEMPLATE.format(symbol=proxy.stooq)
    response = _get_response(url, source_name="Stooq")
    return parse_stooq_csv(response.text)


def _fetch_from_yahoo(proxy: ProxySymbol) -> list[tuple[date, float]]:
    """从 Yahoo Finance 取日线。"""
    url = YAHOO_URL_TEMPLATE.format(symbol=proxy.yahoo)
    response = _get_response(
        url,
        source_name="Yahoo Finance",
        headers={"User-Agent": BROWSER_USER_AGENT, "Accept": "application/json"},
    )
    return parse_yahoo_chart(response.json())


@dataclass(frozen=True, slots=True)
class PriceSource:
    """一个行情源。"""

    name: str
    symbol_of: Callable[[ProxySymbol], str]
    fetch: Callable[[ProxySymbol], list[tuple[date, float]]]


#: 按优先级排列的行情源，顺序即尝试顺序。
#:
#: **Yahoo 在前、Stooq 在后**：Stooq 的 CSV 端点被 JS 反爬挡住，每次请求必然失败
#: 并耗尽三次重试与退避（实测约 5 秒）才轮到下一个源。把它放在首位等于每次调用
#: 先白等。Stooq 实现保留为兜底——若其反爬策略调整，调换这里两行即可恢复。
SOURCES: Final[tuple[PriceSource, ...]] = (
    PriceSource(
        name="Yahoo Finance",
        symbol_of=lambda proxy: proxy.yahoo,
        fetch=_fetch_from_yahoo,
    ),
    PriceSource(
        name="Stooq",
        symbol_of=lambda proxy: proxy.stooq,
        fetch=_fetch_from_stooq,
    ),
)


def _fetch_daily_closes(proxy: ProxySymbol) -> tuple[list[tuple[date, float]], str, str]:
    """依次尝试各行情源，第一个产出数据的即被采用。

    Returns:
        ``(按日期升序的收盘价序列, 源名, 该源使用的代码)``。

    Raises:
        PriceFetchError: 全部源都失败或都没有数据。
    """
    attempts: list[str] = []
    for source in SOURCES:
        symbol = source.symbol_of(proxy)
        try:
            rows = source.fetch(proxy)
        except PriceFetchError as exc:
            attempts.append(f"{source.name}: {exc}")
            continue
        if rows:
            logger.info(
                "行情命中：source=%s symbol=%s commodity=%s points=%d",
                source.name,
                symbol,
                proxy.commodity,
                len(rows),
            )
            return sorted(rows), source.name, symbol
        attempts.append(f"{source.name}: 无数据")

    msg = (
        f"{proxy.commodity} 的全部行情源均不可用（代理代码 stooq={proxy.stooq}, "
        f"yahoo={proxy.yahoo}）：" + "；".join(attempts)
    )
    raise PriceFetchError(msg)


class StooqPriceProvider(PriceProvider):
    """真实实现：按优先级尝试 Stooq 与 Yahoo Finance。"""

    @override
    def get_price(self, commodity: str, date: str | None) -> PricePoint:
        """取指定交易日的价格；``date`` 为 None 时取最新交易日。

        Raises:
            UnsupportedCommodityError: 品种不受支持。
            PriceUnavailableError: 该交易日没有行情（例如非交易日）。
            PriceFetchError: 全部行情源均不可用。
        """
        proxy = resolve_proxy(commodity)
        rows, source_name, symbol = _fetch_daily_closes(proxy)

        if date is None:
            day, close = rows[-1]
        else:
            target = parse_iso_date(date)
            matched = next((row for row in rows if row[0] == target), None)
            if matched is None:
                day, close = _nearest_row(rows, target, proxy)
            else:
                day, close = matched

        return PricePoint(
            commodity=proxy.commodity,
            date=day,
            price=round(close, 4),
            currency="USD",
            # 代理工具是上市份额，单位绝不是每吨金属价。
            unit="USD/share",
            source=source_label(source_name, symbol, proxy),
        )

    @override
    def get_trend(self, commodity: str, days: int = 30) -> TrendSeries:
        """取最近 ``days`` 个交易日的走势。

        涨跌幅、极值与均线均由 ``build_trend_series`` 统一计算。

        Raises:
            UnsupportedCommodityError: 品种不受支持。
            PriceFetchError: 全部行情源均不可用。
        """
        proxy = resolve_proxy(commodity)
        rows, source_name, symbol = _fetch_daily_closes(proxy)
        window = rows[-days:]

        points = [
            PricePoint(
                commodity=proxy.commodity,
                date=day,
                price=round(close, 4),
                currency="USD",
                unit="USD/share",
                source=source_label(source_name, symbol, proxy),
            )
            for day, close in window
        ]
        return build_trend_series(proxy.commodity, points, source_label(source_name, symbol, proxy))


def _nearest_row(
    rows: list[tuple[date, float]], target: date, proxy: ProxySymbol
) -> tuple[date, float]:
    """在请求日期没有行情时，报错并给出最近的可交易日。

    Raises:
        PriceUnavailableError: 总是抛出。
    """
    nearest = min(rows, key=lambda row: abs((row[0] - target).days))
    msg = (
        f"{proxy.commodity} 在 {target.isoformat()} 没有行情（可能是非交易日）。"
        f"最近的可交易日是 {nearest[0].isoformat()}。"
    )
    raise PriceUnavailableError(msg)
