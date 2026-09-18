"""人工验收入口：真实拉起三个 MCP server，列出连接池汇总到的工具。

用法::

    uv run python scripts/verify_pool.py

这是**给人看的报告**，所以输出走 ``logging`` 直接写到 stdout（``format="%(message)s"``），
而不是 ``print``——既满足 CLAUDE.md「统一使用标准库 logging，禁止 print」，
又保持纯文本输出。退出码非零表示验收不通过。
"""

from __future__ import annotations

import asyncio
import logging
import sys

from mining_daily_agent.client import McpConnectionPool

logger = logging.getLogger("verify_pool")

#: 三个 server 合起来应暴露的工具数。
EXPECTED_TOOL_COUNT = 5
#: 连接的 server 数。
EXPECTED_SERVER_COUNT = 3


async def main() -> int:
    """连接三个 server、打印工具清单，返回进程退出码。"""
    pool = await McpConnectionPool.create()
    try:
        connected = pool.connected_servers
        logger.info(
            "已连接 %d/%d 个 server：%s",
            len(connected),
            EXPECTED_SERVER_COUNT,
            ", ".join(connected) or "（无）",
        )
        for name, reason in pool.failed_servers.items():
            logger.info("  连接失败：%s -> %s", name, reason)

        tools = await pool.list_tools()
        logger.info("")
        logger.info("汇总到 %d 个工具：", len(tools))
        for tool in tools:
            headline = tool.description.splitlines()[0] if tool.description else ""
            logger.info("  %-20s [%-5s] %s", tool.name, tool.server, headline)

        problems: list[str] = []
        if len(connected) != EXPECTED_SERVER_COUNT:
            problems.append(f"预期连上 {EXPECTED_SERVER_COUNT} 个 server，实际 {len(connected)} 个")
        if len(tools) != EXPECTED_TOOL_COUNT:
            problems.append(f"预期 {EXPECTED_TOOL_COUNT} 个工具，实际 {len(tools)} 个")

        if problems:
            for problem in problems:
                logger.error("验收不通过：%s", problem)
            return 1
        logger.info("")
        logger.info("验收通过：%d 个 server、%d 个工具。", len(connected), len(tools))
        return 0
    finally:
        await pool.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    raise SystemExit(asyncio.run(main()))
