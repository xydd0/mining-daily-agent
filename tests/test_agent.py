"""Agent 编排的行为测试。

LLM 与连接池全部替换成假实现，不触网、不起子进程；简报输出目录指向临时目录，
避免跑一次测试就往仓库里写文件（见 CLAUDE.md「测试」）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from mcp.types import CallToolResult, ContentBlock, TextContent
from pydantic import SecretStr

from mining_daily_agent import __main__ as cli_module
from mining_daily_agent.agent import graph as graph_module
from mining_daily_agent.agent import nodes as nodes_module
from mining_daily_agent.agent.graph import (
    PoolHandle,
    initial_state,
    run_daily_brief,
)
from mining_daily_agent.agent.llm import (
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT_SECONDS,
    build_chat_openai,
)
from mining_daily_agent.agent.nodes import (
    DEGRADED_MARK,
    ToolCaller,
    analyze,
    build_citations,
    default_plan,
    fetch_data,
    parse_plan,
    planner,
    report_path,
    slugify,
)
from mining_daily_agent.agent.state import FetchPlan
from mining_daily_agent.config import (
    DEFAULT_REPORT_URL_BUILTIN,
    Config,
    default_report_url,
    reports_dir,
)
from mining_daily_agent.models.news import NewsItem
from mining_daily_agent.models.prices import PricePoint
from mining_daily_agent.models.resources import ResourceCategory, ResourceItem, ResourceReport
from mining_daily_agent.providers.prices.base import build_trend_series

TOPIC = "Pilbara 锂矿"
NEWS_URL = "https://example.com/news/0.pdf"
REPORT_URL = "https://example.com/report.pdf"

PLAN_REPLY = json.dumps(
    {
        "keywords": "pilbara lithium",
        "days": 3,
        "commodity": "lithium",
        "needs_pdf": True,
        "rationale": "锂矿项目主题",
    },
    ensure_ascii=False,
)
SUMMARY_REPLY = "## 概览\n\nPilbara 锂矿产量上升 [1]。\n\n## 价格与走势\n\n区间上涨 [3]。\n"


# --- 假实现 -----------------------------------------------------------------


class _FakeLLM:
    """满足 nodes.BriefLLM 的假 LLM：按顺序吐出预置回复。"""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    async def ainvoke(self, input: str) -> BaseMessage:  # noqa: A002 — 与 SDK 同名
        self.prompts.append(input)
        text = self._replies.pop(0) if self._replies else ""
        return AIMessage(content=text)


class _FakePool:
    """满足 graph.PoolHandle 的假连接池。

    ``responses`` 的值可以是 ``CallToolResult``（正常返回）或 ``BaseException``
    （模拟该数据源失败）。
    """

    def __init__(self, responses: Mapping[tuple[str, str], object]) -> None:
        self._responses = dict(responses)
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.closed = False

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: Mapping[str, object] | None = None,
    ) -> CallToolResult:
        self.calls.append((server_name, tool_name, dict(arguments or {})))
        result = self._responses.get((server_name, tool_name))
        if isinstance(result, BaseException):
            raise result
        if result is None:
            msg = f"未预置 {server_name}.{tool_name} 的返回"
            raise AssertionError(msg)
        assert isinstance(result, CallToolResult)
        return result

    async def aclose(self) -> None:
        self.closed = True


def _ok(payload: object) -> CallToolResult:
    """构造成功结果，形态与 FastMCP 实际返回一致。

    列表会被包成 ``{"result": [...]}``，且 ``content`` 里**每个元素各占一个文本块**
    ——只读第一个文本块会丢数据，这个形态是刻意保留的。
    """
    # content 的元素类型是 ContentBlock 联合（文本/图片/音频/链接/嵌入资源）。
    # 直接传 list[TextContent] 会因 list 不变而被 mypy 拒绝，故按联合类型标注。
    if isinstance(payload, list):
        blocks: list[ContentBlock] = [
            TextContent(type="text", text=json.dumps(item, ensure_ascii=False)) for item in payload
        ]
        return CallToolResult(content=blocks, structured_content={"result": payload})
    single: list[ContentBlock] = [
        TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))
    ]
    structured = payload if isinstance(payload, dict) else {}
    return CallToolResult(content=single, structured_content=structured)


def _error(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=message)], is_error=True)


def _news_payload() -> list[dict[str, object]]:
    published = datetime(2026, 9, 18, tzinfo=UTC)
    return [
        NewsItem(
            title=f"Pilbara lithium output rises {index}",
            url=f"https://example.com/news/{index}.pdf",
            source="Example News",
            published_at=published,
            summary="Output rose.",
        ).model_dump(mode="json")
        for index in range(2)
    ]


def _trend_payload() -> dict[str, object]:
    points = [
        PricePoint(
            commodity="lithium",
            date=date(2026, 1, 1) + timedelta(days=index),
            price=100.0 + index,
            unit="USD/t",
            source="test-source",
        )
        for index in range(35)
    ]
    return build_trend_series("lithium", points, "test-source").model_dump(mode="json")


def _report_payload() -> dict[str, object]:
    return ResourceReport(
        project_name="Pilgangoora",
        source_url=REPORT_URL,
        fetched_at=datetime(2026, 9, 18, tzinfo=UTC),
        resources=[
            ResourceItem(
                category=ResourceCategory.INDICATED,
                commodity="Li2O",
                tonnage_t=214e6,
                grade=1.15,
                grade_unit="%",
            ),
            ResourceItem(
                category=ResourceCategory.INDICATED,
                commodity="Li2O",
                tonnage_t=86e6,
                grade=1.08,
                grade_unit="%",
            ),
            ResourceItem(
                category=ResourceCategory.INFERRED,
                commodity="Li2O",
                tonnage_t=89e6,
                grade=1.05,
                grade_unit="%",
            ),
        ],
    ).model_dump(mode="json")


def _plain_news_item(**overrides: object) -> dict[str, object]:
    """一条不含 ``.pdf`` 链接、也不带 report/resource 线索词的普通新闻。"""
    defaults: dict[str, object] = {
        "title": "Some unrelated headline",
        "url": "https://example.com/plain-page",
        "source": "Example",
        "published_at": datetime(2026, 9, 18, tzinfo=UTC),
        "summary": "Nothing about resources.",
    }
    return NewsItem.model_validate({**defaults, **overrides}).model_dump(mode="json")


def _responses() -> dict[tuple[str, str], object]:
    return {
        ("news", "search"): _ok(_news_payload()),
        ("price", "get_trend"): _ok(_trend_payload()),
        ("pdf", "extract_resources"): _ok(_report_payload()),
    }


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """简报写到临时目录、清掉兜底 URL、每个用例结束后清空注入的连接池。"""
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path))
    monkeypatch.delenv("DEFAULT_REPORT_URL", raising=False)
    yield tmp_path
    nodes_module.set_pool(None)


def _install_llm(monkeypatch: pytest.MonkeyPatch, *replies: str) -> _FakeLLM:
    llm = _FakeLLM(list(replies))
    monkeypatch.setattr(nodes_module, "build_llm", lambda: llm)
    return llm


# --- 场景 1：全链路 ---------------------------------------------------------


async def test_full_pipeline_produces_markdown_with_sources_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_llm(monkeypatch, PLAN_REPLY, SUMMARY_REPLY)
    pool = _FakePool(_responses())

    document = await run_daily_brief(TOPIC, pool=pool)

    assert "## 概览" in document
    assert "## 来源" in document
    assert "Pilbara lithium output rises 0" in document, "新闻应进来源小节"
    assert NEWS_URL in document
    assert REPORT_URL in document, "资源报告的 source_url 应进来源"
    assert "test-source" in document, "价格来源应进来源"


async def test_full_pipeline_calls_every_expected_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_llm(monkeypatch, PLAN_REPLY, SUMMARY_REPLY)
    pool = _FakePool(_responses())

    await run_daily_brief(TOPIC, pool=pool)

    called = {(server, tool) for server, tool, _ in pool.calls}
    assert called == {
        ("news", "search"),
        ("price", "get_trend"),
        ("pdf", "extract_resources"),
    }
    plan_call = next(call for call in pool.calls if call[:2] == ("news", "search"))
    assert plan_call[2] == {"query": "pilbara lithium", "days": 3}


async def test_brief_is_written_to_the_reports_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_llm(monkeypatch, PLAN_REPLY, SUMMARY_REPLY)
    pool = _FakePool(_responses())

    document = await run_daily_brief(TOPIC, pool=pool)

    written = list(tmp_path.glob("*.md"))
    assert len(written) == 1
    assert written[0].read_text(encoding="utf-8") == document
    assert written[0].name.startswith("20"), "文件名应以日期开头"


# --- 场景 2：单个数据源失败不阻塞 -------------------------------------------


async def test_news_failure_does_not_block_the_brief(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _responses()
    responses[("news", "search")] = RuntimeError("news server down")
    _install_llm(monkeypatch, PLAN_REPLY, SUMMARY_REPLY)

    document = await run_daily_brief(TOPIC, pool=_FakePool(responses))

    assert "## 来源" in document, "新闻挂了仍要产出简报"
    assert "价格数据" in document, "其余数据源应照常进来源"


async def test_tool_level_error_result_is_recorded_as_a_risk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _responses()
    responses[("price", "get_trend")] = _error("价格工具执行失败")
    _install_llm(monkeypatch, PLAN_REPLY, SUMMARY_REPLY)

    document = await run_daily_brief(TOPIC, pool=_FakePool(responses))

    assert "## 来源" in document
    assert "test-source" not in document, "失败的价格源不应出现在来源里"


async def test_pdf_failure_does_not_block_the_brief(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _responses()
    responses[("pdf", "extract_resources")] = RuntimeError("pdf server down")
    _install_llm(monkeypatch, PLAN_REPLY, SUMMARY_REPLY)

    document = await run_daily_brief(TOPIC, pool=_FakePool(responses))

    assert "## 来源" in document
    assert REPORT_URL not in document


async def test_synthesised_resource_data_is_disclosed_as_a_risk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PDF 降级成合成数据时必须留下提示。

    否则简报会拿合成吨位当真实资源量呈现——那比直接报错更糟。
    """
    payload = _report_payload()
    payload["degraded"] = True
    responses = _responses()
    responses[("pdf", "extract_resources")] = _ok(payload)
    llm = _install_llm(monkeypatch, PLAN_REPLY, SUMMARY_REPLY)

    await run_daily_brief(TOPIC, pool=_FakePool(responses))

    assert "不可用于任何判断" in llm.prompts[-1], "降级声明必须进入合成提示词"


