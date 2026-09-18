"""mineral-pdf-mcp 的抽取行为测试。

网络层全部被替换，PDF 夹具用 reportlab 在内存里现生成，不触网、也不往仓库
写二进制文件（见 CLAUDE.md「测试」）。
"""

from __future__ import annotations

import io
import logging
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from mining_daily_agent.models.resources import ResourceCategory, ResourceItem
from mining_daily_agent.providers import BROWSER_USER_AGENT
from mining_daily_agent.providers import pdf as pdf_pkg
from mining_daily_agent.providers.pdf import mock as pdf_mock
from mining_daily_agent.providers.pdf import parser as pdf_parser
from mining_daily_agent.providers.pdf.mock import MockPdfProvider, build_mock_report
from mining_daily_agent.providers.pdf.parser import PdfFetchError, PdfResourceProvider
from mining_daily_agent.servers import pdf_server

REPORT_URL = "https://example.com/pilgangoora-resource.pdf"

#: 只用于下载相关测试：这些用例不解析内容，够长到能过魔数检查即可。
FAKE_PDF_PAYLOAD = b"%PDF-1.4\n% minimal placeholder\n"

#: 一份「有资源量」的 PDF 正文。
REPORT_LINES = [
    "Pilgangoora Lithium Project",
    "Mineral Resource Estimate",
    "",
    "Indicated Mineral Resource: 214 Mt at 1.15% Li2O",
    "Indicated Mineral Resource: 86 Mt at 1.08% Li2O",
    "Inferred Mineral Resource: 89 Mt at 1.05% Li2O",
    "Inferred Mineral Resource: 42 Mt at 1.02% Li2O",
]

#: 表头写单位、单元格只放数字——真实 NI 43-101 报告的主流表格写法。
TABLE_TEXT = (
    "Category    Tonnage (Mt)    Grade (% Li2O)\n"
    "Indicated   214             1.15\n"
    "Inferred    89              1.05\n"
)

#: 同一张表，但表头与数据行被空行切成了两个块。
TABLE_TEXT_SPLIT_BLOCKS = (
    "Table 1: Mineral Resource Estimate\n"
    "Category    Tonnage (Mt)    Grade (% Li2O)\n"
    "\n"
    "Indicated   214    1.15\n"
    "Inferred    89     1.05\n"
)

#: 供 PDF 链路测试用的表格型正文。
TABLE_LINES = [
    "Pilgangoora Lithium Project",
    "Table 1: Mineral Resource Estimate",
    "",
    "Category   Tonnage (Mt)   Grade (% Li2O)",
    "Indicated  214           1.15",
    "Inferred   89            1.05",
]

#: 一份「提到资源量但抽不出条目」的 PDF 正文。
NO_FIGURES_LINES = [
    "Pilgangoora Lithium Project",
    "This note discusses Mineral Resource estimation methodology only.",
    "An Indicated classification is being considered for the deposit.",
    "No tonnage or grade figures are given in this sample document.",
]


def _build_pdf(lines: list[str]) -> bytes:
    """用 reportlab 在内存里生成一份带文本层的 PDF。"""
    buffer = io.BytesIO()
    document = canvas.Canvas(buffer, pagesize=A4)
    text_object = document.beginText(50, 800)
    text_object.setFont("Helvetica", 10)
    for line in lines:
        text_object.textLine(line)
    document.drawText(text_object)
    document.showPage()
    document.save()
    return buffer.getvalue()


@pytest.fixture(scope="module")
def report_pdf() -> bytes:
    return _build_pdf(REPORT_LINES)


@pytest.fixture(scope="module")
def no_figures_pdf() -> bytes:
    return _build_pdf(NO_FIGURES_LINES)


def _pdf_response(
    url: str, payload: bytes, content_type: str = "application/pdf"
) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        content=payload,
        headers={"content-type": content_type},
        request=httpx.Request("GET", url),
    )


def _stub_get(
    payload: bytes, content_type: str = "application/pdf"
) -> Callable[[str], httpx.Response]:
    def _get(url: str) -> httpx.Response:
        return _pdf_response(url, payload, content_type)

    return _get


# --- 真实年报回归：量级必须合理 ---------------------------------------------

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
PLS_TABLE = FIXTURE_DIR / "pls_2025_mineral_resource_table.txt"


