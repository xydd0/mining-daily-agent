"""领域数据模型。"""

from __future__ import annotations

from mining_daily_agent.models.news import Article, NewsItem
from mining_daily_agent.models.resources import (
    ResourceCategory,
    ResourceItem,
    ResourceReport,
)

__all__ = [
    "Article",
    "NewsItem",
    "ResourceCategory",
    "ResourceItem",
    "ResourceReport",
]
