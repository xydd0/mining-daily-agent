"""基于 pdfplumber 的矿产资源报告解析实现。

解析策略：按空行切成文本块，只在**含资源量关键词**的块内做正则抽取；
每个类别关键词开启一个"段"，段内第一个吨位与第一个品位归给该类别。
抽取本身就是启发式的，因此抽不到条目时**不报错**——把候选原文放进
``raw_snippets`` 交给上层判断（见 CLAUDE.md「可靠性」）。
"""

from __future__ import annotations

import io
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, override
from urllib.parse import unquote, urlparse

import httpx
import pdfplumber

from mining_daily_agent.models.resources import ResourceCategory, ResourceItem, ResourceReport
from mining_daily_agent.providers import BROWSER_USER_AGENT, net
from mining_daily_agent.providers.pdf.base import PdfProvider

logger = logging.getLogger(__name__)

#: 单次 HTTP 请求的超时秒数。
PDF_TIMEOUT_SECONDS: Final = 30.0
#: 指数退避重试的最大尝试次数（含首次）。
MAX_ATTEMPTS: Final = 3
#: 退避基数：第 n 次失败后等待 BACKOFF_BASE_SECONDS * 2**(n-1) 秒。
BACKOFF_BASE_SECONDS: Final = 0.5
#: PDF 文件头魔数。
PDF_MAGIC: Final = b"%PDF-"
#: 认可的 PDF Content-Type。
ACCEPTED_CONTENT_TYPES: Final[frozenset[str]] = frozenset({"application/pdf", "application/x-pdf"})
#: 单个 raw_snippet 的最大长度。
MAX_SNIPPET_CHARS: Final = 1000
#: 识别不出矿种时的占位值。
UNKNOWN_COMMODITY: Final = "unknown"

#: 单条目吨位的可信区间（吨）。超出即视为正则误配，丢弃并只留在 raw_snippets 里。
#:
#: 下界 1e5 t（0.1 Mt）——真实资源量很少小于这个量级；上界 1e10 t（1 万 Mt）——
#: 全球最大的矿床也在其下。注意典型值是 1e8 量级（如 Pilgangoora Indicated 349 Mt
#: = 3.49e8 t），所以区间必须覆盖到那里。
TONNAGE_MIN_T: Final = 1e5
TONNAGE_MAX_T: Final = 1e10

_KEYWORD_RE: Final = re.compile(
    r"\b(?:mineral\s+resources?|measured|indicated|inferred)\b",
    re.IGNORECASE,
)
# 类别**只认首字母大写或全大写**，刻意不加 IGNORECASE。
# 实测一份真实年报（Pilbara Minerals 2025）：87 条抽取里有 80 条来自这种误报——
# 会计与绩效正文里的 "measured at fair value"、"where indicated in the Annual Report"、
# "measured against the Baseline"。而资源表里的 15 处类别词**全部**是首字母大写。
# 这一条改动的效果：87 条 → 16 条，且真表的行一条不少。
_CATEGORY_RE: Final = re.compile(
    r"\b(?P<category>Measured|Indicated|Inferred|MEASURED|INDICATED|INFERRED)\b"
)
# 单位按长度降序排列，避免 "Mt" 被 "t" 抢先匹配。
_TONNAGE_RE: Final = re.compile(
    r"(?P<value>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>million\s+tonnes|Mt|kt|tonnes|t)\b",
    re.IGNORECASE,
)
_GRADE_RE: Final = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>g/t|%)", re.IGNORECASE)
_COMMODITY_RE: Final = re.compile(
    r"\b(?P<commodity>Li2O|Li|Cu|Au|Ag|Fe|Zn|Pb|Ni|Sn|Mo|Co|U3O8|TREO|REE|Ta2O5|Nb2O5|Graphite)\b"
)
_WHITESPACE_RE: Final = re.compile(r"\s+")

#: 各类单位换算成"吨"的系数。
_TONNAGE_FACTORS: Final[dict[str, float]] = {
    "mt": 1e6,
    "million tonnes": 1e6,
    "kt": 1e3,
    "tonnes": 1.0,
    "t": 1.0,
}

