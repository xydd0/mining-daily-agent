"""LangGraph 节点：planner → fetch_data → analyze → synthesize → render。

每个取数调用都单独捕获异常并记入 ``risk_notes``，不阻塞整条流程
（见 CLAUDE.md「可靠性」）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Final, Protocol

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ValidationError

from mining_daily_agent.agent.llm import build_llm
from mining_daily_agent.agent.state import BriefState, FetchPlan
from mining_daily_agent.config import default_report_url, reports_dir
from mining_daily_agent.models.news import Article, NewsItem
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
#: 默认计划的回溯天数（需求：缺省 7 天）。
DEFAULT_PLAN_DAYS: Final = 7
#: 取数计划的**最小**回溯天数。主题写的是「今日简报」，实测 LLM 会据此返回 days=1——
#: 一天的窗口配上 Google News 的时效性，整条新闻链搜回来 1 条、还被主体过滤掉，
#: 简报的新闻小节整个空掉。7 天是产品口径，不是优化项，所以在这里兜底而不是只写进提示词。
MIN_PLAN_DAYS: Final = DEFAULT_PLAN_DAYS
#: 价格走势的回看交易日数。
TREND_DAYS: Final = 30
#: 抓回来的正文在进简报前截断到多少字符（MCP 工具侧上限是 8000）。
ARTICLE_EXCERPT_CHARS: Final = 4000
#: 「要点式小标题」的字数上限。
NEWS_HEADLINE_MAX_CHARS: Final = 25
#: 默认计划用的**精确主体**：整条中文主题丢给 Google News 搜不到东西（实测），
#: 必须落到英文实体名。键是主题里出现的线索词，值是检索用的主体。
SUBJECT_HINTS: Final[tuple[tuple[str, str], ...]] = (
    ("pilgangoora", "Pilbara Minerals"),
    ("pilbara", "Pilbara Minerals"),
    ("greenbushes", "Greenbushes lithium"),
    ("wodgina", "Wodgina lithium"),
    ("lithium", "lithium spodumene"),
)
#: 判相关性时要忽略的泛词。一条新闻光提 "lithium"/"market" 说明不了它讲的是
#: **主题主体**——实测 Google News 拿 "Pilbara" 检索回来的头几条是
#: "Raiden Resources…Lithium Market Conditions…"，与 Pilbara 无关。
GENERIC_SUBJECT_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "lithium",
        "nickel",
        "copper",
        "cobalt",
        "mining",
        "mine",
        "mines",
        "mineral",
        "minerals",
        "resource",
        "resources",
        "market",
        "markets",
        "price",
        "prices",
        "stock",
        "stocks",
        "share",
        "shares",
        "news",
        "report",
        "reports",
        "today",
        "daily",
        "brief",
        "and",
        "the",
        "for",
        "with",
    }
)
#: 请求式主题里的措辞，压「主体」时去掉。
_TOPIC_FILLER: Final[tuple[str, ...]] = (
    "给我",
    "帮我",
    "麻烦",
    "请",
    "生成",
    "写",
    "做",
    "来",
    "一份",
    "一个",
    "关于",
    "有关",
    "今日",
    "今天",
    "的",
    "简报",
    "日报",
    "报告",
)
_LATIN_WORD_RE: Final = re.compile(r"[A-Za-z][A-Za-z.\-]*")
_WORD_RE: Final = re.compile(r"[A-Za-z]{3,}")
_WHITESPACE_RE: Final = re.compile(r"\s+")
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
#: 合成行情的显式标注。价格小节与风险提示都要出现这句原话——降级链路的诚实性
#: 最终就体现在这里：读者必须一眼看出这不是市场行情。
PRICE_DEGRADED_MARK: Final = "合成数据，非真实行情"
#: 文件名里保留的字符：Unicode 字母数字、下划线、连字符（中文因此得以保留）。
_SLUG_STRIP_RE: Final = re.compile(r"[^\w\-]+", re.UNICODE)
#: 从可能带代码围栏的文本里抓第一个 JSON 对象。
_JSON_OBJECT_RE: Final = re.compile(r"\{.*\}", re.DOTALL)

_PLANNER_PROMPT: Final = """你是矿业研究助手。请为下面的简报主题制定取数计划。