def test_case_insensitive_prose_is_not_mistaken_for_a_resource_table() -> None:
    """类别只认首字母大写——这是最大的一类误报来源。

    实测 Pilbara Minerals 2025 年报：87 条抽取里 80 条来自 "measured at fair value"、
    "where indicated in the Annual Report"、"measured against the Baseline" 这类句子，
    而资源表里的类别词**全部**是首字母大写。
    """
    text = (
        "Indicated 214 Mt at 1.15% Li2O\n"
        "Items are measured at fair value. Amounts are indicated in note 12.\n"
        "Performance is measured against the Baseline over a 3-year period.\n"
    )

    items, _ = pdf_parser.parse_resource_text(text)

    assert len(items) == 1, "小写的 measured / indicated 不应产生条目"
    assert items[0].category is ResourceCategory.INDICATED


def test_self_reported_total_is_read_from_the_table() -> None:
    """表里既有分块 Sub total（436 / 9）也有全矿 Total（445），应取最大者。

    这个值是交叉核对的基准：按类别逐行相加会把分块与总计算两遍。
    """
    total = pdf_parser.parse_self_reported_total(PLS_TABLE.read_text(encoding="utf-8"))

    assert total is not None
    assert total == pytest.approx(445e6), "应取全矿总计而非分块小计"

    items, _ = pdf_parser.parse_resource_text(PLS_TABLE.read_text(encoding="utf-8"))
    assert sum(item.tonnage_t for item in items) > total, (
        "逐条求和本就会超过自报合计——这正是需要交叉核对的原因"
    )


def test_self_reported_total_is_none_without_a_total_row() -> None:
    assert pdf_parser.parse_self_reported_total("Indicated 214 Mt at 1.15% Li2O") is None


def test_real_annual_report_table_parses_to_a_sane_magnitude() -> None:
    """用真实年报的资源表页做回归。

    fixture 是 Pilbara Minerals 2025 年报第 32 页 Table 5 的文本（从 "Table 5:" 到
    "Table 6:" 之间）。未修复前同一份文档能抽出 87 条、Measured 合计十亿吨级——
    全是会计正文误报；修复后应只剩表里的真实行，量级回到 1e8 吨量级。

    已知不足（**未被本测试覆盖，别当成已修**）：品位多不可靠（表把单位放在列头，
    段内第一个带 % 的数字常来自 "(≥0.2% Li2O)" 这类注脚）；Stockpiles 的 Inferred
    行没有数值，会误取到同块的 "Sub total" 数字。
    """
    items, snippets = pdf_parser.parse_resource_text(PLS_TABLE.read_text(encoding="utf-8"))

    # 1. 没有失控的误报：真实表只有 9 行（3 个分块 × 3 个类别）。
    assert len(items) <= 12, f"条目数失控：{len(items)}"
    assert snippets, "命中关键词的原文应留作溯源"

    # 2. 每一条的吨位都在可信量级内。
    for item in items:
        assert pdf_parser.TONNAGE_MIN_T <= item.tonnage_t <= pdf_parser.TONNAGE_MAX_T

    # 3. 表里的关键行都在：Pilgangoora 分块的三个类别。
    by_tonnage = {round(item.tonnage_t / 1e6, 1) for item in items}
    assert {19.0, 356.0, 70.0} <= by_tonnage, f"缺少合并行，实得 {sorted(by_tonnage)}"

    # 4. 合计回到合理量级——修复前是 1,068,508 Mt。
    total_mt = sum(item.tonnage_t for item in items) / 1e6
    assert total_mt < 1500, f"合计 {total_mt:.1f} Mt 明显偏高，疑似误报回归"


# --- 场景 1：正常解析出 Indicated / Inferred --------------------------------


def test_extracts_indicated_and_inferred(
    monkeypatch: pytest.MonkeyPatch, report_pdf: bytes
) -> None:
    monkeypatch.setattr(pdf_parser, "_http_get", _stub_get(report_pdf))

    report = PdfResourceProvider().extract_resources(REPORT_URL)

    indicated = [i for i in report.resources if i.category is ResourceCategory.INDICATED]
    inferred = [i for i in report.resources if i.category is ResourceCategory.INFERRED]
    assert len(indicated) == 2
    assert len(inferred) == 2
    assert indicated[0].tonnage_t == pytest.approx(214e6)
    assert indicated[0].grade == pytest.approx(1.15)
    assert indicated[0].grade_unit == "%"
    assert indicated[0].commodity == "Li2O"
    assert inferred[0].tonnage_t == pytest.approx(89e6)


def test_report_carries_provenance(monkeypatch: pytest.MonkeyPatch, report_pdf: bytes) -> None:
    monkeypatch.setattr(pdf_parser, "_http_get", _stub_get(report_pdf))

    report = PdfResourceProvider().extract_resources(REPORT_URL)

    assert report.source_url == REPORT_URL
    assert report.project_name == "Pilgangoora Lithium Project"
    assert report.raw_snippets, "命中关键词的原文应留作溯源"
    assert any("Indicated Mineral Resource" in snippet for snippet in report.raw_snippets)


