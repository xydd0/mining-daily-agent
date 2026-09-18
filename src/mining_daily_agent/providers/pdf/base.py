"""PDF 资源报告数据源接口。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from mining_daily_agent.models.resources import ResourceReport


@runtime_checkable
class PdfProvider(Protocol):
    """PDF 资源报告数据源协议。

    真实实现与 mock 实现都必须满足该接口，以便真实源失败时可以无缝降级。
    """

    def extract_resources(self, pdf_url: str) -> ResourceReport:
        """下载并解析指定 PDF，返回结构化的资源量报告。

        Args:
            pdf_url: PDF 的绝对 http(s) URL。

        Returns:
            解析结果；解析不到条目时 ``resources`` 为空列表而非报错。
        """
        raise NotImplementedError