主题：{topic}

只输出一个 JSON 对象，不要任何解释或 Markdown 围栏，字段如下：
- "keywords": 传给新闻检索的关键词（英文效果最好）。字符串或字符串数组均可，
  多个相关词用数组给出覆盖面更好
- "days": 新闻回溯天数，1-30 的整数。**默认 7**，只有主题明确要求更短的时间窗
  （如「只看今天」）才调小——窗口太短会搜不到东西
- "commodity": 关注的大宗商品，只能是 lithium / nickel / copper / cobalt 之一
- "needs_pdf": 是否需要抽取资源报告 PDF（涉及矿山项目、储量、资源量时为 true）
- "rationale": 一句话说明理由
"""

#: 输出模板。**写死**：小节名、编号来源、吨位措辞全部由代码决定，不交给 LLM 发挥。
BRIEF_TITLE: Final = "# 矿权日报 · {subject} · {date}"
SECTION_NEWS: Final = "## 一、新闻摘要"
SECTION_RESOURCE: Final = "## 二、储量数据"
SECTION_PRICE: Final = "## 三、价格走势"
SECTION_RISK: Final = "## 四、风险提示"
SECTION_CITATIONS: Final = "## 引用源"
#: 简报必须包含的小节——契约，缺一个都不算完整（见 `_missing_sections`）。
REQUIRED_SECTIONS: Final[tuple[str, ...]] = (
    SECTION_NEWS,
    SECTION_RESOURCE,
    SECTION_PRICE,
    SECTION_RISK,
)
#: 正文没抓到时，新闻小节末尾的统一注释。
#:
#: 各条导语逐条写「该报道正文未能抓取」既啰嗦又抢戏，收成一句放在小节末尾说明一次。
#: 风险提示里对应的那条保留——那是披露，与小节注释不是一回事。
NEWS_BODY_NOTE: Final = (
    "注：本期新闻源经 Google News 中转页，正文未能抓取，各条导语基于标题与摘要撰写。"
)

#: 重试时追加的更强硬指令。上一次的输出没能让简报成篇，这一次必须只吐合规 JSON。
_STRICT_RETRY_INSTRUCTION: Final = (
    "上一次的输出不符合要求。这次只输出那个 JSON 对象本身：不要解释、"
    "不要 Markdown 围栏、不要遗漏任何一条新闻，每条的 headline 与 lede 都不能为空。"
)

#: LLM 不可用时的风险提示。措辞要让读者知道：结构没丢，只是没有被语言模型润色过。
LLM_FALLBACK_NOTE: Final = (
    "LLM 合成失败，以下为数据直出（小节结构完整，新闻导语退回原标题与摘要）。"
)

#: LLM 在整条流水线里**只负责**新闻小节的小标题与导语——这两样确实需要理解正文。
#: 其余小节全部由代码按模板渲染：小节名、编号对应、「矿石量」这类措辞是硬性约束，
#: 靠提示词保证不了，靠代码可以。LLM 整个挂掉时，这一节退回「标题 + 摘要」的确定性写法。
_NEWS_PROMPT: Final = """你是矿业分析师。下面每条新闻都有自己的编号与资料。

请为**每一条**写要点式小标题与 2-3 句导语，只输出一个 JSON 对象：
{{"items": [{{"index": 1, "headline": "…", "lede": "…"}}]}}

硬性要求：
- **小标题必须是你自拟的完整概括短句**，≤ {headline_max} 字，把该条讲了什么概括出来
  （如「锂价走势牵动 ASX 电池材料股」）。**不要照抄或截取原标题**，句末**不要**用省略号
  ——「Pilbara Minerals 定于11月24…」这种剪断的半句话是明确禁止的
- 小标题不带编号
- 导语**只能**用该条自己那份资料，不得掺入任何其它条目的内容——把 A 条的正文配到
  B 条标题下是严重错误
- 时间、主体、数字都必须与该条资料一致；资料里没有的一律不写，不要推测
- **不要在导语里交代正文有没有抓到**：那由小节末尾的统一注释说明一次，逐条重复会抢戏
- 标有「{degraded_mark}」的资料不是真实报道，导语里必须写明这一点