@pytest.mark.parametrize(
    ("line", "expected_tonnes"),
    [
        ("Indicated 214 Mt at 1.15% Li2O", 214e6),
        ("Indicated 1,200 kt at 1.15% Li2O", 1_200_000.0),
        ("Indicated 214000 t at 1.15% Li2O", 214_000.0),
        ("Indicated 3.5 million tonnes at 1.15% Li2O", 3.5e6),
    ],
)
def test_tonnage_units_are_converted(line: str, expected_tonnes: float) -> None:
    items, _ = pdf_parser.parse_resource_text(line)

    assert items[0].tonnage_t == pytest.approx(expected_tonnes)


def test_grade_is_optional_for_an_item() -> None:
    items, _ = pdf_parser.parse_resource_text("Indicated 214 Mt")

    assert items[0].grade is None
    assert items[0].grade_unit == ""


# --- 表头单位推断（真实 NI 43-101 表格的主流写法）---------------------------


def test_header_units_are_applied_to_bare_numbers() -> None:
    items, _ = pdf_parser.parse_resource_text(TABLE_TEXT)

    indicated = [i for i in items if i.category is ResourceCategory.INDICATED]
    assert len(indicated) == 1
    assert indicated[0].tonnage_t == pytest.approx(214e6)
    assert indicated[0].grade == pytest.approx(1.15)
    assert indicated[0].grade_unit == "%"
    assert indicated[0].commodity == "Li2O"


def test_header_tonnage_unit_kt_is_applied() -> None:
    text = "Category  Tonnage (kt)  Grade (% Li2O)\nIndicated  1200  1.15\n"

    items, _ = pdf_parser.parse_resource_text(text)

    assert items[0].tonnage_t == pytest.approx(1_200_000.0)


def test_header_grade_unit_g_per_tonne_is_applied() -> None:
    text = "Category  Tonnage (Mt)  Grade (g/t Au)\nIndicated  12  1.85\n"

    items, _ = pdf_parser.parse_resource_text(text)

    assert items[0].grade == pytest.approx(1.85)
    assert items[0].grade_unit == "g/t"
    assert items[0].commodity == "Au"


def test_header_without_unit_yields_no_items() -> None:
    """表头没有单位时不做任何猜测，仍走空结果 + raw_snippets 路径。"""
    text = "Category  Tonnage  Grade\nIndicated  214  1.15\n"

    items, snippets = pdf_parser.parse_resource_text(text)

    assert items == []
    assert snippets, "抽不到条目时仍要给出候选原文"


def test_explicit_units_win_over_header_hints() -> None:
    text = "Category  Tonnage (kt)  Grade (% Li2O)\nIndicated  214 Mt at 0.9% Li2O\n"

    items, _ = pdf_parser.parse_resource_text(text)

    assert items[0].tonnage_t == pytest.approx(214e6), "应采信行内显式的 Mt，而非表头的 kt"
    assert items[0].grade == pytest.approx(0.9)


def test_header_hints_carry_across_blocks() -> None:
    """PDF 文本提取常把表头与数据行切成两块，单位提示需要沿用下去。"""
    items, _ = pdf_parser.parse_resource_text(TABLE_TEXT_SPLIT_BLOCKS)

    indicated = [i for i in items if i.category is ResourceCategory.INDICATED]
    assert len(indicated) == 1
    assert indicated[0].tonnage_t == pytest.approx(214e6)
    assert indicated[0].grade == pytest.approx(1.15)


def test_extracts_from_table_style_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    """表格写法经由真实 PDF 文本提取链路也要能抽出条目。"""
    payload = _build_pdf(TABLE_LINES)
    monkeypatch.setattr(pdf_parser, "_http_get", _stub_get(payload))

    report = PdfResourceProvider().extract_resources(REPORT_URL)

    indicated = [i for i in report.resources if i.category is ResourceCategory.INDICATED]
    assert indicated, "表格型报告应能抽出 Indicated 条目"
    assert indicated[0].tonnage_t == pytest.approx(214e6)
    assert indicated[0].grade == pytest.approx(1.15)


def test_resource_item_rejects_grade_without_unit() -> None:
    with pytest.raises(ValidationError, match="grade_unit"):
        ResourceItem(
            category=ResourceCategory.INDICATED,
            commodity="Li2O",
            tonnage_t=1.0,
            grade=1.1,
        )


