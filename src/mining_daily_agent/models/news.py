"""新闻领域模型。

这些模型同时充当 MCP 工具的输入/输出 schema：``Field(description=...)``
会进入工具描述，供 LLM 理解字段含义（见 CLAUDE.md「MCP 约定」）。
"""

# 刻意**不**使用 `from __future__ import annotations`：pydantic 需要在运行时
# 求值这些注解来完成字段校验，datetime 必须真实可导入。
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class NewsItem(BaseModel):
    """一条新闻的摘要信息。"""

    model_config = ConfigDict(frozen=True)

    title: str = Field(description="Headline of the news item.")
    url: str = Field(description="Absolute URL of the original article.")
    source: str = Field(description="Name of the publishing outlet or feed.")
    published_at: datetime = Field(description="Publication time, timezone-aware (ISO-8601).")
    summary: str = Field(
        description="Plain-text summary with HTML stripped, truncated to 500 characters."
    )


class Article(BaseModel):
    """一篇新闻的正文。"""

    model_config = ConfigDict(frozen=True)

    title: str = Field(description="Headline of the article.")
    url: str = Field(description="Absolute URL of the article.")
    source: str = Field(description="Name of the publishing outlet.")
    published_at: datetime = Field(description="Publication time, timezone-aware (ISO-8601).")
    text: str = Field(description="Extracted plain-text body of the article.")
