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
from mining_daily_agent.providers import BROWSER_USER_AGENT
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


@dataclass(frozen=True, slots=True)
class _UnitHints:
    """从表头推断出的单位提示；取不到为 None。"""

    tonnage_unit: str | None = None
    grade_unit: str | None = None


class PdfFetchError(RuntimeError):
    """PDF 不可用：下载失败，或响应根本不是 PDF。"""


def _http_get(url: str) -> httpx.Response:
    """发出单次 GET。

    这是本模块唯一的 HTTP 接缝：超时与请求头在此统一设置，测试也在这里替换。

    带浏览器 UA 作为兼容手段：**并非所有站点都需要**（pls.com 的年报、Yahoo 的行情接口
    不带也能取到），但部分站点会对非浏览器 UA 直接 403（实测 mining.com 的文章页）。
    """
    return httpx.get(
        url,
        timeout=PDF_TIMEOUT_SECONDS,
        follow_redirects=True,
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
    """先按显式单位解析品位；失败时若表头给了单位，取未被吨位占用的第一个裸数字。"""
    match = _GRADE_RE.search(segment)
    if match is not None:
        return float(match.group("value")), match.group("unit").lower()
    if header_unit is None:
        return None, ""
    for number in _BARE_NUMBER_RE.finditer(segment):
        if consumed is not None and _overlaps(number.span(), consumed):
            continue
        return float(number.group(0).replace(",", "")), header_unit
    return None, ""


def _items_from_block(block: str, hints: _UnitHints) -> list[ResourceItem]:
    """在一个文本块内按类别切段，逐段抽取吨位与品位。

    每个类别关键词开启一段，段的范围是它到下一个类别关键词之间。段内优先用
    **自带单位**的写法（``Indicated: 214 Mt @ 1.15% Li2O``）；没有自带单位时退回
    **表头推断**：表头给了吨位单位就按它解析段内第一个裸数字，给了品位单位就取
    其后第一个未被占用的裸数字——即 ``Indicated  214  1.15`` 配
    ``Category  Tonnage (Mt)  Grade (% Li2O)`` 的表格形态。

    表头推断是启发式的，它假定表格列序为「吨位在前、品位在后」。
    """
    matches = list(_CATEGORY_RE.finditer(block))
    items: list[ResourceItem] = []
    for index, match in enumerate(matches):
        segment_end = matches[index + 1].start() if index + 1 < len(matches) else len(block)
        segment = block[match.end() : segment_end]

        tonnage_t, consumed = _resolve_tonnage(segment, hints.tonnage_unit)
        if tonnage_t is None:
            # 既没有显式单位、表头也没给单位：(多半是表头或叙述句) 跳过。
            continue
        if not TONNAGE_MIN_T <= tonnage_t <= TONNAGE_MAX_T:
            # 量级不合理：多半是正则配到了某个无关数字。不进 resources——
            # 宁可少给也不要给错，那段原文仍留在 raw_snippets 里供人工核对。
            logger.debug(
                "丢弃量级可疑的吨位：category=%s tonnage_t=%.3g 区间=[%.0e, %.0e]",
                match.group("category"),
                tonnage_t,
                TONNAGE_MIN_T,
                TONNAGE_MAX_T,
            )
            continue

        grade, grade_unit = _resolve_grade(segment, hints.grade_unit, consumed)
        items.append(
            ResourceItem(
                category=ResourceCategory(match.group("category").capitalize()),
                commodity=_find_commodity(segment, block),
                tonnage_t=tonnage_t,
                grade=grade,
                grade_unit=grade_unit,
            )
        )
    return items


#: 自报合计行的行首写法（JORC 表里是 "Sub total" 与 "Total"）。
_TOTAL_ROW_RE: Final = re.compile(r"^\s*(?:Sub\s+)?total\b", re.IGNORECASE)


def parse_self_reported_total(text: str) -> float | None:
    """取报告**自报**的资源量合计吨位。

    JORC 资源表通常既有各分块的 "Sub total"，也有全矿的 "Total"，都在同一张表里。
    取其中**最大**的一个即是全矿总计——总计不会小于任何分块小计。

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
        for line in block.splitlines():
            if not _TOTAL_ROW_RE.match(line):
                continue
            number = _BARE_NUMBER_RE.search(line)
            if number is None:
                continue
            candidates.append(
                float(number.group(0).replace(",", "")) * _TONNAGE_FACTORS[hints.tonnage_unit]
            )
    return max(candidates) if candidates else None


def parse_resource_text(text: str) -> tuple[list[ResourceItem], list[str]]:
    """从 PDF 文本中抽取资源量条目与溯源片段。

    表头单位会**只沿用到紧邻的下一个块**：PDF 文本提取常把表头与数据行切成两块，
    只在块内找表头会漏掉单位。但沿用范围必须限定——早先一路沿用到底，导致真表的
    "Tonnes (Mt)" 表头泄漏进后面几十块会计正文，把那些段落里的任意数字都当成了吨位。

    Returns:
        ``(条目列表, 命中关键词的原文块列表)``。抽不到条目时第一个列表为空，
        第二个列表仍会给出候选原文。
    """
    items: list[ResourceItem] = []
    snippets: list[str] = []
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
        items.extend(_items_from_block(block, effective))
    return items, snippets


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
        resources, snippets = parse_resource_text(text)

        logger.info(
            "PDF 解析完成：url=%s text_len=%d resources=%d snippets=%d",
            pdf_url,
            len(text),
            len(resources),
            len(snippets),
        )
        return ResourceReport(
            project_name=_guess_project_name(text, pdf_url),
            source_url=pdf_url,
            fetched_at=fetched_at,
            resources=resources,
            raw_snippets=snippets,
            self_reported_total_t=parse_self_reported_total(text),
        )