async def test_degraded_news_is_disclosed_in_the_prompt_and_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mock 新闻带真实标题、来源与域名，不标注就会被当真实报道引用。"""
    degraded = _news_payload()
    for item in degraded:
        item["degraded"] = True
    responses = _responses()
    responses[("news", "search")] = _ok(degraded)
    llm = _install_llm(monkeypatch, PLAN_REPLY, SUMMARY_REPLY)

    document = await run_daily_brief(TOPIC, pool=_FakePool(responses))

    prompt = llm.prompts[-1]
    assert "不得当作真实报道引用" in prompt, "降级新闻必须留下风险提示"
    assert DEGRADED_MARK in prompt, "资料块里应逐条标出降级条目"
    assert DEGRADED_MARK in document, "来源小节应标出降级条目"


async def test_real_data_is_not_marked_as_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实数据不能被打上降级标记，否则读者会以为整篇都不可信。"""
    _install_llm(monkeypatch, PLAN_REPLY, SUMMARY_REPLY)

    document = await run_daily_brief(TOPIC, pool=_FakePool(_responses()))

    assert DEGRADED_MARK not in document


async def test_default_annual_report_beats_hint_matched_news(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有确定性年报时，绝不能退回线索词命中的新闻页。

    Google News 返回的条目几乎不是 .pdf，线索词命中的多是普通新闻网页（矿企名里带
    "Resources" 极常见）。把它喂给 PDF 解析器只会解析失败再降级成 mock——挑到哪条
    全看运气，每次结果都可能不同。这是本项目修过的一个真实缺陷。
    """
    responses = _responses()
    responses[("news", "search")] = _ok(
        [
            NewsItem(
                title="Raiden Resources eyes lithium exploration",
                url="https://example.com/news-page",
                source="Example",
                published_at=datetime(2026, 9, 18, tzinfo=UTC),
                summary="Exploration update.",
            ).model_dump(mode="json")
        ]
    )
    _install_llm(monkeypatch, PLAN_REPLY, SUMMARY_REPLY)
    pool = _FakePool(responses)

    await run_daily_brief(TOPIC, pool=pool)

    pdf_call = next(call for call in pool.calls if call[:2] == ("pdf", "extract_resources"))
    assert pdf_call[2] == {"pdf_url": DEFAULT_REPORT_URL_BUILTIN}


async def test_configured_report_url_beats_the_builtin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEFAULT_REPORT_URL", "https://example.com/custom-annual.pdf")
    responses = _responses()
    # 新闻里不能有 .pdf 链接，否则第一级就命中了，到不了年报这一级。
    responses[("news", "search")] = _ok([_plain_news_item()])
    _install_llm(monkeypatch, PLAN_REPLY, SUMMARY_REPLY)
    pool = _FakePool(responses)

    await run_daily_brief(TOPIC, pool=pool)

    pdf_call = next(call for call in pool.calls if call[:2] == ("pdf", "extract_resources"))
    assert pdf_call[2] == {"pdf_url": "https://example.com/custom-annual.pdf"}


async def test_hint_matched_news_is_the_last_resort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保底分支：只有在没有确定性年报时才回落到线索词命中的新闻。

    内置默认值存在时这条分支不可达，所以这里显式把 default_report_url 打成空串来覆盖它
    ——保留这段逻辑，是为了让「确定性优先」这条原则写死在代码里而不是靠默认值巧合成立。
    """
    monkeypatch.setattr(nodes_module, "default_report_url", lambda: "")
    responses = _responses()
    responses[("news", "search")] = _ok(
        [
            NewsItem(
                title="Some company annual resource report",
                url="https://example.com/resource-summary",
                source="Example",
                published_at=datetime(2026, 9, 18, tzinfo=UTC),
                summary="Resource statement.",
            ).model_dump(mode="json")
        ]
    )
    _install_llm(monkeypatch, PLAN_REPLY, SUMMARY_REPLY)
    pool = _FakePool(responses)

    await run_daily_brief(TOPIC, pool=pool)

    pdf_call = next(call for call in pool.calls if call[:2] == ("pdf", "extract_resources"))
    assert pdf_call[2] == {"pdf_url": "https://example.com/resource-summary"}


# --- 场景 3：LLM 计划解析失败回退默认计划 ------------------------------------


async def test_unparsable_plan_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_llm(monkeypatch, "抱歉，我无法只输出 JSON。", SUMMARY_REPLY)
    pool = _FakePool(_responses())

    await run_daily_brief(TOPIC, pool=pool)

    plan_call = next(call for call in pool.calls if call[:2] == ("news", "search"))
    assert plan_call[2]["query"] == TOPIC, "默认计划以整条主题作关键词"


async def test_planner_llm_failure_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM 整个挂掉时计划要回退，而不是把异常抛给上层的 fetch_data。"""

    class _BrokenLLM:
        async def ainvoke(self, input: str) -> BaseMessage:  # noqa: A002 — 与 SDK 同名
            msg = "upstream 503"
            raise RuntimeError(msg)

    monkeypatch.setattr(nodes_module, "build_llm", _BrokenLLM)

    result = await planner(initial_state(TOPIC))

    plan = result["plan"]
    assert isinstance(plan, FetchPlan)
    assert plan.keywords == TOPIC, "默认计划以整条主题作关键词"
    notes = result["risk_notes"]
    assert isinstance(notes, list)
    assert any("LLM 计划生成失败" in note for note in notes)


def test_parse_plan_accepts_json_in_a_code_fence() -> None:
    text = f"这是计划：\n```json\n{PLAN_REPLY}\n```\n请查收。"

    plan = parse_plan(text)

    assert plan is not None
    assert plan.keywords == "pilbara lithium"
    assert plan.days == 3


def test_parse_plan_rejects_garbage() -> None:
    assert parse_plan("完全没有 JSON") is None
    assert parse_plan('{"days": "not-a-number"}') is None


def test_parse_plan_accepts_a_keyword_list() -> None:
    """实测 LLM 会返回关键词数组（如 ["Pilbara lithium mine", "Pilgangoora"]）。

    若直接判为非法，计划会白白回退成默认值——首版就是这么在真实调用里失手的。
    """
    text = json.dumps(
        {
            "keywords": ["Pilbara lithium mine", "Pilgangoora", "  "],
            "days": 3,
            "commodity": "lithium",
            "needs_pdf": True,
        }
    )

    plan = parse_plan(text)

    assert plan is not None
    assert plan.keywords == "Pilbara lithium mine OR Pilgangoora"


def test_default_plan_marks_lithium_topics_as_needing_pdf() -> None:
    lithium = default_plan("给我一份 Pilbara 锂矿简报")
    other = default_plan("铜价走势")

    assert lithium.commodity == "lithium"
    assert lithium.needs_pdf is True
    assert other.commodity == "copper"
    assert other.needs_pdf is False


# --- 工具结果解码 -----------------------------------------------------------


async def test_list_payload_is_unwrapped_without_losing_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """列表返回被 FastMCP 包成 {"result": [...]}；只读 content 会只拿到第一条。"""
    _install_llm(monkeypatch, PLAN_REPLY)
    nodes_module.set_pool(_FakePool(_responses()))

    result = await fetch_data(initial_state(TOPIC))

    news = result["news"]
    assert isinstance(news, list)
    assert len(news) == 2, "两条新闻都应被解析出来"


async def test_pdf_link_is_preferred_over_hint_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """矿业公司名里常带 "Resources"，只按线索词匹配会把新闻页当成报告。"""
    responses = _responses()
    responses[("news", "search")] = _ok(
        [
            NewsItem(
                title="Raiden Resources eyes lithium exploration",
                url="https://example.com/news-page",  # 名字里有 Resources，但不是 PDF
                source="Example",
                published_at=datetime(2026, 9, 18, tzinfo=UTC),
                summary="s",
            ).model_dump(mode="json"),
            NewsItem(
                title="Annual report",
                url="https://example.com/annual.PDF",  # 真 PDF，大小写不敏感
                source="Example",
                published_at=datetime(2026, 9, 18, tzinfo=UTC),
                summary="s",
            ).model_dump(mode="json"),
        ]
    )
    _install_llm(monkeypatch, PLAN_REPLY)
    pool = _FakePool(responses)

    nodes_module.set_pool(pool)
    await fetch_data(initial_state(TOPIC))

    pdf_call = next(call for call in pool.calls if call[:2] == ("pdf", "extract_resources"))
    assert pdf_call[2] == {"pdf_url": "https://example.com/annual.PDF"}


async def test_fetch_data_records_risk_notes_for_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 新闻保持正常，否则挑不出 PDF 地址、走的是「未找到报告」那条分支
    responses = _responses()
    responses[("price", "get_trend")] = RuntimeError("boom")
    responses[("pdf", "extract_resources")] = _error("pdf failed")
    _install_llm(monkeypatch, PLAN_REPLY)
    nodes_module.set_pool(_FakePool(responses))

    result = await fetch_data(initial_state(TOPIC))

    notes = result["risk_notes"]
    assert isinstance(notes, list)
    assert any("价格源失败" in note for note in notes)
    assert any("资源报告解析失败" in note for note in notes)


# --- analyze ----------------------------------------------------------------


async def test_analyze_summarises_price_and_tonnage() -> None:
    state = initial_state(TOPIC)
    state["price_trend"] = build_trend_series(
        "lithium",
        [
            PricePoint(
                commodity="lithium",
                date=date(2026, 1, 1) + timedelta(days=index),
                price=100.0 + index,
                unit="USD/t",
                source="t",
            )
            for index in range(35)
        ],
        "t",
    )
    state["resource_report"] = ResourceReport.model_validate(_report_payload())

    result = await analyze(state)

    highlights = result["highlights"]
    assert isinstance(highlights, list)
    assert any("区间涨跌" in item for item in highlights)
    assert any("Indicated 合计 300.0 Mt" in item for item in highlights)
    assert any("Inferred 合计 89.0 Mt" in item for item in highlights)


async def test_analyze_replaces_a_double_counted_sum_with_the_self_reported_total() -> None:
    """JORC 表同时列分块小计与全矿总计，逐行相加会把同一份资源量算两遍。

    实测一份真实年报把 356 Mt 的 Indicated 加成 760 Mt。报告自报合计更小时以自报为准，
    并且必须留下提示——否则简报会把重复计算的数字当事实呈现。
    """
    state = initial_state(TOPIC)
    state["resource_report"] = ResourceReport(
        project_name="Pilgangoora",
        source_url=REPORT_URL,
        fetched_at=datetime(2026, 9, 18, tzinfo=UTC),
        resources=[
            ResourceItem(
                category=ResourceCategory.INDICATED,
                commodity="Li2O",
                tonnage_t=349e6,
                grade=1.29,
                grade_unit="%",
            ),
            ResourceItem(
                category=ResourceCategory.INDICATED,
                commodity="Li2O",
                tonnage_t=356e6,
                grade=1.29,
                grade_unit="%",
            ),
        ],
        # 逐条求和 705 Mt，自报合计 445 Mt → 明显重复
        self_reported_total_t=445e6,
    )

    result = await analyze(state)

    highlights = result["highlights"]
    assert isinstance(highlights, list)
    assert any("445.0 Mt" in item for item in highlights), "应以自报合计为准"
    assert not any("705" in item for item in highlights), "不应再把重复求和高亮出去"
    notes = result["risk_notes"]
    assert isinstance(notes, list)
    assert any("重复计入" in note for note in notes)


async def test_analyze_keeps_per_category_totals_when_they_agree() -> None:
    """自报合计与求和一致时不该误报重复——正常报告仍要给出分类别数字。"""
    payload = _report_payload()
    payload["self_reported_total_t"] = 389e6  # 与逐条求和（214+86+89）一致
    state = initial_state(TOPIC)
    state["resource_report"] = ResourceReport.model_validate(payload)

    result = await analyze(state)

    highlights = result["highlights"]
    assert isinstance(highlights, list)
    assert any("Indicated 合计 300.0 Mt" in item for item in highlights)
    assert result["risk_notes"] == []


async def test_analyze_tolerates_a_difference_within_five_percent() -> None:
    """±5% 是容差分界线：容差内的偏差不该被当成重复计入。

    JORC 表逐行四舍五入到 0.1 Mt，求和与自报合计差几个百分点是常态。为此把整张表
    判为不可信、只报一个总数，反而丢掉了分类别信息。
    """
    payload = _report_payload()
    payload["self_reported_total_t"] = 400e6  # 求和 389 Mt，偏差 -2.8%
    state = initial_state(TOPIC)
    state["resource_report"] = ResourceReport.model_validate(payload)

    result = await analyze(state)

    highlights = result["highlights"]
    assert isinstance(highlights, list)
    assert any("Indicated 合计 300.0 Mt" in item for item in highlights), "容差内仍给分类别数字"
    assert result["risk_notes"] == []


async def test_analyze_falls_back_to_the_reported_total_beyond_the_tolerance() -> None:
    """一旦越过 ±5%，求和值不再可信，改报自报合计并说明原因。"""
    payload = _report_payload()
    payload["self_reported_total_t"] = 360e6  # 求和 389 Mt，偏差 +8.1%
    state = initial_state(TOPIC)
    state["resource_report"] = ResourceReport.model_validate(payload)

    result = await analyze(state)

    highlights = result["highlights"]
    assert isinstance(highlights, list)
    assert any("自报资源量合计 360.0 Mt" in item for item in highlights)
    assert not any("389" in item for item in highlights), "可疑的求和值不该被高亮出去"
    notes = result["risk_notes"]
    assert isinstance(notes, list)
    assert any("重复计入" in note and "+8.1%" in note for note in notes), (
        f"提示里要写明偏差与容差，实得 {notes}"
    )


async def test_analyze_flags_risk_keywords_in_headlines() -> None:
    state = initial_state(TOPIC)
    state["news"] = [
        NewsItem(
            title="Pilbara mine halt extended amid dispute",
            url="https://example.com/a",
            source="Example",
            published_at=datetime(2026, 9, 18, tzinfo=UTC),
            summary="Operations suspended.",
        )
    ]

    result = await analyze(state)

    notes = result["risk_notes"]
    assert isinstance(notes, list)
    assert any("halt" in note for note in notes)
    assert any("dispute" in note for note in notes)


async def test_analyze_is_quiet_without_data() -> None:
    result = await analyze(initial_state(TOPIC))

    assert result == {"highlights": [], "risk_notes": []}


# --- render / 路径 ----------------------------------------------------------


def test_slugify_keeps_cjk_and_strips_punctuation() -> None:
    assert slugify("Pilbara 锂矿，今日简报！") == "Pilbara-锂矿-今日简报"


def test_slugify_falls_back_for_empty_topic() -> None:
    assert slugify("   ") == "brief"


def test_report_path_uses_configured_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path))

    path = report_path("Pilbara 锂矿", today=date(2026, 9, 18))

    assert path.parent == tmp_path
    assert path.name == "2026-09-18-Pilbara-锂矿.md"


def test_reports_dir_defaults_to_reports_relative_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REPORTS_DIR", raising=False)

    assert reports_dir() == Path("reports")


def test_default_report_url_falls_back_to_the_builtin_annual_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未配置时用内置年报——有确定性来源，才不至于每次挑到哪条新闻全看运气。"""
    monkeypatch.delenv("DEFAULT_REPORT_URL", raising=False)
    assert default_report_url() == DEFAULT_REPORT_URL_BUILTIN

    monkeypatch.setenv("DEFAULT_REPORT_URL", " https://example.com/annual.pdf ")
    assert default_report_url() == "https://example.com/annual.pdf"


def test_build_citations_lists_every_source() -> None:
    state = initial_state(TOPIC)
    state["news"] = [
        NewsItem(
            title="标题",
            url="https://example.com/a",
            source="Example",
            published_at=datetime(2026, 9, 18, tzinfo=UTC),
            summary="s",
        )
    ]
    state["resource_report"] = ResourceReport.model_validate(_report_payload())
    state["price_trend"] = build_trend_series(
        "lithium",
        [
            PricePoint(
                commodity="lithium",
                date=date(2026, 1, 1),
                price=1.0,
                unit="USD/t",
                source="price-src",
            )
        ],
        "price-src",
    )

    citations = build_citations(state)

    assert len(citations) == 3
    assert "https://example.com/a" in citations[0]
    assert REPORT_URL in citations[1]
    assert "price-src" in citations[2]


# --- 连接池生命周期 ---------------------------------------------------------


def _factory(pool: PoolHandle) -> object:
    async def _create() -> PoolHandle:
        return pool

    return _create


async def test_run_daily_brief_closes_the_pool_it_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_llm(monkeypatch, PLAN_REPLY, SUMMARY_REPLY)
    pool = _FakePool(_responses())
    monkeypatch.setattr(graph_module, "_create_pool", _factory(pool))

    await run_daily_brief(TOPIC)

    assert pool.closed, "自建的连接池必须被关闭"


async def test_run_daily_brief_leaves_an_injected_pool_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_llm(monkeypatch, PLAN_REPLY, SUMMARY_REPLY)
    pool = _FakePool(_responses())

    await run_daily_brief(TOPIC, pool=pool)

    assert not pool.closed, "注入的池由调用方负责关闭"


# --- CLI --------------------------------------------------------------------


def test_cli_prints_the_brief(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """刻意写成同步用例：main() 内部调 asyncio.run()，测试本身若跑在事件循环里会冲突。"""
    captured: list[str] = []

    async def _fake(topic: str, pool: ToolCaller | None = None) -> str:
        captured.append(topic)
        return "# 简报正文"

    monkeypatch.setattr(cli_module, "run_daily_brief", _fake)

    code = cli_module.main(["Pilbara 锂矿"])

    assert code == 0
    assert captured == ["Pilbara 锂矿"]
    assert "# 简报正文" in capsys.readouterr().out


def test_cli_defaults_to_the_default_topic() -> None:
    args = cli_module.build_parser().parse_args([])

    assert args.topic == cli_module.DEFAULT_TOPIC


def test_cli_takes_a_positional_topic() -> None:
    args = cli_module.build_parser().parse_args(["铜价走势"])

    assert args.topic == "铜价走势"


# --- LLM 接线 ---------------------------------------------------------------


def test_build_chat_openai_wires_base_url_and_timeout() -> None:
    """别名（model / api_key / base_url / timeout）一旦失效，base_url 会被静默丢掉、
    请求打回 api.openai.com，因此这里断言真正落到字段上的取值。"""
    config = Config(
        llm_api_key="sk-test",
        llm_base_url="https://api.deepseek.com",
        llm_model="deepseek-chat",
        news_days_default=1,
    )

    client = build_chat_openai(config)

    assert client.model_name == "deepseek-chat"
    assert client.openai_api_base == "https://api.deepseek.com"
    assert client.temperature == DEFAULT_TEMPERATURE
    assert client.request_timeout == DEFAULT_TIMEOUT_SECONDS
    api_key = client.openai_api_key
    assert isinstance(api_key, SecretStr), "密钥应以 SecretStr 保存，不会随 repr 泄漏"
    assert api_key.get_secret_value() == "sk-test"