# --- 场景 2：下载失败自动降级 mock -------------------------------------------


def test_factory_falls_back_to_mock_when_init_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class _BrokenProvider(PdfResourceProvider):
        def __init__(self) -> None:
            raise RuntimeError("cannot initialise parser")

    monkeypatch.setattr(pdf_pkg, "PdfResourceProvider", _BrokenProvider)

    with caplog.at_level(logging.WARNING):
        provider = pdf_pkg.get_pdf_provider()

    assert isinstance(provider, MockPdfProvider)
    assert "降级" in caplog.text
    assert "cannot initialise parser" in caplog.text


def test_tool_falls_back_to_mock_when_download_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(url: str) -> httpx.Response:
        raise httpx.ConnectError("dns failure")

    monkeypatch.setattr(pdf_parser, "_http_get", _boom)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    report = pdf_server.extract_resources(pdf_url=REPORT_URL)

    assert report.resources, "降级后应返回内置数据而不是抛异常"
    assert report.project_name == pdf_mock.PROJECT_NAME
    assert "降级" in report.raw_snippets[0], "降级数据必须自带声明，避免被当作真实披露值"
    assert report.source_url == REPORT_URL


def test_tool_logs_degraded_event(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom(url: str) -> httpx.Response:
        raise httpx.ConnectError("dns failure")

    monkeypatch.setattr(pdf_parser, "_http_get", _boom)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    with caplog.at_level(logging.INFO):
        pdf_server.extract_resources(pdf_url=REPORT_URL)

    assert "degraded=true" in caplog.text
    assert "server=mineral-pdf-mcp" in caplog.text
    assert "tool=extract_resources" in caplog.text


# --- 场景 3：解析不到资源时返回空列表且不抛异常 ------------------------------


def test_returns_empty_resources_without_raising(
    monkeypatch: pytest.MonkeyPatch, no_figures_pdf: bytes
) -> None:
    monkeypatch.setattr(pdf_parser, "_http_get", _stub_get(no_figures_pdf))

    report = PdfResourceProvider().extract_resources(REPORT_URL)

    assert report.resources == []
    assert report.raw_snippets, "抽不到条目时仍要给出候选原文供上层判断"


def test_empty_parse_result_is_not_treated_as_failure(
    monkeypatch: pytest.MonkeyPatch, no_figures_pdf: bytes
) -> None:
    """解析不出条目 ≠ 下载失败：不应触发 mock 降级，否则会掩盖真实文档内容。"""
    monkeypatch.setattr(pdf_parser, "_http_get", _stub_get(no_figures_pdf))

    report = pdf_server.extract_resources(pdf_url=REPORT_URL)

    assert report.resources == []
    assert report.project_name != pdf_mock.PROJECT_NAME
    assert all("降级" not in snippet for snippet in report.raw_snippets)


# --- 场景 4：非法 URL 报错 ---------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    ["", "not-a-url", "example.com/report.pdf", "ftp://example.com/a.pdf", "file:///tmp/a.pdf"],
)
def test_invalid_url_raises(bad_url: str) -> None:
    with pytest.raises(ValueError, match="URL"):
        pdf_server.extract_resources(pdf_url=bad_url)


def test_invalid_url_error_is_a_tool_error_so_message_reaches_the_llm() -> None:
    """MCP 只原样转发 ToolError 的 message；其余异常会被替换成通用文案。"""
    with pytest.raises(ToolError) as excinfo:
        pdf_server.extract_resources(pdf_url="not-a-url")

    assert "not-a-url" in str(excinfo.value)


# --- 下载层：超时、重试、Content-Type 校验 ----------------------------------


def test_http_get_sets_explicit_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def _fake(url: str, **kwargs: object) -> httpx.Response:
        seen.update(kwargs)
        return _pdf_response(url, FAKE_PDF_PAYLOAD)

    monkeypatch.setattr(httpx, "get", _fake)

    pdf_parser._http_get(REPORT_URL)

    assert seen["timeout"] == pdf_parser.PDF_TIMEOUT_SECONDS == 30.0


def test_http_get_sends_a_browser_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """不少站点对非浏览器 UA 直接 403（实测 mining.com 的文章页），必须带上。"""
    seen: dict[str, object] = {}

    def _fake(url: str, **kwargs: object) -> httpx.Response:
        seen.update(kwargs)
        return _pdf_response(url, FAKE_PDF_PAYLOAD)

    monkeypatch.setattr(httpx, "get", _fake)

    pdf_parser._http_get(REPORT_URL)

    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["User-Agent"] == BROWSER_USER_AGENT
    assert "Mozilla" in str(headers["User-Agent"])


