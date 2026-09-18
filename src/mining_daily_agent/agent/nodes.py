"""LangGraph 节点：planner → fetch_data → analyze → synthesize → render。

每个取数调用都单独捕获异常并记入 ``risk_notes``，不阻塞整条流程
（见 CLAUDE.md「可靠性」）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Final, Protocol

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ValidationError

from mining_daily_agent.agent.llm import build_llm
from mining_daily_agent.agent.state import BriefState, FetchPlan
from mining_daily_agent.config import default_report_url, reports_dir
from mining_daily_agent.models.news import NewsItem
from mining_daily_agent.models.prices import TrendSeries
from mining_daily_agent.models.resources import ResourceCategory, ResourceReport

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from langchain_core.messages import BaseMessage

logger = logging.getLogger(__name__)


class AgentError(RuntimeError):
    """编排层错误。"""


class ToolCaller(Protocol):
    """节点需要的连接池能力。

    只声明 ``call_tool``：真实的 ``McpConnectionPool`` 与测试替身都满足它，
    节点因此不必依赖具体连接池类型。
    """

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: Mapping[str, object] | None = None,
    ) -> CallToolResult:
        """路由一次工具调用。"""
        raise NotImplementedError


#: 当前连接池。由 run_daily_brief 注入、测试直接替换。
#:
#: 用模块级变量而不是把池塞进 state 或 config：节点要是纯函数（需求如此），
#: 而把活的连接对象放进 TypedDict 状态里会污染「状态即数据」的语义。
_pool: ToolCaller | None = None


def set_pool(pool: ToolCaller | None) -> None:
    """注入/清除连接池。"""
    global _pool
    _pool = pool


def get_pool() -> ToolCaller:
    """取当前连接池。

    Raises:
        AgentError: 尚未注入连接池。
    """
    if _pool is None:
        msg = "连接池尚未注入；请先调用 agent.nodes.set_pool()。"
        raise AgentError(msg)
    return _pool


#: topic 命中这些词就按锂矿处理（需求：Pilbara/锂矿类默认需要 PDF）。
LITHIUM_HINTS: Final[tuple[str, ...]] = ("lithium", "li2o", "pilbara", "spodumene", "锂")
#: 默认计划的回溯天数。
DEFAULT_PLAN_DAYS: Final = 3
#: 价格走势的回看交易日数。
TREND_DAYS: Final = 30
#: 新闻标题/摘要里出现即视为风险信号。
RISK_KEYWORDS: Final[tuple[str, ...]] = (
    "halt",
    "dispute",
    "decline",
    "suspend",
    "delay",
    "lawsuit",
    "investigation",
    "downgrade",
)
#: 判断一条新闻是否可能指向资源报告的线索词。
RESOURCE_HINTS: Final = ("resource", "reserve", "mineral", "feasibility", "report")
#: 来源里给降级数据加的后缀，提醒读者这不是真实数据。
DEGRADED_MARK: Final = "【降级示例数据】"
#: 文件名里保留的字符：Unicode 字母数字、下划线、连字符（中文因此得以保留）。
_SLUG_STRIP_RE: Final = re.compile(r"[^\w\-]+", re.UNICODE)
#: 从可能带代码围栏的文本里抓第一个 JSON 对象。
_JSON_OBJECT_RE: Final = re.compile(r"\{.*\}", re.DOTALL)

_PLANNER_PROMPT: Final = """你是矿业研究助手。请为下面的简报主题制定取数计划。

主题：{topic}

只输出一个 JSON 对象，不要任何解释或 Markdown 围栏，字段如下：
- "keywords": 传给新闻检索的关键词（英文效果最好）。字符串或字符串数组均可，
  多个相关词用数组给出覆盖面更好
- "days": 新闻回溯天数，1-30 的整数
- "commodity": 关注的大宗商品，只能是 lithium / nickel / copper / cobalt 之一
- "needs_pdf": 是否需要抽取资源报告 PDF（涉及矿山项目、储量、资源量时为 true）
- "rationale": 一句话说明理由
"""

_SYNTHESIZE_PROMPT: Final = """你是矿业分析师。请根据下方资料，写一份关于「{topic}」的中文每日简报。

硬性要求：
- 用 Markdown，按需包含「## 概览」「## 价格与走势」「## 资源量」「## 风险提示」小节
- **每条事实后面必须用 [编号] 标注来源**，编号对应「编号来源」清单
- 资料里没有的数字一律不要编造；缺失的小节直接省略，不要写占位符
- **标有「{degraded_mark}」的资料不是真实数据**：引用时必须写明它不可采信，
  绝不能当作真实报道或真实资源量陈述
