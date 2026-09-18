"""数据源实现。

每个数据源都必须同时提供真实实现与 mock 降级实现（见 CLAUDE.md「可靠性」）。
"""

from __future__ import annotations

from typing import Final

#: 抓取外部站点时携带的浏览器 User-Agent。
#:
#: 不少站点对非浏览器 UA 会返回 403（实测：mining.com 的文章页），带上它才能取到内容。
#: 放在 providers 包级而不是各 provider 里，避免同一个字符串在多处重复定义、改一处漏一处。
BROWSER_USER_AGENT: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

__all__ = ["BROWSER_USER_AGENT"]