def test_user_agent_is_defined_in_exactly_one_place() -> None:
    """同一个 UA 字符串在多处各写一份，迟早改一处漏一处。"""
    sources = (Path(__file__).resolve().parents[1] / "src").rglob("*.py")

    defining = [
        path.name
        for path in sources
        if "Mozilla/5.0 (Windows NT 10.0" in path.read_text(encoding="utf-8")
    ]

    assert defining == ["__init__.py"], "UA 只应定义在 providers/__init__.py"


def test_download_retries_with_exponential_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    def _get(url: str) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.ConnectError("flaky network")
        return _pdf_response(url, FAKE_PDF_PAYLOAD)

    monkeypatch.setattr(pdf_parser, "_http_get", _get)
    monkeypatch.setattr(time, "sleep", sleeps.append)

    assert pdf_parser._download_pdf(REPORT_URL) == FAKE_PDF_PAYLOAD
    assert len(attempts) == 3
    assert sleeps == [0.5, 1.0], "退避应为 0.5s、1.0s"


def test_download_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    def _get(url: str) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectError("down")

    monkeypatch.setattr(pdf_parser, "_http_get", _get)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    with pytest.raises(PdfFetchError, match="连续失败"):
        pdf_parser._download_pdf(REPORT_URL)

    assert len(attempts) == pdf_parser.MAX_ATTEMPTS == 3


def test_rejects_response_that_is_not_a_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_parser, "_http_get", _stub_get(b"<html>not a pdf</html>", "text/html"))

    with pytest.raises(PdfFetchError, match="不是 PDF"):
        pdf_parser._download_pdf(REPORT_URL)


def test_content_type_mismatch_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """Content-Type 不对是确定性错误，重试没有意义。"""
    calls: list[int] = []

    def _get(url: str) -> httpx.Response:
        calls.append(1)
        return _pdf_response(url, b"<html>nope</html>", "text/html")

    monkeypatch.setattr(pdf_parser, "_http_get", _get)

    with pytest.raises(PdfFetchError):
        pdf_parser._download_pdf(REPORT_URL)

    assert len(calls) == 1


def test_accepts_generic_content_type_when_magic_bytes_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pdf_parser, "_http_get", _stub_get(FAKE_PDF_PAYLOAD, "application/octet-stream")
    )

    assert pdf_parser._download_pdf(REPORT_URL) == FAKE_PDF_PAYLOAD


def test_project_name_falls_back_to_url_stem() -> None:
    assert pdf_parser._guess_project_name("", "https://example.com/pilgangoora-report.pdf") == (
        "pilgangoora-report"
    )


# --- MockPdfProvider --------------------------------------------------------


def test_mock_has_at_least_two_indicated_and_two_inferred() -> None:
    report = build_mock_report(REPORT_URL)

    indicated = [i for i in report.resources if i.category is ResourceCategory.INDICATED]
    inferred = [i for i in report.resources if i.category is ResourceCategory.INFERRED]
    assert len(indicated) >= 2
    assert len(inferred) >= 2


def test_mock_indicated_matches_pilgangoora_magnitude() -> None:
    """Indicated 合计应在 2-3 亿吨量级，品位 1.0-1.2% Li2O。"""
    report = build_mock_report(REPORT_URL)

    indicated = [i for i in report.resources if i.category is ResourceCategory.INDICATED]
    total_tonnes = sum(item.tonnage_t for item in indicated)
    assert 2e8 <= total_tonnes <= 3e8
    assert all(item.grade is not None and 1.0 <= item.grade <= 1.2 for item in indicated)
    assert all(item.grade_unit == "%" for item in indicated)
    assert all(item.commodity == "Li2O" for item in indicated)


def test_mock_report_is_marked_as_degraded() -> None:
    """合成吨位必须带结构化标记，否则简报会把它当真实资源量呈现。"""
    report = build_mock_report(REPORT_URL)

    assert report.degraded


def test_real_parse_is_not_marked_as_degraded(
    monkeypatch: pytest.MonkeyPatch, report_pdf: bytes
) -> None:
    monkeypatch.setattr(pdf_parser, "_http_get", _stub_get(report_pdf))

    report = PdfResourceProvider().extract_resources(REPORT_URL)

    assert not report.degraded, "真实解析结果不能被误标为降级"


def test_mock_provider_never_raises_for_any_url() -> None:
    report = MockPdfProvider().extract_resources("https://example.com/whatever.pdf")

    assert report.resources
    assert report.source_url == "https://example.com/whatever.pdf"