- 正文控制在 400 字以内

## 编号来源

{sources}

## 资料

{data}
"""


# --- MCP 工具结果解码 -------------------------------------------------------


def _payload_from_result(result: CallToolResult) -> object:
    """取出工具返回的载荷。

    MCP 的结构化内容必须是对象，所以返回**列表**的工具会被 FastMCP 包一层
    ``{"result": [...]}``，而返回单个对象的工具直接给出对象本身——两种形态都要认。
    另外列表返回时 ``content`` 里每个元素各占一个文本块，只读第一块会丢数据，
    因此优先用 ``structured_content``。
    """
    structured = result.structured_content
    if structured is not None:
        if set(structured) == {"result"} and isinstance(structured["result"], list):
            return structured["result"]
        return structured
    for block in result.content:
        if isinstance(block, TextContent):
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return None
    return None


def _raw_items(payload: object) -> list[object]:
    """把载荷规整成列表；单个对象也包成单元素列表。"""
    if payload is None:
        return []
    if isinstance(payload, list):
        return list(payload)
    return [payload]


def _models_from_result[T: BaseModel](result: CallToolResult, model: type[T]) -> list[T]:
    """把工具结果解析成模型列表。

    Raises:
        AgentError: 工具报错或载荷无法解析。
    """
    if result.is_error:
        raise AgentError(_error_text(result))
    payload = _payload_from_result(result)
    if payload is None:
        msg = "工具返回了空载荷。"
        raise AgentError(msg)
    try:
        return [model.model_validate(item) for item in _raw_items(payload)]
    except ValidationError as exc:
        msg = f"工具返回的载荷无法解析成 {model.__name__}：{exc}"
        raise AgentError(msg) from exc


def _model_from_result[T: BaseModel](result: CallToolResult, model: type[T]) -> T:
    """把工具结果解析成单个模型。

    Raises:
        AgentError: 工具报错、载荷为空或无法解析。
    """
    items = _models_from_result(result, model)
    if not items:
        msg = f"工具没有返回 {model.__name__}。"
        raise AgentError(msg)
    return items[0]


def _error_text(result: CallToolResult) -> str:
    """拼接工具错误结果里的文本。"""
    parts = [block.text for block in result.content if isinstance(block, TextContent)]
    return "；".join(parts) or "工具执行失败（无详情）"


def _message_text(message: BaseMessage) -> str:
    """把消息内容压成纯文本（内容可能是多块结构）。"""
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        else:
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


# --- planner ----------------------------------------------------------------


def default_plan(topic: str) -> FetchPlan:
    """LLM 不可用或输出不可解析时的兜底计划。

    Pilbara / 锂矿类主题默认需要 PDF（需求指定）。
    """
    lowered = topic.casefold()
    is_lithium = any(hint in lowered for hint in LITHIUM_HINTS)
    return FetchPlan(
        keywords=topic.strip(),
        days=DEFAULT_PLAN_DAYS,
        commodity="lithium" if is_lithium else "copper",
        needs_pdf=is_lithium,
        rationale="默认计划（未采用 LLM 输出）",
    )


def _extract_json_object(text: str) -> dict[str, object] | None:
    """从可能带代码围栏的文本里取第一个 JSON 对象。"""
    match = _JSON_OBJECT_RE.search(text)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def parse_plan(text: str) -> FetchPlan | None:
    """把 LLM 输出解析成取数计划；不可解析时返回 None。"""
    payload = _extract_json_object(text)
    if payload is None:
        return None
    try:
        return FetchPlan.model_validate(payload)
    except ValidationError:
        return None


async def planner(state: BriefState) -> dict[str, object]:
    """调 LLM 产出取数计划；调用失败或输出不可解析时退回默认计划。"""
    topic = state["topic"]
    notes: list[str] = []
    plan: FetchPlan | None = None
    try:
        response = await build_llm().ainvoke(_PLANNER_PROMPT.format(topic=topic))
        plan = parse_plan(_message_text(response))
    except Exception as exc:  # 计划只是优化项，拿不到也要继续跑
        notes.append(f"LLM 计划生成失败，已使用默认计划：{type(exc).__name__}: {exc}")

    if plan is None:
        if not notes:
            notes.append("LLM 计划输出无法解析为 JSON，已使用默认计划。")
        plan = default_plan(topic)

    logger.info(
        "planner 完成：keywords=%r days=%d commodity=%s needs_pdf=%s",
        plan.keywords,
        plan.days,
        plan.commodity,
        plan.needs_pdf,
    )
    return {"plan": plan, "risk_notes": notes}


# --- fetch_data -------------------------------------------------------------


def _pick_report_url(news: list[NewsItem]) -> str | None:
    """挑一条最可能指向资源报告的新闻 URL；没有就退回可配置的默认年报 URL。

    分两轮：先找真正的 ``.pdf`` 链接，再退而求其次找标题/链接里带 resource、report
    等线索词的条目。两轮是必要的——矿业公司名里带 "Resources" 极其常见（如
    "Raiden Resources"），一轮混着判会把普通新闻页当成报告，实测踩过。
    """
    pdf_urls = [item.url for item in news if item.url.casefold().endswith(".pdf")]
    if pdf_urls:
        return pdf_urls[0]

    for item in news:
        haystack = f"{item.title} {item.url}".casefold()
        if any(hint in haystack for hint in RESOURCE_HINTS):
            return item.url
    return default_report_url() or None


async def fetch_data(state: BriefState) -> dict[str, object]:
    """并行取新闻与价格，再按需抽取资源报告；单个失败只记风险、不阻塞。"""
    pool = get_pool()
    plan = state["plan"]
    notes: list[str] = []

    news_result, price_result = await asyncio.gather(
        pool.call_tool("news", "search", {"query": plan.keywords, "days": plan.days}),
        pool.call_tool("price", "get_trend", {"commodity": plan.commodity, "days": TREND_DAYS}),
        return_exceptions=True,
    )

    news: list[NewsItem] = []
    if isinstance(news_result, BaseException):
        notes.append(f"新闻源失败，已降级：{type(news_result).__name__}: {news_result}")
    else:
        try:
            news = _models_from_result(news_result, NewsItem)
        except AgentError as exc:
            notes.append(f"新闻源失败，已降级：{exc}")
    if any(item.degraded for item in news):
        # mock 新闻带真实标题、来源与域名，不做这一步简报会把合成内容当报道引用。
        notes.append(
            "新闻为降级后的示例数据（真实 RSS 源不可用），标题与来源均系伪造，"
            "不得当作真实报道引用。"
        )

    trend: TrendSeries | None = None
    if isinstance(price_result, BaseException):
        notes.append(f"价格源失败，已降级：{type(price_result).__name__}: {price_result}")
    else:
        try:
            trend = _model_from_result(price_result, TrendSeries)
        except AgentError as exc:
            notes.append(f"价格源失败，已降级：{exc}")

    report: ResourceReport | None = None
    if plan.needs_pdf:
        report_url = _pick_report_url(news)
        if report_url is None:
            notes.append(
                "未找到资源报告 PDF，储量数据缺失；可设置 DEFAULT_REPORT_URL 指定兜底年报。"
            )
        else:
            try:
                result = await pool.call_tool("pdf", "extract_resources", {"pdf_url": report_url})
                report = _model_from_result(result, ResourceReport)
            except Exception as exc:  # 报告缺失不应中断简报，任何异常都只记风险
                notes.append(f"资源报告解析失败，已降级：{type(exc).__name__}: {exc}")

    if report is not None and report.degraded:
        # 合成吨位如果不加披露，简报会把它们当真实资源量呈现——那比报错更糟。
        notes.append("资源量为降级后的合成数据（PDF 未能真实解析），数值不可用于任何判断。")

    article = news[0] if news else None
    logger.info(
        "fetch_data 完成：news=%d trend=%s report=%s risks=%d",
        len(news),
        trend is not None,
        report is not None,
        len(notes),
    )
    return {
        "news": news,
        "article": article,
        "price_trend": trend,
        "resource_report": report,
        "risk_notes": notes,
    }


# --- analyze ----------------------------------------------------------------


def _price_highlight(trend: TrendSeries) -> str:
    """把走势序列压成一行可引用的统计。"""
    parts = [f"{trend.commodity} 区间涨跌 {trend.change_pct:+.2f}%"]
    parts.append(f"区间 {trend.min:.4g}–{trend.max:.4g}")
    if trend.ma7 is not None:
        parts.append(f"ma7={trend.ma7:.4g}")
    if trend.ma30 is not None:
        parts.append(f"ma30={trend.ma30:.4g}")
    return "；".join(parts) + f"（{len(trend.points)} 个交易日）"


def _resource_highlights(report: ResourceReport) -> list[str]:
    """按类别汇总吨位。"""
    totals: dict[ResourceCategory, float] = {}
    for item in report.resources:
        totals[item.category] = totals.get(item.category, 0.0) + item.tonnage_t

    highlights: list[str] = []
    for category in (ResourceCategory.INDICATED, ResourceCategory.INFERRED):
        tonnes = totals.get(category)
        if tonnes:
            highlights.append(f"{report.project_name} {category.value} 合计 {tonnes / 1e6:.1f} Mt")
    return highlights


async def analyze(state: BriefState) -> dict[str, object]:
    """纯计算：价格统计、储量汇总、新闻标题里的风险词。"""
    highlights: list[str] = []
    notes: list[str] = []

    trend = state["price_trend"]
    if trend is not None:
        highlights.append(_price_highlight(trend))

    report = state["resource_report"]
    if report is not None:
        highlights.extend(_resource_highlights(report))

    for item in state["news"]:
        lowered = f"{item.title} {item.summary}".casefold()
        hits = [keyword for keyword in RISK_KEYWORDS if keyword in lowered]
        if hits:
            notes.append(f"风险信号（{'、'.join(hits)}）：{item.title}")

    return {"highlights": highlights, "risk_notes": notes}


# --- synthesize -------------------------------------------------------------


def build_citations(state: BriefState) -> list[str]:
    """按固定顺序给出编号来源。

    synthesize 需要编号才能要求 LLM 标注 [n]，render 需要同一份编号出「来源」小节；
    两处都调用本函数，保证编号一致（它是纯函数，结果只取决于 state）。
    """
    citations = [
        f"{item.title} — {item.url}（{item.source}{DEGRADED_MARK if item.degraded else ''}）"
        for item in state["news"]
    ]
    report = state["resource_report"]
    if report is not None:
        mark = DEGRADED_MARK if report.degraded else ""
        citations.append(f"{report.project_name} 资源量报告 — {report.source_url}{mark}")
    trend = state["price_trend"]
    if trend is not None:
        citations.append(f"{trend.commodity} 价格数据 — {trend.source}")
    return citations


def _synthesis_data(state: BriefState) -> str:
    """把状态里的数据整理成给 LLM 的资料块。"""
    lines: list[str] = []

    if state["news"]:
        lines.append("### 新闻")
        lines.extend(
            f"- [{index}] {DEGRADED_MARK if item.degraded else ''}{item.title}：{item.summary}"
            for index, item in enumerate(state["news"], 1)
        )

    if state["highlights"]:
        lines.append("### 计算结果")
        lines.extend(f"- {item}" for item in state["highlights"])

    if state["resource_report"] is not None:
        lines.append("### 资源量明细")
        lines.extend(
            f"- {item.category.value} {item.commodity}：{item.tonnage_t / 1e6:.1f} Mt"
            + (f" @ {item.grade}{item.grade_unit}" if item.grade is not None else "")
            for item in state["resource_report"].resources
        )

    if state["risk_notes"]:
        lines.append("### 风险提示")
        lines.extend(f"- {item}" for item in state["risk_notes"])

    return "\n".join(lines) if lines else "（本次没有取到任何数据）"


async def synthesize(state: BriefState) -> dict[str, object]:
    """调 LLM 把各方资料合成 Markdown 简报正文。"""
    citations = build_citations(state)
    sources = "\n".join(f"{index}. {text}" for index, text in enumerate(citations, 1))
    prompt = _SYNTHESIZE_PROMPT.format(
        topic=state["topic"],
        sources=sources or "（无来源）",
        data=_synthesis_data(state),
        degraded_mark=DEGRADED_MARK,
    )
    response = await build_llm().ainvoke(prompt)
    return {"markdown": _message_text(response).strip()}


# --- render -----------------------------------------------------------------


def slugify(topic: str) -> str:
    """把主题转成可用作文件名的 slug；保留 Unicode 字母（因此中文会保留）。"""
    slug = _SLUG_STRIP_RE.sub("-", topic.strip()).strip("-")
    return (slug or "brief")[:80]


def report_path(topic: str, today: date | None = None) -> Path:
    """简报的落盘路径：``<REPORTS_DIR>/YYYY-MM-DD-<slug>.md``。"""
    day = today if today is not None else datetime.now(UTC).date()
    return reports_dir() / f"{day.isoformat()}-{slugify(topic)}.md"


async def render(state: BriefState) -> dict[str, object]:
    """拼上「来源」小节、写入文件，返回最终 Markdown。"""
    citations = build_citations(state)
    body = state["markdown"].rstrip()
    if citations:
        sources = "\n".join(f"{index}. {text}" for index, text in enumerate(citations, 1))
        document = f"{body}\n\n## 来源\n\n{sources}\n"
    else:
        document = f"{body}\n\n## 来源\n\n（本次没有取到可引用的来源）\n"

    path = report_path(state["topic"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")
    logger.info("简报已写入：path=%s chars=%d", path, len(document))
    return {"markdown": document, "citations": citations}