## 新闻资料

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


# --- 主题主体与相关性 -------------------------------------------------------


def brief_subject(topic: str) -> str:
    """把请求式主题压成「主体」：``给我生成一份关于 Pilbara 锂矿的今日简报`` → ``Pilbara 锂矿``。

    标题与资源小节都要用到它。压不出来时原样返回，宁可标题啰嗦也不要留空。
    """
    text = topic
    for word in _TOPIC_FILLER:
        text = text.replace(word, " ")
    subject = _WHITESPACE_RE.sub(" ", text).strip(" 　·、,，。.：:；;")
    return subject or topic.strip()


def _subject_prefix(subject: str) -> str:
    """取主体里的首个拉丁词当前缀（``Pilbara 锂矿`` → ``Pilbara``）。

    资源小节读作 ``Pilbara Annual Report 2025（来源 [2]，…）``——年报解析出的
    项目名常常只是文件名（如 ``Annual Report 2025``），没有前缀就不知道是谁的。
    """
    match = _LATIN_WORD_RE.search(subject)
    return match.group(0) if match is not None else subject


def plan_keywords(topic: str) -> str:
    """默认计划的检索词：**精确主体**，不是整条主题。

    Google News 搜不了整句中文（实测：拿中文主题去搜，返回的全是无关条目，整条数据链
    就此降级）。命中不了线索词时退回主体本身——它至少是用户写的原话。
    """
    lowered = topic.casefold()
    for hint, keyword in SUBJECT_HINTS:
        if hint in lowered:
            return keyword
    return brief_subject(topic)


def subject_tokens(topic: str, plan: FetchPlan) -> set[str]:
    """判相关性用的**有区分度**主体词（小写）。

    取自计划关键词与主题，去掉 ``lithium`` / ``minerals`` / ``market`` 这类泛词：
    一条新闻光提 "lithium" 说明不了它讲的是主题主体。实测 Google News 拿 "Pilbara"
    检索，头几条里就有 "Raiden Resources…Lithium Market Conditions…"。
    """
    haystack = f"{plan.keywords} {topic}"
    return {
        token.casefold()
        for token in _WORD_RE.findall(haystack)
        if token.casefold() not in GENERIC_SUBJECT_TOKENS
    }


def filter_relevant_news(
    news: list[NewsItem], topic: str, plan: FetchPlan
) -> tuple[list[NewsItem], int]:
    """只保留**讲主题主体**的条目。

    Returns:
        ``(保留的条目, 被剔除的条数)``。主体词一个都挑不出来时不筛（无从判断，
        宁可全留也不要凭一个空集合把新闻清空）。
    """
    tokens = subject_tokens(topic, plan)
    if not tokens:
        return list(news), 0
    kept = [
        item
        for item in news
        if any(token in f"{item.title} {item.summary}".casefold() for token in tokens)
    ]
    return kept, len(news) - len(kept)


# --- planner ----------------------------------------------------------------


