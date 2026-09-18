"""PDF 资源报告数据源：真实解析实现 + mock 降级实现。"""

from __future__ import annotations

import logging

from mining_daily_agent.providers.pdf.base import PdfProvider
from mining_daily_agent.providers.pdf.mock import MockPdfProvider
from mining_daily_agent.providers.pdf.parser import PdfResourceProvider

logger = logging.getLogger(__name__)

__all__ = [
    "MockPdfProvider",
    "PdfProvider",
    "PdfResourceProvider",
    "get_pdf_provider",
]


def get_pdf_provider() -> PdfProvider:
    """返回可用的 PDF 资源源：优先真实实现，任何异常都降级到 mock。

    这里只负责初始化阶段的降级；调用阶段的降级由 ``servers.pdf_server``
    负责，两层共同保证真实源失败时流程不中断（见 CLAUDE.md「可靠性」）。
    """
    try:
        return PdfResourceProvider()
    except Exception as exc:  # 降级兜底刻意捕获一切初始化失败，不限定异常类型
        logger.warning(
            "真实 PDF 源初始化失败，降级到 MockPdfProvider：provider=PdfResourceProvider "
            "error=%s: %s",
            type(exc).__name__,
            exc,
        )
        return MockPdfProvider()
