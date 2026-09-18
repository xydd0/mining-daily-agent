"""内置固定数据的降级新闻源。

数据是手工编写的演示夹具：标题、摘要与来源采用真实矿业媒体的风格，
URL 使用真实媒体域名下的**示意性路径**（非已发布文章的可访问链接）。

发布时间按"当前时间"相对计算，因此数据始终落在最近 7 天内，不会随时间失效。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import override
from urllib.parse import urlparse

from mining_daily_agent.models.news import Article, NewsItem
from mining_daily_agent.providers.news.base import NewsProvider


@dataclass(frozen=True, slots=True)
class _MockSpec:
    """一条 mock 新闻的静态部分。"""

    title: str
    url: str
    source: str
    age_hours: int
    summary: str


#: 8 条 Pilbara 锂矿相关新闻，age_hours 均小于 168（7 天）。
_SPECS: tuple[_MockSpec, ...] = (
    _MockSpec(
        title="Pilbara Minerals lifts spodumene output guidance as Pilgangoora ramps up",
        url="https://www.mining.com/pilbara-minerals-lifts-spodumene-output-guidance/",
        source="Mining.com",
        age_hours=6,
        summary=(
            "Pilbara Minerals has raised its full-year spodumene concentrate guidance after "
            "the Pilgangoora operation in Western Australia's Pilbara region ran above "
            "nameplate throughput for a second consecutive quarter. The company cited "
            "improved recoveries and a lower strip ratio."
        ),
    ),
    _MockSpec(
        title="Lithium carbonate prices steady as Pilbara auction clears above expectations",
        url="https://www.reuters.com/markets/commodities/lithium-carbonate-prices-pilbara-auction-2026/",
        source="Reuters",
        age_hours=30,
        summary=(
            "A closely watched spot auction of spodumene concentrate from the Pilbara drew "
            "bids above analyst expectations, signalling firming demand from Chinese "
            "converters. Traders said the result could put a floor under lithium carbonate "
            "prices in the near term."
        ),
    ),
    _MockSpec(
        title="Mineral Resources reviews Wodgina expansion amid softer spodumene prices",
        url="https://www.afr.com/companies/mining/mineral-resources-reviews-wodgina-expansion-2026/",
        source="Australian Financial Review",
        age_hours=54,
        summary=(
            "Mineral Resources is reassessing the timing of a planned expansion at its "
            "Wodgina lithium mine in the Pilbara, pointing to a softer spodumene price deck. "
            "Current production volumes are unaffected, the company said."
        ),
    ),
    _MockSpec(
        title="Port Hedland lithium exports climb on stronger Chinese offtake",
        url="https://thewest.com.au/business/mining/port-hedland-lithium-exports-climb-2026/",
        source="The West Australian",
        age_hours=78,
        summary=(
            "Lithium concentrate shipments through Port Hedland rose last month as Chinese "
            "offtakers lifted volumes, according to port authority data. The Pilbara gateway "
            "is now the largest single export point for Australian spodumene."
        ),
    ),
    _MockSpec(
        title="Wildcat Resources confirms high-grade lithium strike at Tabba Tabba",
        url="https://www.miningweekly.com/article/wildcat-resources-tabba-tabba-lithium-2026",
        source="Mining Weekly",
        age_hours=102,
        summary=(
            "Wildcat Resources has reported broad high-grade lithium intersections from "
            "drilling at its Tabba Tabba project in the Pilbara, extending mineralisation "
            "along strike. Assays returned spodumene-bearing pegmatite over a 400-metre "
            "corridor."
        ),
    ),
    _MockSpec(
        title="Hancock Prospecting expands Pilbara lithium tenement footprint",
        url="https://www.smh.com.au/business/companies/hancock-prospecting-pilbara-lithium-2026/",
        source="The Sydney Morning Herald",
        age_hours=126,
        summary=(
            "Hancock Prospecting has lodged applications over additional ground adjacent to "
            "its existing Pilbara lithium holdings, according to state mining registry "
            "filings, extending the private company's footprint in the region's spodumene "
            "belts."
        ),
    ),
    _MockSpec(
        title="WA fast-tracks approvals for Pilbara critical minerals projects",
        url="https://www.abc.net.au/news/2026/wa-pilbara-critical-minerals-approvals/",
        source="ABC News",
        age_hours=150,
        summary=(
            "The Western Australian government has added several Pilbara lithium and rare "
            "earths projects to a fast-tracked approvals pathway in an effort to cut "
            "assessment times. Industry groups welcomed the change; environmental advocates "
            "questioned the shortened consultation window."
        ),
    ),
    _MockSpec(
        title="Analysts split on Pilbara lithium recovery as supply discipline tightens",
        url="https://stockhead.com.au/resources/analysts-split-pilbara-lithium-recovery-2026/",
        source="Stockhead",
        age_hours=163,
        summary=(
            "Brokers remain divided on the pace of a Pilbara lithium recovery, with some "
            "pointing to curtailments at high-cost operations and others warning of new "
            "supply from Africa and Brazil. Most expect spodumene contract prices to "
            "stabilise rather than rally."
        ),
    ),
)


def build_mock_items(now: datetime | None = None) -> list[NewsItem]:
    """生成内置新闻条目，发布时间相对 ``now``（默认当前时间）倒推。"""
    reference = now if now is not None else datetime.now(UTC)
    return [
        NewsItem(
            title=spec.title,
            url=spec.url,
            source=spec.source,
            published_at=reference - timedelta(hours=spec.age_hours),
            summary=spec.summary,
        )
        for spec in _SPECS
    ]


class MockNewsProvider(NewsProvider):
    """降级实现：返回内置固定新闻，绝不触网。"""

    @override
    def search(self, query: str, days: int) -> list[NewsItem]:
        """返回内置新闻中发布时间在 ``days`` 天内的条目。

        本实现**忽略 ``query``**：它是兜底数据集，若按关键词过滤，绝大多数
        查询都会返回空，反而失去兜底意义。真实的关键词检索由 RssNewsProvider
        负责。
        """
        cutoff = datetime.now(UTC) - timedelta(days=days)
        return [item for item in build_mock_items() if item.published_at >= cutoff]

    @override
    def fetch_article(self, url: str) -> Article:
        """返回与内置条目匹配的正文；URL 不在内置数据中时返回占位正文。

        与 ``search`` 一致，本实现不会因为找不到 URL 而抛异常，以保证降级路径
        永远不会中断上层流程。
        """
        for item in build_mock_items():
            if item.url == url:
                return Article(
                    title=item.title,
                    url=item.url,
                    source=item.source,
                    published_at=item.published_at,
                    text=item.summary,
                )
        return Article(
            title=url,
            url=url,
            source=urlparse(url).netloc,
            published_at=datetime.now(UTC),
            text="（mock 降级数据：未内置该 URL 的正文。）",
        )
