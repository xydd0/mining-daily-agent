"""lme-price-mcp 的行为测试。

HTTP 层通过替换 ``price_stooq._http_get`` 这一唯一接缝来 mock，不触网；
退避等待也被替换掉，测试不会真的 sleep（见 CLAUDE.md「测试」）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import override

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from mining_daily_agent.models.prices import PricePoint, TrendSeries
from mining_daily_agent.providers import prices as prices_pkg
from mining_daily_agent.providers.prices import stooq as price_stooq
from mining_daily_agent.providers.prices.base import (
    PriceProvider,
    UnsupportedCommodityError,
    build_trend_series,
    normalize_commodity,
    parse_iso_date,
)
from mining_daily_agent.providers.prices.mock import (
    MOCK_SOURCE,
    SERIES_DAYS,
    MockPriceProvider,
    build_mock_series,
)
from mining_daily_agent.providers.prices.stooq import (
    PriceFetchError,
    StooqPriceProvider,
    parse_stooq_csv,
    parse_yahoo_chart,
    resolve_proxy,
)
from mining_daily_agent.servers import price_server

CSV_TEXT = (
    "Date,Open,High,Low,Close,Volume\n"
    "2026-09-14,70.1,71.2,69.8,70.9,1000\n"
    "2026-09-15,70.9,72.0,70.5,71.5,1200\n"
    "2026-09-16,71.5,71.9,70.1,70.4,900\n"
)

#: Stooq 现在会对非 JS 客户端返回这类挑战页，而不是 CSV。
CHALLENGE_HTML = (
    '<!DOCTYPE html><html><head><meta charset="utf-8">'
    '<meta name="robots" content="noindex,nofollow"></head><body>'
    "<noscript>This site requires JavaScript to verify your browser.</noscript>"
    "</body></html>"
)

_Stub = Callable[[str, Mapping[str, str] | None], httpx.Response]


def _response(
    url: str, text: str, content_type: str = "text/csv", status: int = 200
) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        text=text,
        headers={"content-type": content_type},
        request=httpx.Request("GET", url),
    )


def _stub(
    *,
    stooq_text: str | None = None,
    yahoo_payload: object | None = None,
) -> _Stub:
    """构造一个假的 _http_get：按域名返回不同内容，未提供的源一律失败。"""

    def _get(url: str, headers: Mapping[str, str] | None = None) -> httpx.Response:
        if "stooq.com" in url:
            if stooq_text is None:
                return _response(url, "forbidden", "text/plain", status=403)
            return _response(url, stooq_text, "text/csv")
        if "yahoo" in url:
            if yahoo_payload is None:
                return _response(url, "{}", "application/json", status=500)
            return httpx.Response(
                status_code=200,
                json=yahoo_payload,
                request=httpx.Request("GET", url),
            )
        return _response(url, "", "text/plain", status=404)

    return _get


def _yahoo_payload(days: int = 40, start: float = 100.0) -> dict[str, object]:
    """构造 Yahoo chart API 形状的返回。"""
    last = datetime(2026, 9, 16, tzinfo=UTC)
    stamps = [int((last - timedelta(days=days - 1 - index)).timestamp()) for index in range(days)]
    closes = [start + index for index in range(days)]
    return {
        "chart": {"result": [{"timestamp": stamps, "indicators": {"quote": [{"close": closes}]}}]}
    }


def _points(prices: list[float]) -> list[PricePoint]:
    start = date(2026, 1, 1)
    return [
        PricePoint(
            commodity="lithium",
            date=start + timedelta(days=index),
            price=price,
            unit="USD/t",
            source="test",
        )
        for index, price in enumerate(prices)
    ]


class _RecordingProvider(PriceProvider):
    """记录调用参数的假价格源。"""

    def __init__(self) -> None:
        self.series = build_mock_series("lithium")
        self.price_calls: list[tuple[str, str | None]] = []
        self.trend_calls: list[tuple[str, int]] = []

    @override
    def get_price(self, commodity: str, date: str | None) -> PricePoint:
        self.price_calls.append((commodity, date))
        return self.series[-1]

    @override
    def get_trend(self, commodity: str, days: int = 30) -> TrendSeries:
        self.trend_calls.append((commodity, days))
        return build_trend_series(commodity, self.series[-days:], "recorded")


class _FailingProvider(PriceProvider):
    """模拟真实源在调用阶段整体不可用。"""

    @override
    def get_price(self, commodity: str, date: str | None) -> PricePoint:
        raise RuntimeError("upstream 503")

    @override
    def get_trend(self, commodity: str, days: int = 30) -> TrendSeries:
        raise RuntimeError("upstream 503")


# --- CSV / JSON 解析 --------------------------------------------------------


def test_parse_stooq_csv_reads_closing_prices() -> None:
    rows = parse_stooq_csv(CSV_TEXT)

    assert rows == [
        (date(2026, 9, 14), 70.9),
        (date(2026, 9, 15), 71.5),
        (date(2026, 9, 16), 70.4),
    ]


def test_parse_stooq_csv_rejects_the_current_challenge_page() -> None:
    """Stooq 对非 JS 客户端返回挑战页，必须被当成格式错误而不是解析出垃圾数据。"""
    with pytest.raises(PriceFetchError, match="不是行情 CSV"):
        parse_stooq_csv(CHALLENGE_HTML)


def test_parse_stooq_csv_skips_unparsable_rows() -> None:
    text = "Date,Open,High,Low,Close,Volume\n2026-09-14,1,1,1,,100\n2026-09-15,1,1,1,7.5,100\n"

    rows = parse_stooq_csv(text)

    assert rows == [(date(2026, 9, 15), 7.5)]


def test_parse_yahoo_chart_reads_closes_and_skips_nulls() -> None:
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1_700_000_000, 1_700_086_400, 1_700_172_800],
                    "indicators": {"quote": [{"close": [10.0, None, 30.0]}]},
                }
            ]
        }
    }

    rows = parse_yahoo_chart(payload)

    assert [price for _, price in rows] == [10.0, 30.0]


def test_parse_stooq_csv_rejects_headers_without_rows() -> None:
    with pytest.raises(PriceFetchError, match="没有可用的收盘价行"):
        parse_stooq_csv("Date,Open,High,Low,Close,Volume\n")


def test_parse_yahoo_chart_rejects_empty_result() -> None:
    with pytest.raises(PriceFetchError, match=r"chart\.result"):
        parse_yahoo_chart({"chart": {"result": []}})


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"chart": "not-a-mapping"}, r"chart\.result"),
        ({"chart": {"result": [{"timestamp": "x"}]}}, "缺少 timestamp"),
        ({"chart": {"result": [{"indicators": {"quote": [{}]}}]}}, "缺少 timestamp"),
        (
            {"chart": {"result": [{"timestamp": [1], "indicators": {"quote": [{}]}}]}},
            "缺少收盘价序列",
        ),
        (
            {"chart": {"result": [{"timestamp": [1], "indicators": {"quote": [{"close": []}]}}]}},
            "没有有效的收盘价",
        ),
    ],
)
def test_parse_yahoo_chart_rejects_malformed_payloads(
    payload: dict[str, object], message: str
) -> None:
    with pytest.raises(PriceFetchError, match=message):
        parse_yahoo_chart(payload)


def test_trend_series_rejects_empty_points() -> None:
    with pytest.raises(ValidationError, match="至少需要一个数据点"):
        TrendSeries(commodity="lithium", points=[], change_pct=0.0, min=1.0, max=2.0, source="t")


def test_trend_series_rejects_an_inverted_range() -> None:
    with pytest.raises(ValidationError, match="不应大于"):
        TrendSeries(
            commodity="lithium",
            points=_points([1.0]),
            change_pct=0.0,
            min=5.0,
            max=1.0,
            source="t",
        )


# --- 场景 1：指定日期 / 缺省日期取价 ----------------------------------------


def test_get_price_for_explicit_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(price_stooq, "_http_get", _stub(stooq_text=CSV_TEXT))

    point = StooqPriceProvider().get_price("lithium", "2026-09-15")

    assert point.date == date(2026, 9, 15)
    assert point.price == pytest.approx(71.5)
    assert point.currency == "USD"


def test_get_price_without_date_takes_the_latest_trading_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(price_stooq, "_http_get", _stub(stooq_text=CSV_TEXT))

    point = StooqPriceProvider().get_price("lithium", None)

    assert point.date == date(2026, 9, 16)
    assert point.price == pytest.approx(70.4)


def test_real_quote_is_labelled_as_a_proxy_not_a_metal_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """代理品种是上市份额，绝不能标成 USD/t，否则 LLM 会把股价当金属价引用。"""
    monkeypatch.setattr(price_stooq, "_http_get", _stub(stooq_text=CSV_TEXT))

    point = StooqPriceProvider().get_price("lithium", None)

    assert point.unit == "USD/share"
    assert "proxy" in point.source


def test_get_trend_labels_every_point_as_a_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实源拿到的走势，每个点都必须带代理标记——不能只标顶层的 source。"""
    monkeypatch.setattr(price_stooq, "_http_get", _stub(stooq_text=CSV_TEXT))

    series = StooqPriceProvider().get_trend("lithium", 3)

    assert len(series.points) == 3
    assert all(point.unit == "USD/share" for point in series.points)
    assert all("proxy" in point.source for point in series.points)
    assert series.source == series.points[0].source
    assert series.change_pct == pytest.approx((70.4 - 70.9) / 70.9 * 100, abs=1e-3)


