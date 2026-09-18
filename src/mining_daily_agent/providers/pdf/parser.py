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
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, override
from urllib.parse import unquote, urlparse

import httpx
import pdfplumber

from mining_daily_agent.models.resources import ResourceCategory, ResourceItem, ResourceReport
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

_KEYWORD_RE: Final = re.compile(
    r"\b(?:mineral\s+resources?|measured|indicated|inferred)\b",
    re.IGNORECASE,
)
_CATEGORY_RE: Final = re.compile(r"\b(?P<category>measured|indicated|inferred)\b", re.IGNORECASE)
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


class PdfFetchError(RuntimeError):
    """PDF 不可用：下载失败，或响应根本不是 PDF。"""


def _http_get(url: str) -> httpx.Response:
    """发出单次 GET。

    这是本模块唯一的 HTTP 接缝：超时在此统一设置，测试也在这里替换。
    """
    return httpx.get(url, timeout=PDF_TIMEOUT_SECONDS, follow_redirects=True)


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


def _parse_grade(segment: str) -> tuple[float | None, str]:
    """取段内第一个「数字 + %/g/t」，返回 (品位, 单位)。"""
    match = _GRADE_RE.search(segment)
    if match is None:
        return None, ""
    return float(match.group("value")), match.group("unit").lower()


def _find_commodity(*candidates: str) -> str:
    """在候选文本中按给定顺序找矿种符号，找不到返回 ``unknown``。"""
    for text in candidates:
        match = _COMMODITY_RE.search(text)
        if match is not None:
            return match.group("commodity")
    return UNKNOWN_COMMODITY


def _items_from_block(block: str) -> list[ResourceItem]:
    """在一个文本块内按类别切段，逐段抽取吨位与品位。

    每个类别关键词开启一段，段的范围是它到下一个类别关键词之间；段内取
    第一个吨位与第一个品位。这同时兼容
    ``Indicated: 214 Mt @ 1.15% Li2O`` 的整句，
    以及表格里 ``Indicated  214 Mt  1.15%`` 的逐行形态。
    """
    matches = list(_CATEGORY_RE.finditer(block))
    items: list[ResourceItem] = []
    for index, match in enumerate(matches):
        segment_end = matches[index + 1].start() if index + 1 < len(matches) else len(block)
        segment = block[match.end() : segment_end]

        tonnage_match = _TONNAGE_RE.search(segment)
        if tonnage_match is None:
            # 有类别关键词但没有可识别吨位：多半是表头或叙述句，跳过。
            continue

        grade, grade_unit = _parse_grade(segment)
        items.append(
            ResourceItem(
                category=ResourceCategory(match.group("category").capitalize()),
                commodity=_find_commodity(segment, block),
                tonnage_t=_tonnage_to_tonnes(
                    tonnage_match.group("value"), tonnage_match.group("unit")
                ),
                grade=grade,
                grade_unit=grade_unit,
            )
        )
    return items


def parse_resource_text(text: str) -> tuple[list[ResourceItem], list[str]]:
    """从 PDF 文本中抽取资源量条目与溯源片段。

    Returns:
        ``(条目列表, 命中关键词的原文块列表)``。抽不到条目时第一个列表为空，
        第二个列表仍会给出候选原文。
    """
    items: list[ResourceItem] = []
    snippets: list[str] = []
    for block in _blocks(text):
        if not _KEYWORD_RE.search(block):
            continue
        snippets.append(_truncate(block))
        items.extend(_items_from_block(block))
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
        )