# --- 表头单位推断用到的模式 -------------------------------------------------
# 真实 NI 43-101 报告的表格几乎都是「表头写单位、单元格只放数字」，
# 没有下面这组模式就只能抽到整句写法的少数文档。
#: 表头里的吨位关键词。
_TONNAGE_HEADER_RE: Final = re.compile(r"\b(?:tonnage|tonnes?|ore|resource)\b", re.IGNORECASE)
#: 表头里的吨位单位，形如 "(Mt)"。
_PAREN_TONNAGE_UNIT_RE: Final = re.compile(r"\(\s*(?P<unit>Mt|kt|t)\s*\)", re.IGNORECASE)
#: 表头里的品位关键词。
_GRADE_HEADER_RE: Final = re.compile(r"\bgrade\b", re.IGNORECASE)
#: 表头里的品位单位，形如 "(% Li2O)"、"（g/t Au）"。
_PAREN_GRADE_UNIT_RE: Final = re.compile(r"\(\s*(?P<unit>g/t|%)\s*[^)]*\)", re.IGNORECASE)
#: 不带单位的裸数字，用于上述表格形态。
_BARE_NUMBER_RE: Final = re.compile(r"\d[\d,]*(?:\.\d+)?")

# --- 分块与库存 -------------------------------------------------------------
#: 行内「边界品位说明」的括号组，形如 ``(≥0.2% Li2O)``、``(0.3% Li2O cut-off)``。
#: 组里的百分比是**筛选阈值**（用什么品位下限圈的矿），不是矿体品位——实测不排除时
#: 会把 Pilgangoora 的 1.33% Li2O 记成 0.2%。
_CUTOFF_NOTE_RE: Final = re.compile(
    r"\([^)]*(?:[≥≤<>]|cut[\s\-]?off|minimum)[^)]*\)",
    re.IGNORECASE,
)
#: 矿石库存关键词。``Stockpiles`` 分块是已采出矿石的堆存量（JORC 表里单列），
#: 属于库存而不属于**原地**矿产资源量，整块剔除。
_STOCKPILE_RE: Final = re.compile(r"\bstockpiles?\b", re.IGNORECASE)
#: 自报合计行的行首写法（JORC 表里是 "Sub total" 与 "Total"）。
_TOTAL_ROW_RE: Final = re.compile(r"^\s*(?:Sub\s+)?total\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _UnitHints:
    """从表头推断出的单位提示；取不到为 None。"""

    tonnage_unit: str | None = None
    grade_unit: str | None = None


class PdfFetchError(RuntimeError):
    """PDF 不可用：下载失败，或响应根本不是 PDF。"""


def _http_get(url: str) -> httpx.Response:
    """发出单次 GET。

    这是本模块唯一的 HTTP 接缝：超时、请求头、大小上限与安全护栏都在这里统一设置，
    测试也在这里替换。

    带浏览器 UA 作为兼容手段：**并非所有站点都需要**（pls.com 的年报、Yahoo 的行情接口
    不带也能取到），但部分站点会对非浏览器 UA 直接 403（实测 mining.com 的文章页）。

    PDF 给到 50 MB 上限：内置年报就有 15.6 MB，但也不该任由一个链接把内存吃光。
    """
    return net.get_capped(
        url,
        max_bytes=net.PDF_MAX_BYTES,
        source_name="PDF 报告",
        timeout=PDF_TIMEOUT_SECONDS,
        headers={"User-Agent": BROWSER_USER_AGENT, "Accept": "application/pdf,*/*"},
    )


def _validate_pdf_response(response: httpx.Response) -> None:
    """校验响应确实是 PDF：先看 Content-Type，再用文件头兜底确认。

    Raises:
        PdfFetchError: 两者都不像 PDF。
    """
    content_type = response.headers.get("content-type", "")
    normalized = content_type.split(";")[0].strip().lower()
    if normalized in ACCEPTED_CONTENT_TYPES:
        return
    if response.content.startswith(PDF_MAGIC):
        logger.debug(
            "Content-Type=%r 非 PDF，但文件头是 %%PDF-，按 PDF 处理。",
            content_type,
        )
        return
    msg = f"响应不是 PDF：Content-Type={content_type!r}，且文件头不是 {PDF_MAGIC.decode()}。"
    raise PdfFetchError(msg)


def _download_pdf(url: str) -> bytes:
    """下载 PDF，带超时与指数退避重试。

    Content-Type 不对属于确定性错误，不重试，立即抛出。
    """
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = _http_get(url)
            response.raise_for_status()
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            last_error = exc
            will_retry = attempt < MAX_ATTEMPTS
            logger.warning(
                "PDF 下载失败：url=%s attempt=%d/%d retrying=%s error=%s: %s",
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
            _validate_pdf_response(response)
            return response.content

    msg = (
        f"下载 {url} 连续失败 {MAX_ATTEMPTS} 次，"
        f"最后一次错误：{type(last_error).__name__}: {last_error}"
    )
    raise PdfFetchError(msg) from last_error


def extract_text(payload: bytes) -> str:
    """逐页提取 PDF 文本，页与页之间以空行分隔。"""
    with pdfplumber.open(io.BytesIO(payload)) as pdf:
        return "\n\n".join(page.extract_text() or "" for page in pdf.pages)


def _blocks(text: str) -> list[str]:
    """按空行切分文本块，去掉全空白块。"""
    return [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]


def _collapse(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _truncate(text: str, limit: int = MAX_SNIPPET_CHARS) -> str:
    collapsed = _collapse(text)
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _tonnage_to_tonnes(value: str, unit: str) -> float:
    """把形如 ``214 Mt`` 的吨位换算成吨。"""
    factor = _TONNAGE_FACTORS[_collapse(unit).lower()]
    return float(value.replace(",", "")) * factor


def _find_commodity(*candidates: str) -> str:
    """在候选文本中按给定顺序找矿种符号，找不到返回 ``unknown``。"""
    for text in candidates:
        match = _COMMODITY_RE.search(text)
        if match is not None:
            return match.group("commodity")
    return UNKNOWN_COMMODITY


def _header_unit_hints(block: str) -> _UnitHints:
    """从表头行里取单位提示，形如 ``Tonnage (Mt)``、``Grade (% Li2O)``。

    只认「表头关键词 + 括号里的单位」这一最小形态，不做真正的列表格切分。
    """
    tonnage_unit: str | None = None
    grade_unit: str | None = None
    for line in block.splitlines():
        if tonnage_unit is None and _TONNAGE_HEADER_RE.search(line):
            match = _PAREN_TONNAGE_UNIT_RE.search(line)
            if match is not None:
                tonnage_unit = match.group("unit").lower()
        if grade_unit is None and _GRADE_HEADER_RE.search(line):
            match = _PAREN_GRADE_UNIT_RE.search(line)
            if match is not None:
                grade_unit = match.group("unit").lower()
        if tonnage_unit is not None and grade_unit is not None:
            break
    return _UnitHints(tonnage_unit, grade_unit)


def _overlaps(first: tuple[int, int], second: tuple[int, int]) -> bool:
    """判断两个 span 是否相交。"""
    return first[0] < second[1] and second[0] < first[1]


def _resolve_tonnage(
    segment: str, header_unit: str | None
) -> tuple[float | None, tuple[int, int] | None]:
    """先按显式单位解析吨位；失败时若表头给了单位，取段内第一个裸数字。"""
    match = _TONNAGE_RE.search(segment)
    if match is not None:
        return _tonnage_to_tonnes(match.group("value"), match.group("unit")), match.span()
    if header_unit is None:
        return None, None
    bare = _BARE_NUMBER_RE.search(segment)
    if bare is None:
        return None, None
    return float(bare.group(0).replace(",", "")) * _TONNAGE_FACTORS[header_unit], bare.span()


def _resolve_grade(
    segment: str, header_unit: str | None, consumed: tuple[int, int] | None
) -> tuple[float | None, str]:
    """先按显式单位解析品位；失败时若表头给了单位，取未被吨位占用的第一个裸数字。

    **边界品位说明里的百分数不算品位**：``In-situ Measured 18 1.33 …`` 后面紧跟
    ``(≥0.2% Li2O)``，那是筛选阈值。段内没有别的品位写法时宁可返回 None——
    「没有品位」比「一个错的品位」诚实，下游也不会拿 0.2% 去做任何判断。
    """
    notes = [note.span() for note in _CUTOFF_NOTE_RE.finditer(segment)]
    for match in _GRADE_RE.finditer(segment):
        if any(_overlaps(match.span(), note) for note in notes):
            continue
        return float(match.group("value")), match.group("unit").lower()
    if header_unit is None:
        return None, ""
    for number in _BARE_NUMBER_RE.finditer(segment):
        if consumed is not None and _overlaps(number.span(), consumed):
            continue
        if any(_overlaps(number.span(), note) for note in notes):
            continue
        return float(number.group(0).replace(",", "")), header_unit
    return None, ""


def _item_from_segment(
    category: str, segment: str, hints: _UnitHints, block: str
) -> ResourceItem | None:
    """把「类别词之后的那一段」解析成一条记录；解析不出可用吨位时返回 None。

    ``category`` 必须是已经通过大小写筛选的类别词原文。

    段内优先用**自带单位**的写法（``Indicated: 214 Mt @ 1.15% Li2O``）；没有自带单位时
    退回**表头推断**：表头给了吨位单位就按它解析段内第一个裸数字，给了品位单位就取其后
    第一个未被占用的裸数字——即 ``Indicated  214  1.15`` 配
    ``Category  Tonnage (Mt)  Grade (% Li2O)`` 的表格形态。表头推断假定列序为
    「吨位在前、品位在后」。
    """
    tonnage_t, consumed = _resolve_tonnage(segment, hints.tonnage_unit)
    if tonnage_t is None:
        # 既没有显式单位、表头也没给单位：(多半是表头或叙述句) 跳过。
        return None
    if not TONNAGE_MIN_T <= tonnage_t <= TONNAGE_MAX_T:
        # 量级不合理：多半是正则配到了某个无关数字。不进 resources——
        # 宁可少给也不要给错，那段原文仍留在 raw_snippets 里供人工核对。
        logger.debug(
            "丢弃量级可疑的吨位：category=%s tonnage_t=%.3g 区间=[%.0e, %.0e]",
            category,
            tonnage_t,
            TONNAGE_MIN_T,
            TONNAGE_MAX_T,
        )
        return None

    grade, grade_unit = _resolve_grade(segment, hints.grade_unit, consumed)
    return ResourceItem(
        category=ResourceCategory(category.capitalize()),
        commodity=_find_commodity(segment, block),
        tonnage_t=tonnage_t,
        grade=grade,
        grade_unit=grade_unit,
    )


def _items_from_prose(block: str, hints: _UnitHints) -> list[ResourceItem]:
    """散文形态：一个类别词开启一段，段的范围到下一个类别词为止，**可以跨行**。"""
    matches = list(_CATEGORY_RE.finditer(block))
    items: list[ResourceItem] = []
    for index, match in enumerate(matches):
        segment_end = matches[index + 1].start() if index + 1 < len(matches) else len(block)
        item = _item_from_segment(
            match.group("category"), block[match.end() : segment_end], hints, block
        )
        if item is not None:
            items.append(item)
    return items


@dataclass(slots=True)
class _Section:
    """资源表里的一个分块（``In-situ`` / ``Stockpiles`` / 矿名），可带自报小计。"""

    label: str | None
    items: list[ResourceItem]
    self_reported_total_t: float | None = None


def _is_stockpile(label: str | None) -> bool:
    """分块名里含 stockpile 的，是已采出矿石的堆存量，不属于**原地**矿产资源量。"""
    return label is not None and _STOCKPILE_RE.search(label) is not None


def _section_label(line: str, category_start: int) -> str | None:
    """取「类别词之前的那截」当分块名；取不到返回 None。

    ``In-situ Measured 18 …`` → ``In-situ``；``Stockpiles Measured 1 …`` → ``Stockpiles``。
    而 ``Indicated 349 …`` 前面什么都没有，``(≥0.2% Li2O) Indicated 349 …`` 前面只有
    边界品位说明——两者都不算分块名，沿用上一行的分块。
    """
    prefix = _CUTOFF_NOTE_RE.sub(" ", line[:category_start])
    if not any(character.isalpha() for character in prefix):
        return None
    return _collapse(prefix)[:80]


def _sections_from_table(block: str, hints: _UnitHints) -> list[_Section]:
    """按行解析表格块，切成若干分块，并逐块记下自报小计。

    一行 = 一条记录。类别词之前的文字若像分块名（``In-situ``、``Stockpiles``），
    就开启新分块；``Sub total`` / ``Total`` 开头的行为该分块的**自报小计**，
    并且**结束**这个分块——后面的行属于下一张表。

    两条规则都是被真实年报逼出来的：

    - 逐行解析：早先按「类别词到下一个类别词」跨行切段，Stockpiles 那行没有数值时
      就会把下一行的 ``Sub total`` 数字取来当吨位；
    - 合计行结束分块：Table 5（资源量）与 Table 6（储量）在真实年报里排在**同一个
      文本块**里，而储量表的行用 Proved / Probable 作类别词，解析器认不出，于是
      储量表的 ``Sub total 207.2`` 一路覆盖掉了资源量表的 ``Sub total 445``——
      全矿口径凭空缩水一半还多。
    """
    sections: list[_Section] = []
    current = _Section(label=None, items=[])
    for line in block.splitlines():
        matches = list(_CATEGORY_RE.finditer(line))
        if not matches:
            total_match = _TOTAL_ROW_RE.match(line)
            if total_match is not None:
                subtotal, _ = _resolve_tonnage(line[total_match.end() :], hints.tonnage_unit)
                if subtotal is not None and TONNAGE_MIN_T <= subtotal <= TONNAGE_MAX_T:
                    current.self_reported_total_t = subtotal
                    sections.append(current)
                    current = _Section(label=None, items=[])
            continue
        label = _section_label(line, matches[0].start())
        if label is not None and label != current.label:
            # 同名标签不另起分块：有的表在每一行都重复写一遍分块名。
            sections.append(current)
            current = _Section(label=label, items=[])
        for index, match in enumerate(matches):
            segment_end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
            item = _item_from_segment(
                match.group("category"), line[match.end() : segment_end], hints, block
            )
            if item is not None:
                current.items.append(item)
    sections.append(current)
    return [
        section
        for section in sections
        if section.items or section.self_reported_total_t is not None
    ]


def _pick_sections(sections: list[_Section]) -> list[_Section]:
    """从**全部**表格分块里挑出应当计入的那些。

    1. 含 stockpile 的分块**整块剔除**——矿石库存不是矿产资源量；
    2. 只要有分块带自报合计，就**只取合计最大的那一块**。JORC 表把同一份资源量按
       In-situ / Stockpiles / 全矿三种口径各列一遍（实测 Pilgangoora 436 + 9 + 445，
       相加得 890，正好是真值 445 的两倍）；报告里还可能有**多个项目**的资源表
       （内置年报除 Pilgangoora 445 Mt 外还有 Colina 70.9 Mt），跨块相加得到的是个
       没有意义的和。取合计最大的那一块，就是报告自己的旗舰口径；
    3. 一个自报合计都没有时，才全部保留。

    **比较是全局的，不是逐块各自取最大**：分块散在多个文本块里时，逐块取各自的
    最大值仍会把两个项目加在一起——实测就是这么得到 515.9 Mt 的。
    """
    usable = [section for section in sections if not _is_stockpile(section.label)]
    totalled = [
        section for section in usable if section.self_reported_total_t is not None and section.items
    ]
    if totalled:
        best = max(totalled, key=lambda section: section.self_reported_total_t or 0.0)
        logger.debug(
            "按自报合计选取分块：label=%s total_t=%.3g 候选=%d（其余分块不计入，避免重复计入）",
            best.label,
            best.self_reported_total_t or 0.0,
            len(totalled),
        )
        return [best]
    return usable


def parse_self_reported_total(text: str) -> float | None:
    """取报告**自报**的资源量合计吨位。

    JORC 资源表通常既有各分块的 "Sub total"，也有全矿的 "Total"。取其中**最大**的一个
    即是全矿总计——总计不会小于任何分块小计。含 stockpile 的分块不参与：那是矿石库存。

    这个值是交叉核对用的基准：同一份报告里「分块」与「总计」并存，按类别直接把所有
    行相加会把同一份资源量算两遍。

    ⚠️ 已知局限：若同一文本块里既有资源量表也有储量表（真实年报常把 Table 5/6 排在同一
    页），这里的候选会混入**储量**的合计行。目前靠「取最大」侥幸躲过（资源量 445 Mt >
    储量 207.2 Mt），但这层保障很薄——要稳妥需要先做表格区域切分。

    Returns:
        合计吨位；找不到可识别的合计行时返回 None。
    """
    candidates: list[float] = []
    for block in _blocks(text):
        hints = _header_unit_hints(block)
        if hints.tonnage_unit is None:
            continue
        for section in _sections_from_table(block, hints):
            if section.self_reported_total_t is None or _is_stockpile(section.label):
                continue
            candidates.append(section.self_reported_total_t)
    return max(candidates) if candidates else None


@dataclass(frozen=True, slots=True)
class ParsedResources:
    """一次解析的全部产出。"""

    items: list[ResourceItem]
    snippets: list[str]
    #: 报告里有、但**没有计入** ``items`` 的资源表（形如 ``"Colina — 70.9 Mt"``）。
    #: 只在报告含多张资源表、而只采信其中一张时非空；见 :func:`_pick_sections`。
    excluded_tables: list[str]


def parse_resources(text: str) -> ParsedResources:
    """从 PDF 文本中抽取资源量条目、溯源片段与「未计入的资源表」。

    表头单位会**只沿用到紧邻的下一个块**：PDF 文本提取常把表头与数据行切成两块，
    只在块内找表头会漏掉单位。但沿用范围必须限定——早先一路沿用到底，导致真表的
    "Tonnes (Mt)" 表头泄漏进后面几十块会计正文，把那些段落里的任意数字都当成了吨位。

    表格块与散文块分两路抽取，最后**全局**挑块：表格块各自切成若干分块（见
    :func:`_sections_from_table`），全部汇总后按自报合计挑一张（见
    :func:`_pick_sections`）。散文块没有分块结构，只在没有任何带自报合计的分块时
    才予采用——有权威表格时，散落正文里的数字往往是同一份资源量的另一种说法。
    """
    prose_items: list[ResourceItem] = []
    snippets: list[str] = []
    sections: list[_Section] = []
    inherited = _UnitHints()
    for block in _blocks(text):
        # 表头块本身可能不含类别关键词，因此先取提示、再做关键词过滤。
        own = _header_unit_hints(block)
        effective = _UnitHints(
            own.tonnage_unit or inherited.tonnage_unit,
            own.grade_unit or inherited.grade_unit,
        )
        # 本块自带表头 → 对下一块有效；本块没有 → 用完即失效，不再往下传。
        inherited = own if (own.tonnage_unit or own.grade_unit) else _UnitHints()

        if not _KEYWORD_RE.search(block):
            continue
        snippets.append(_truncate(block))
        if effective.tonnage_unit is None:
            # 没有表头单位 → 散文形态：一个类别词开启一段，段可以跨行。
            prose_items.extend(_items_from_prose(block, effective))
        else:
            # 有表头单位 → 表格形态：一行 = 一条记录，识别分块与自报合计。
            sections.extend(_sections_from_table(block, effective))

    picked = _pick_sections(sections)
    excluded = [
        _format_table(section)
        for section in sections
        if section.items
        and not _is_stockpile(section.label)
        and all(section is not kept for kept in picked)
    ]

    items = [item for section in picked for item in section.items]
    if not any(section.self_reported_total_t is not None for section in picked):
        items.extend(prose_items)
    return ParsedResources(items=items, snippets=snippets, excluded_tables=excluded)


def _format_table(section: _Section) -> str:
    """把未计入的分块写成一行可读的说明。

    优先用**报告自报**的小计——逐条求和与它可能因四舍五入差一点（实测 In-situ
    自报 436、逐行加得 437），披露时用报告自己的数字。
    """
    total = section.self_reported_total_t
    if total is None:
        total = sum(item.tonnage_t for item in section.items)
    label = section.label or "未命名分块"
    return f"{label} — {total / 1e6:.1f} Mt"


def parse_resource_text(text: str) -> tuple[list[ResourceItem], list[str]]:
    """从 PDF 文本中抽取资源量条目与溯源片段（:func:`parse_resources` 的简化视图）。

    Returns:
        ``(条目列表, 命中关键词的原文块列表)``。抽不到条目时第一个列表为空，
        第二个列表仍会给出候选原文。
    """
    parsed = parse_resources(text)
    return parsed.items, parsed.snippets


def _guess_project_name(text: str, url: str) -> str:
    """取正文第一行作为项目名；正文为空时回退到 URL 里的文件名。"""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:120]
    stem = Path(unquote(urlparse(url).path)).stem
    return stem or "unknown"


class PdfResourceProvider(PdfProvider):
    """下载 PDF 并抽取资源量的真实实现。"""

    @override
    def extract_resources(self, pdf_url: str) -> ResourceReport:
        """下载并解析 PDF，返回结构化的资源量报告。

        Raises:
            PdfFetchError: 下载失败或响应不是 PDF。解析失败**不**在此列。
        """
        fetched_at = datetime.now(UTC)
        payload = _download_pdf(pdf_url)
        text = extract_text(payload)
        parsed = parse_resources(text)

        logger.info(
            "PDF 解析完成：url=%s text_len=%d resources=%d snippets=%d excluded_tables=%d",
            pdf_url,
            len(text),
            len(parsed.items),
            len(parsed.snippets),
            len(parsed.excluded_tables),
        )
        return ResourceReport(
            project_name=_guess_project_name(text, pdf_url),
            source_url=pdf_url,
            fetched_at=fetched_at,
            resources=parsed.items,
            raw_snippets=parsed.snippets,
            excluded_tables=parsed.excluded_tables,
            self_reported_total_t=parse_self_reported_total(text),
        )
