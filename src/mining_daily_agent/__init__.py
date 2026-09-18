"""mining-daily-agent 包入口。"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def main() -> None:
    """命令行入口点。当前为占位实现，待接入采集与编排流程。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logger.info("mining-daily-agent 已启动（当前为占位实现）")
