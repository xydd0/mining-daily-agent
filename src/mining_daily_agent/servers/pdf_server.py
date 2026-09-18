"""mineral-pdf-mcp：向 LLM 暴露矿产资源报告 PDF 的结构化抽取工具。

启动方式（stdio 传输）::

    uv run python -m mining_daily_agent.servers.pdf_server

工具的 docstring 会被 MCP 作为工具描述下发给 LLM，LLM 据此选择工具，
因此这里用英文撰写，并写清用途与每个参数（见 CLAUDE.md「MCP 约定」）。
"""

# 刻意**不**使用 `from __future__ import annotations`：MCP 注册工具时需要解析
# 真实的返回类型注解来生成 JSON schema，ResourceReport 必须在运行时可用。
import logging
import time
from typing import Final
from urllib.parse import urlparse

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mining_daily_agent.models.resources import ResourceReport
from mining_daily_agent.providers import net
from mining_daily_agent.providers.pdf import get_pdf_provider
from mining_daily_agent.providers.pdf.mock import MockPdfProvider

logger = logging.getLogger(__name__)

SERVER_NAME: Final = "mineral-pdf-mcp"

mcp = MCPServer(SERVER_NAME)


class InvalidPdfUrlError(ValueError, ToolError):
    """PDF URL 非法。

    刻意同时继承两个基类：

    - ``ValueError`` 让校验本身与 MCP 解耦，是常规的入参错误语义；
    - ``ToolError`` 是 MCP 唯一会**保留原始 message** 转发给调用方的异常类型。
      SDK 对普通异常只回一句 "Error executing tool xxx"，异常自身的文本留在
      服务端。不继承它，下面的提示就到不了 LLM 那里。
    """


def _validate_url(url: str) -> str:
    """校验 URL 是绝对 http(s) 地址，且不通向本机 / 私网 / 云元数据地址。

    地址来自检索结果，是完全不可信的外部输入——一个被牵着走的 URL 就能读到内网服务。
    DNS 解析那道检查放在下载路径（``net.get_capped``），入参校验不做网络调用。

    Raises:
        InvalidPdfUrlError: URL 非法（同时是 ValueError 与 ToolError）。
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        msg = (
            f"非法的 PDF URL：{url!r}。"
            "需要一个绝对的 http(s) 地址，例如 "
            "https://example.com/mineral-resource-report.pdf。"
        )
        raise InvalidPdfUrlError(msg)
    try:
        net.ensure_public_url(url)
    except net.UnsafeUrlError as exc:
        raise InvalidPdfUrlError(f"拒绝访问该 PDF URL：{exc}") from exc
    return url


def _extract_with_fallback(pdf_url: str) -> tuple[ResourceReport, bool]:
    """抽取资源量，返回 ``(报告, 是否降级)``。

    调用阶段刻意捕获一切异常并回退到 mock：真实源失败不得中断上层流程
    （见 CLAUDE.md「可靠性」）。注意解析不出条目并不算失败，真实实现会正常
    返回空 ``resources``，不会走到这里。
    """
    provider = get_pdf_provider()
    try:
        return provider.extract_resources(pdf_url), False
    except Exception as exc:  # 降级兜底：任何真实源故障都回退，不限定异常类型
        logger.warning(
            "PDF 解析失败，降级到 mock：server=%s tool=extract_resources "
            "provider=%s url=%s degraded=true error=%s: %s",
            SERVER_NAME,
            type(provider).__name__,
            pdf_url,
            type(exc).__name__,
            exc,
        )
        return MockPdfProvider().extract_resources(pdf_url), True


@mcp.tool()
def extract_resources(pdf_url: str) -> ResourceReport:
    """Extract Mineral Resource estimates from a mining project's PDF report.

    Use this when you have a link to a technical report, resource statement or
    ASX/TSX announcement PDF (JORC or NI 43-101 style) and need the resource
    table as structured data instead of prose. It returns tonnages already
    converted to metric tonnes and grades normalised to "%" or "g/t".

    Args:
        pdf_url: Absolute http(s) URL of the PDF to download and parse.

    Returns:
        A report with `project_name`, `source_url`, `fetched_at`, a list of
        `resources` (each with category, commodity, tonnage_t, grade,
        grade_unit) and `raw_snippets` holding the verbatim text blocks that
        matched resource keywords.

        Parsing is heuristic: if nothing could be extracted, `resources` comes
        back empty and `raw_snippets` still carries the candidate text so you
        can inspect it. A download failure is not an error either — the tool
        falls back to a built-in offline dataset and says so in the first
        `raw_snippets` entry.

    Raises:
        InvalidPdfUrlError: If `pdf_url` is not an absolute http(s) URL.
    """
    valid_url = _validate_url(pdf_url)
    started = time.perf_counter()
    report, degraded = _extract_with_fallback(valid_url)
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "工具调用完成：server=%s tool=extract_resources url=%s "
        "resources=%d snippets=%d degraded=%s duration_ms=%.1f",
        SERVER_NAME,
        valid_url,
        len(report.resources),
        len(report.raw_snippets),
        degraded,
        elapsed_ms,
    )
    return report


if __name__ == "__main__":
    mcp.run(transport="stdio")