def test_missing_date_reports_the_nearest_available_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(price_stooq, "_http_get", _stub(stooq_text=CSV_TEXT))

    with pytest.raises(ToolError, match="最近的可交易日是 2026-09-16"):
        price_server.get_price(commodity="lithium", date="2026-09-20")


def test_bad_date_format_is_rejected() -> None:
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        price_server.get_price(commodity="lithium", date="16/09/2026")


def test_commodity_aliases_work_through_the_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(price_stooq, "_http_get", _stub(stooq_text=CSV_TEXT))

    point = price_server.get_price(commodity="Li")

    assert point.commodity == "lithium"


# --- 场景 2：真实源失败降级 mock ---------------------------------------------


def test_falls_back_to_yahoo_when_stooq_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        price_stooq, "_http_get", _stub(yahoo_payload=_yahoo_payload(days=5, start=20.0))
    )
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    point = StooqPriceProvider().get_price("copper", None)

    assert "Yahoo Finance" in point.source
    assert point.price == pytest.approx(24.0)


def test_factory_falls_back_to_mock_when_init_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class _BrokenProvider(StooqPriceProvider):
        def __init__(self) -> None:
            raise RuntimeError("cannot initialise provider")

    monkeypatch.setattr(prices_pkg, "StooqPriceProvider", _BrokenProvider)

    with caplog.at_level(logging.WARNING):
        provider = prices_pkg.get_price_provider()

    assert isinstance(provider, MockPriceProvider)
    assert "降级" in caplog.text
    assert "cannot initialise provider" in caplog.text