def default_plan(topic: str) -> FetchPlan:
    """LLM 不可用或输出不可解析时的兜底计划。

    Pilbara / 锂矿类主题默认需要 PDF（需求指定）。关键词用**精确主体**——见
    :func:`plan_keywords`。
    """
    lowered = topic.casefold()
    is_lithium = any(hint in lowered for hint in LITHIUM_HINTS)
    return FetchPlan(
        keywords=plan_keywords(topic),
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

    if plan.days < MIN_PLAN_DAYS:
        logger.info("回溯天数 %d 太短，按最小值 %d 处理", plan.days, MIN_PLAN_DAYS)
        plan = plan.model_copy(update={"days": MIN_PLAN_DAYS})

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
    """挑一个交给 ``pdf.extract_resources`` 的地址，按确定性从高到低尝试。

    优先级：

    1. 新闻里**真正以 ``.pdf`` 结尾**的链接；
    2. ``config.default_report_url()``——可配置的**确定性**年报地址；
    3. 标题/链接里带 resource、report 等线索词的新闻条目。

    2 排在 3 前面是这条链路的要害：**Google News 返回的条目几乎不是 .pdf**，而线索词
    命中的绝大多数是普通新闻网页（矿业公司名里带 "Resources" 极常见，如
    "Raiden Resources"）。把新闻页喂给 PDF 解析器只会解析失败，再降级成 mock——
    也就是说挑到哪条全看运气，每跑一次结果都可能不同。回溯到确定的年报就没有这个问题。

    ⚠️ 3 是**保底分支，当前实际上不可达**：``default_report_url()`` 有内置默认值，
    不会返回空串，所以走到 ``for`` 循环前必定已经 return。保留它是因为「确定性优先」
    这条原则要写死在代码里——万一将来内置默认值被去掉或可被显式关闭，它仍然接得住，
    而不至于变成「挑到哪条看运气」。
    """
    pdf_urls = [item.url for item in news if item.url.casefold().endswith(".pdf")]
    if pdf_urls:
        return pdf_urls[0]

    configured = default_report_url()
    if configured:
        return configured

    for item in news:
        haystack = f"{item.title} {item.url}".casefold()
        if any(hint in haystack for hint in RESOURCE_HINTS):
            return item.url
    return None


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

    fetched = len(news)
    news, dropped = filter_relevant_news(news, state["topic"], plan)
    if dropped:
        notes.append(
            f"检索到 {fetched} 条新闻，其中 {dropped} 条与主题主体"
            f"（{brief_subject(state['topic'])}）无关，已剔除。"
        )

    trend: TrendSeries | None = None
    if isinstance(price_result, BaseException):
        notes.append(f"价格源失败，已降级：{type(price_result).__name__}: {price_result}")
    else:
        try:
            trend = _model_from_result(price_result, TrendSeries)
        except AgentError as exc:
            notes.append(f"价格源失败，已降级：{exc}")

    if trend is not None and trend.degraded:
        # 合成行情不加披露，简报会把随机游走当成真实报价——比报错更糟。
        notes.append(
            f"价格走势为{PRICE_DEGRADED_MARK}（行情源不可用，已降级为合成序列），"
            "不得当作真实行情引用。"
        )

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

    article: Article | None = None
    if news:
        article = await _fetch_article(pool, news[0], notes)

    logger.info(
        "fetch_data 完成：news=%d trend=%s report=%s article=%s risks=%d",
        len(news),
        trend is not None,
        report is not None,
        article is not None,
        len(notes),
    )
    return {
        "news": news,
        "article": article,
        "price_trend": trend,
        "resource_report": report,
        "risk_notes": notes,
    }


async def _fetch_article(pool: ToolCaller, item: NewsItem, notes: list[str]) -> Article | None:
    """抓**最相关那一条**的正文；限 1 篇，失败只记风险、不阻塞。

    正文截断到 ``ARTICLE_EXCERPT_CHARS``（4000）：整篇正文动辄十几万字符，原样进状态
    会撑爆后面那次 LLM 调用。
    """
    try:
        result = await pool.call_tool("news", "fetch_article", {"url": item.url})
        article = _model_from_result(result, Article)
    except Exception as exc:  # 正文只是加分项，拿不到也要出简报
        notes.append(f"正文抓取失败，该条导语改用标题与摘要：{type(exc).__name__}: {exc}")
        return None

    if not article.text.strip():
        # 实测：Google News 的 <link> 是 JS 中转页，返回 200 但正文 0 字符。
        notes.append(
            "抓回的正文为空（Google News 的链接是 JS 中转页，实测正文 0 字符），"
            "该条导语改用标题与摘要。"
        )
    if len(article.text) > ARTICLE_EXCERPT_CHARS:
        article = article.model_copy(update={"text": article.text[:ARTICLE_EXCERPT_CHARS]})
    return article


# --- analyze ----------------------------------------------------------------


def _reconciliation_note(report: ResourceReport) -> str | None:
    """解析合计与报告自报合计对不上时的风险提示。

    JORC 表把同一份资源量按 In-situ / Stockpiles / 全矿三种口径各列一遍；报告里还可能
    有**多个项目**的资源表（内置年报除 Pilgangoora 外还有 Colina）。两种成因都会让
    求和值失真，容差分不出来，所以文案并列写出两种可能、不做断言。
    """
    reconciliation = report.reconciliation
    if reconciliation is None or not reconciliation.used_self_reported:
        return None
    return (
        f"资源量逐条明细求和得 {reconciliation.parsed_total_t / 1e6:.1f} Mt，"
        f"与报告自报合计 {reconciliation.self_reported_total_t / 1e6:.1f} Mt 相差 "
        f"{reconciliation.difference_ratio:+.1%}"
        f"（超过 ±{reconciliation.tolerance:.0%} 容差）——求和值不可信"
        "（常见于同一份资源量被重复计入，或多张不同项目的资源表被加在了一起），"
        "已以报告自报合计为准。"
    )


async def analyze(state: BriefState) -> dict[str, object]:
    """纯计算：资源量交叉核对差额 + 新闻标题里的风险词。

    价格与储量本身由 synthesize 按模板直接渲染，不在这里压成文案——模板写死之后，
    「数字 → 句子」这一步必须唯一，多一条路径就多一处可能对不上的地方。
    """
    notes: list[str] = []

    report = state["resource_report"]
    if report is not None:
        reconciliation_note = _reconciliation_note(report)
        if reconciliation_note is not None:
            notes.append(reconciliation_note)
        if report.excluded_tables:
            # 只报一张表就必须说清楚——否则读者会以为简报里的数字是报告的全部。
            notes.append(
                "资源量只取了报告自报口径最大的那张表，未计入的还有："
                f"{'、'.join(report.excluded_tables)}。"
            )

    for item in state["news"]:
        lowered = f"{item.title} {item.summary}".casefold()
        hits = [keyword for keyword in RISK_KEYWORDS if keyword in lowered]
        if hits:
            notes.append(f"风险信号（{'、'.join(hits)}）：{item.title}")

    return {"risk_notes": notes}


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
        mark = f"（{PRICE_DEGRADED_MARK}）" if trend.degraded else ""
        citations.append(f"{trend.commodity} 价格数据 — {trend.source}{mark}")
    return citations


def _citation_numbers(state: BriefState) -> dict[str, int]:
    """类别 → 编号，只对 ``resource`` / ``price`` 有意义（新闻各自一条，编号即序号）。

    顺序必须与 :func:`build_citations` 一致：新闻 1..N → 资源报告 → 价格。两处一致是
    硬性要求（正文里的 [n] 要指得准），由 ``test_citation_numbers_match_the_reference_list``
    盯着，不靠人记住。
    """
    numbers: dict[str, int] = {}
    offset = len(state["news"])
    if state["resource_report"] is not None:
        offset += 1
        numbers["resource"] = offset
    if state["price_trend"] is not None:
        offset += 1
        numbers["price"] = offset
    return numbers


def _clean_headline(text: str, item: NewsItem) -> str | None:
    """规整 LLM 给的小标题；有剪裁痕迹时返回 ``None``，由调用方兜底。

    **这里不截断**。早先超过 ``NEWS_HEADLINE_MAX_CHARS`` 就砍到 24 字加省略号，结果
    输出「Pilbara Minerals 定于11月24…」这样的半句话——那不是概括，是把一句话剪断了。
    字数由提示词约束（要求自拟 ≤25 字的完整短句），代码只负责拒掉明显不完整的写法：

    - 句末省略号：剪裁留下的痕迹；
    - 原标题的子串：照抄或截取原标题，不是自拟。

    两种都退回「原标题当小标题」的兜底——宁可用一条完整（哪怕偏长）的原始标题，
    也不要半句话。
    """
    collapsed = _WHITESPACE_RE.sub(" ", text).strip()
    if not collapsed or collapsed.endswith(("…", "...")):
        return None
    title = _WHITESPACE_RE.sub(" ", item.title).strip().casefold()
    if collapsed.casefold() in title:
        return None
    return collapsed


@dataclass(frozen=True, slots=True)
class NewsLede:
    """一条新闻的小标题与导语。"""

    headline: str
    lede: str


def _news_prompt_data(state: BriefState) -> str:
    """整理给 LLM 的新闻资料：**每条只带自己**的标题、摘要与正文。

    正文只挂在它自己所属的那一条上（``article.url == item.url``）。把 A 条的正文摆在
    B 条旁边，是「标题与导语主体不一致」这类错误的直接来源。
    """
    article = state["article"]
    blocks: list[str] = []
    for index, item in enumerate(state["news"], 1):
        mark = DEGRADED_MARK if item.degraded else ""
        lines = [
            f"[{index}] {mark}{item.title}",
            f"    来源：{item.source}　发布：{item.published_at.date().isoformat()}",
            f"    摘要：{_WHITESPACE_RE.sub(' ', item.summary).strip() or '（无）'}",
        ]
        if article is not None and article.url == item.url:
            body = _WHITESPACE_RE.sub(" ", article.text).strip()
            lines.append(f"    正文：{body or '（无）'}")
        else:
            # 刻意不写「只能用标题与摘要」这类提示：LLM 会把它原样抄进导语，而
            # 「正文没抓到」这件事由小节末尾的统一注释讲一次就够了（见 NEWS_BODY_NOTE）。
            lines.append("    正文：（无）")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _parse_ledes(text: str, news: list[NewsItem]) -> dict[int, NewsLede]:
    """解析 LLM 给出的 ``{"items": [...]}``；形状不对的条目直接丢弃（走兜底写法）。"""
    payload = _extract_json_object(text)
    if payload is None:
        return {}
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return {}

    ledes: dict[int, NewsLede] = {}
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        index = entry.get("index")
        headline = entry.get("headline")
        lede = entry.get("lede")
        if not isinstance(index, int) or not isinstance(headline, str) or not isinstance(lede, str):
            continue
        if not lede.strip() or not 1 <= index <= len(news):
            continue
        cleaned = _clean_headline(headline, news[index - 1])
        if cleaned is None:
            continue
        ledes[index] = NewsLede(headline=cleaned, lede=lede.strip())
    return ledes


def _fallback_lede(item: NewsItem) -> NewsLede:
    """LLM 没给出这一条时的确定性写法：原标题当小标题，摘要当导语。

    小标题这里**不裁剪、不加省略号**——裁剪出来的半句话正是本轮要消掉的东西。
    """
    headline = _WHITESPACE_RE.sub(" ", item.title).strip()
    summary = _WHITESPACE_RE.sub(" ", item.summary).strip()
    lede = summary or f"{item.source} 于 {item.published_at.date().isoformat()} 报道：{headline}"
    if item.degraded:
        lede = f"{DEGRADED_MARK}{lede}"
    return NewsLede(headline=headline or item.url, lede=lede)


async def _news_ledes(
    state: BriefState, *, strict: bool = False
) -> tuple[dict[int, NewsLede], bool]:
    """让 LLM 为每条新闻写小标题与导语。

    Args:
        state: 当前状态。
        strict: 重试时置 True，在提示词末尾追加一条更强硬的指令。

    Returns:
        ``(导语表, LLM 是否失败)``。**这里不抛异常**：调用失败（鉴权、超时、5xx）
        返回 ``({}, True)``，由调用方决定重试还是走确定性直出——简报必须出得来。
    """
    prompt = _NEWS_PROMPT.format(
        data=_news_prompt_data(state),
        degraded_mark=DEGRADED_MARK,
        headline_max=NEWS_HEADLINE_MAX_CHARS,
    )
    if strict:
        prompt = f"{prompt}\n{_STRICT_RETRY_INSTRUCTION}\n"
    try:
        response = await build_llm().ainvoke(prompt)
    except Exception as exc:  # LLM 挂了不是中断整条流程的理由
        logger.warning("新闻导语生成失败：%s: %s", type(exc).__name__, exc)
        return {}, True
    return _parse_ledes(_message_text(response), state["news"]), False


# --- 模板渲染 ---------------------------------------------------------------
# 模板是**写死**的：小节名、每行的措辞、编号对应都由代码决定。LLM 只填新闻小节。


def _render_title(state: BriefState) -> str:
    return BRIEF_TITLE.format(
        subject=brief_subject(state["topic"]),
        date=datetime.now(UTC).date().isoformat(),
    )


def _body_unavailable(state: BriefState) -> bool:
    """我们尝试抓的那条正文是不是没拿到（压根没抓 / 抓回来是空的）。"""
    article = state["article"]
    return article is None or not article.text.strip()


def _render_news(state: BriefState, ledes: dict[int, NewsLede]) -> str:
    if not state["news"]:
        return (
            f"{SECTION_NEWS}\n\n本轮未检索到与「{brief_subject(state['topic'])}」直接相关的新闻。"
        )
    blocks = [SECTION_NEWS]
    for index, item in enumerate(state["news"], 1):
        lede = ledes.get(index) or _fallback_lede(item)
        blocks.append(f"**{index}. {lede.headline}**\n{lede.lede} [{index}]")
    if _body_unavailable(state):
        blocks.append(NEWS_BODY_NOTE)
    return "\n\n".join(blocks)


def _render_resources(state: BriefState) -> str:
    report = state["resource_report"]
    if report is None or not report.resources:
        return f"{SECTION_RESOURCE}\n\n本轮未取到资源量陈述。"

    number = _citation_numbers(state).get("resource")
    cite = f"（来源 [{number}]，NI 43-101 资源量陈述）：" if number is not None else "："
    header = f"{_subject_prefix(brief_subject(state['topic']))} {report.project_name}{cite}"

    totals: dict[ResourceCategory, float] = {}
    for item in report.resources:
        totals[item.category] = totals.get(item.category, 0.0) + item.tonnage_t
    # 同一类别出现多行时取**第一行**的品位：JORC 表把该类别的主行排在前面。
    # 品位本身在真实年报上多不可靠（见 docs/architecture.md 的已知取舍），
    # 渲染的是解析到的原值，不做加权、不做推测。
    grades: dict[ResourceCategory, tuple[float, str, str]] = {}
    for item in report.resources:
        if item.grade is not None:
            grades.setdefault(item.category, (item.grade, item.grade_unit, item.commodity))

    bullets: list[str] = []
    # 固定 Measured → Indicated → Inferred 顺序，不按字典序也不按出现顺序。
    for category in (
        ResourceCategory.MEASURED,
        ResourceCategory.INDICATED,
        ResourceCategory.INFERRED,
    ):
        tonnes = totals.get(category)
        if not tonnes:
            continue
        grade = grades.get(category)
        suffix = f"，品位 {grade[0]}{grade[1]} {grade[2]}" if grade is not None else ""
        # 吨位**只能**说「矿石量 X Mt」：commodity 是品位所指的元素，不是吨位的单位，
        # 写成「Li2O 19.0 Mt」是病句（早期版本真这么写过）。
        bullets.append(f"- {category.value}：矿石量 {tonnes / 1e6:.1f} Mt{suffix}")
    if not grades:
        bullets.append("- 品位：该表未解析到品位数")

    total = sum(totals.values())
    reconciliation = report.reconciliation
    if reconciliation is None:
        bullets.append(f"- 合计：{total / 1e6:.1f} Mt（报告未给出自报合计，此为明细求和）")
    elif reconciliation.used_self_reported:
        bullets.append(
            f"- 合计：{reconciliation.self_reported_total_t / 1e6:.1f} Mt（报告自报合计；"
            f"明细求和 {total / 1e6:.1f} Mt，相差 {reconciliation.difference_ratio:+.1%}，"
            "详见风险提示）"
        )
    else:
        bullets.append(f"- 合计：{total / 1e6:.1f} Mt（与报告自报合计一致）")

    return "\n\n".join([SECTION_RESOURCE, header, "\n".join(bullets)])


def _render_price(state: BriefState) -> str:
    trend = state["price_trend"]
    if trend is None:
        return f"{SECTION_PRICE}\n\n本轮未取到价格数据。"

    number = _citation_numbers(state).get("price")
    cite = f" [{number}]" if number is not None else ""
    latest = trend.points[-1]
    ma7 = f"{trend.ma7:.4g}" if trend.ma7 is not None else "数据不足"
    ma30 = f"{trend.ma30:.4g}" if trend.ma30 is not None else "数据不足"
    line = (
        f"{trend.commodity}：最新价 {latest.price:.4g} {latest.unit}，"
        f"区间涨跌 {trend.change_pct:+.2f}%，7 日均线 {ma7}，30 日均线 {ma30}{cite}。"
    )
    # 尾注用数据源自带的说明，不自己改写：代理品种的免责声明必须原样落地。
    tail = f"（{trend.source}）"
    if trend.degraded:
        # 合成序列必须在本节里就点明，不能只躺在风险提示里。
        line += f" **{PRICE_DEGRADED_MARK}。**"
    return "\n\n".join([SECTION_PRICE, line, tail])


def _render_risks(state: BriefState, extra_notes: list[str]) -> str:
    notes = [*state["risk_notes"], *extra_notes]
    body = "\n".join(f"- {note}" for note in notes) if notes else "本轮无重大风险事件。"
    return f"{SECTION_RISK}\n\n{body}"


def _render_citations(state: BriefState) -> str:
    citations = build_citations(state)
    if not citations:
        return f"{SECTION_CITATIONS}\n\n（本次没有取到可引用的来源）"
    body = "\n".join(f"[{index}] {text}" for index, text in enumerate(citations, 1))
    return f"{SECTION_CITATIONS}\n\n{body}"


def _assemble(state: BriefState, ledes: dict[int, NewsLede], extra_notes: list[str]) -> str:
    """把各小节拼成完整简报。抽成函数是因为契约校验失败时要能整体重来一次。"""
    blocks = [
        _render_title(state),
        _render_news(state, ledes),
        _render_resources(state),
        _render_price(state),
        _render_risks(state, extra_notes),
        _render_citations(state),
    ]
    return "\n\n".join(blocks).rstrip() + "\n"


def _missing_sections(document: str) -> list[str]:
    """返回文档里缺失的必需小节标题。

    四个小节全部由代码渲染，所以正常情况下这里永远为空——它是一道**回归护栏**：
    改 `SECTION_*` 或渲染分支时漏掉一节，会被当场抓住，而不是让读者拿到半份简报。
    """
    return [section for section in REQUIRED_SECTIONS if section not in document]


async def synthesize(state: BriefState) -> dict[str, object]:
    """按写死的模板渲染简报正文。

    LLM 只写新闻小节的小标题与导语——那两样确实需要读正文；其余小节全部由代码渲染。
    小节名、编号对应、「矿石量」这类措辞是硬性约束，靠提示词保证不了，靠代码可以。

    三道收口，缺一不可：

    1. LLM 调用失败（鉴权 / 超时 / 5xx）**不中断**，新闻退回首标题 + 摘要的确定性写法；
    2. 拼装完成后校验四节契约，缺失就带严格指令**重试一次**（只影响 LLM 那一节）；
    3. 重试后仍不合契约，或 LLM 根本不可用，就整体走**确定性直出**，风险提示里注明。
    """
    ledes: dict[int, NewsLede] = {}
    llm_failed = False
    if state["news"]:
        ledes, llm_failed = await _news_ledes(state)

    extra_notes: list[str] = []
    document = _assemble(state, ledes, extra_notes)

    missing = _missing_sections(document)
    if missing:
        logger.warning("简报缺少小节 %s，带严格指令重试一次", "、".join(missing))
        if state["news"]:
            ledes, llm_failed = await _news_ledes(state, strict=True)
        document = _assemble(state, ledes, extra_notes)
        missing = _missing_sections(document)

    if missing or llm_failed:
        if missing:
            logger.warning("重试后仍缺少小节 %s，改用确定性模板直出", "、".join(missing))
        extra_notes = [LLM_FALLBACK_NOTE]
        document = _assemble(state, {}, extra_notes)

    return {"markdown": document, "risk_notes": extra_notes}


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
    """写入文件，返回最终 Markdown。

    正文已由 synthesize 按模板渲染完毕（含「引用源」小节），这里不再拼接任何内容——
    拼接逻辑散在两个节点里，改了一处漏一处就会正文与落盘文件不一致。
    """
    document = state["markdown"]
    path = report_path(state["topic"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")
    logger.info("简报已写入：path=%s chars=%d", path, len(document))
    return {"markdown": document, "citations": build_citations(state)}