def test_tool_falls_back_to_mock_when_every_source_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(price_stooq, "_http_get", _stub())
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    point = price_server.get_price(commodity="lithium")

    assert point.commodity == "lithium"
    assert point.source == MOCK_SOURCE
    assert point.unit == "USD/t", "合成数据才是每吨金属价"


def test_tool_logs_degraded_event(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(price_stooq, "_http_get", _stub())
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    with caplog.at_level(logging.INFO):
        price_server.get_price(commodity="lithium")

    assert "degraded=true" in caplog.text
    assert "server=lme-price-mcp" in caplog.text
    assert "tool=get_price" in caplog.text


def test_trend_falls_back_to_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(price_stooq, "_http_get", _stub())
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    series = price_server.get_trend(commodity="nickel", days=7)

    assert len(series.points) == 7
    assert series.source == MOCK_SOURCE


def test_provider_level_unsupported_commodity_maps_to_a_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工厂返回的实现若自己抛品种错误，也要转成 ToolError 而不是被降级吞掉。

    server 的前置校验让这条分支无法从公开工具进入，但它防的是「工厂返回的实现
    支持范围更窄」——那种情况下把品种错误降级成 mock 会返回另一个品种的数据。
    """

    class _RejectingProvider(PriceProvider):
        @override
        def get_price(self, commodity: str, date: str | None) -> PricePoint:
            raise UnsupportedCommodityError("不支持的品种：'gold'")

        @override
        def get_trend(self, commodity: str, days: int = 30) -> TrendSeries:
            raise UnsupportedCommodityError("不支持的品种：'gold'")

    monkeypatch.setattr(price_server, "get_price_provider", _RejectingProvider)

    with pytest.raises(price_server.UnsupportedCommodityToolError, match="gold"):
        price_server._with_fallback(
            commodity="gold",
            tool="get_price",
            real=lambda provider: provider.get_price("gold", None),
            fallback=lambda provider: provider.get_price("gold", None),
        )


def test_unsupported_commodity_is_not_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    """品种不支持是合法否定，降级只会拿到别的品种的合成数据。"""
    monkeypatch.setattr(price_stooq, "_http_get", _stub(stooq_text=CSV_TEXT))

    with pytest.raises(ToolError, match="不支持的品种"):
        price_server.get_price(commodity="gold")


def test_tool_propagates_source_failure_that_the_mock_also_cannot_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """合成序列覆盖不到该日期时如实报错，不要返回编造的数值。"""
    monkeypatch.setattr(price_stooq, "_http_get", _stub())
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    with pytest.raises(ToolError, match="合成数据"):
        price_server.get_price(commodity="lithium", date="2000-01-03")


# --- 场景 3：days 越界钳制 ---------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(30, 30), (1, 1), (90, 90), (91, 90), (999, 90), (0, 1), (-5, 1)],
)
def test_trend_clamps_days(monkeypatch: pytest.MonkeyPatch, requested: int, expected: int) -> None:
    provider = _RecordingProvider()
    monkeypatch.setattr(price_server, "get_price_provider", lambda: provider)

    price_server.get_trend(commodity="lithium", days=requested)

    assert provider.trend_calls == [("lithium", expected)]


def test_trend_defaults_to_thirty_days(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _RecordingProvider()
    monkeypatch.setattr(price_server, "get_price_provider", lambda: provider)

    price_server.get_trend(commodity="lithium")

    assert provider.trend_calls == [("lithium", 30)]


def test_failing_provider_is_degraded_for_trend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(price_server, "get_price_provider", _FailingProvider)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    series = price_server.get_trend(commodity="lithium", days=5)

    assert series.source == MOCK_SOURCE
    assert len(series.points) == 5


# --- 场景 4：ma7 / ma30 计算 -------------------------------------------------


def test_moving_averages_use_the_last_points() -> None:
    prices = [float(value) for value in range(1, 41)]

    series = build_trend_series("lithium", _points(prices), "test")

    assert series.ma7 == pytest.approx(sum(range(34, 41)) / 7)
    assert series.ma30 == pytest.approx(sum(range(11, 41)) / 30)


def test_moving_averages_are_none_without_enough_points() -> None:
    series = build_trend_series("lithium", _points([1.0, 2.0, 3.0]), "test")

    assert series.ma7 is None
    assert series.ma30 is None


def test_trend_statistics() -> None:
    series = build_trend_series("lithium", _points([10.0, 20.0, 5.0, 40.0]), "test")

    assert series.min == pytest.approx(5.0)
    assert series.max == pytest.approx(40.0)
    assert series.change_pct == pytest.approx(300.0), "(40 - 10) / 10 * 100"
    assert series.points[0].price == pytest.approx(10.0)


def test_empty_points_are_rejected() -> None:
    with pytest.raises(ValueError, match="没有任何价格点"):
        build_trend_series("lithium", [], "test")


def test_mock_ma7_matches_the_last_seven_points() -> None:
    series = MockPriceProvider().get_trend("copper", 10)

    expected = sum(point.price for point in series.points[-7:]) / 7
    assert series.ma7 == pytest.approx(round(expected, 4))


# --- 品种归一与代理映射 ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("lithium", "lithium"), ("  Nickel ", "nickel"), ("Li", "lithium"), ("Cu", "copper")],
)
def test_commodity_aliases_normalize(raw: str, expected: str) -> None:
    assert normalize_commodity(raw) == expected


def test_unknown_commodity_normalizes_to_none() -> None:
    assert normalize_commodity("unobtainium") is None


def test_every_supported_commodity_has_a_proxy() -> None:
    for commodity in ("lithium", "nickel", "copper", "cobalt"):
        proxy = resolve_proxy(commodity)
        assert proxy.stooq
        assert proxy.yahoo
        assert proxy.label


def test_resolve_proxy_rejects_unknown_commodity() -> None:
    with pytest.raises(UnsupportedCommodityError, match="不支持的品种"):
        resolve_proxy("gold")


def test_parse_iso_date_rejects_bad_format() -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        parse_iso_date("2026/09/18")


# --- mock 合成序列 ----------------------------------------------------------


def test_mock_series_has_ninety_points() -> None:
    assert len(build_mock_series("lithium")) == SERIES_DAYS == 90


def test_mock_series_is_reproducible() -> None:
    """固定 seed：同一品种两次生成必须完全一致。"""
    first = build_mock_series("lithium")
    second = build_mock_series("lithium")

    assert [point.price for point in first] == [point.price for point in second]


def test_mock_series_differs_between_commodities() -> None:
    lithium = [point.price for point in build_mock_series("lithium")]
    copper = [point.price for point in build_mock_series("copper")]

    assert lithium != copper


def test_mock_series_skips_weekends() -> None:
    points = build_mock_series("lithium")

    assert all(point.date.weekday() < 5 for point in points)


def test_mock_series_is_labelled_as_synthetic() -> None:
    points = build_mock_series("lithium")

    assert all(point.source == MOCK_SOURCE for point in points)
    assert all(point.unit == "USD/t" for point in points)


def test_mock_provider_returns_price_for_a_known_date() -> None:
    points = build_mock_series("cobalt")

    point = MockPriceProvider().get_price("cobalt", points[10].date.isoformat())

    assert point.price == pytest.approx(points[10].price)


def test_mock_provider_rejects_unknown_commodity() -> None:
    with pytest.raises(UnsupportedCommodityError):
        MockPriceProvider().get_price("gold", None)


def test_mock_provider_reports_dates_outside_the_series() -> None:
    with pytest.raises(ValueError, match="没有行情"):
        MockPriceProvider().get_price("lithium", "2000-01-01")
