"""人工验收入口：真实拉起三个 MCP server，列出连接池汇总到的工具。

用法::

    uv run python scripts/verify_pool.py

本文件用 ``print`` 而非 ``logging``：它的产出**就是**给终端看的报告，不是需要被
采集的服务日志。CLAUDE.md 的「禁止 print」针对库与 server 代码，CLI 报告是另一个
职责，故 ``pyproject.toml`` 对 ``scripts/**`` 放行了 T201。退出码非零表示验收不通过。
"""

from __future__ import annotations

import asyncio
import sys

from mining_daily_agent.client import McpConnectionPool

#: 三个 server 合起来应暴露的工具数。
EXPECTED_TOOL_COUNT = 5
#: 连接的 server 数。
EXPECTED_SERVER_COUNT = 3


async def main() -> int:
    """连接三个 server、打印工具清单，返回进程退出码。"""
    pool = await McpConnectionPool.create()
    try:
        connected = pool.connected_servers
        joined = ", ".join(connected) or "（无）"
        print(f"已连接 {len(connected)}/{EXPECTED_SERVER_COUNT} 个 server：{joined}")
        for name, reason in pool.failed_servers.items():
            print(f"  连接失败：{name} -> {reason}")

        tools = await pool.list_tools()
        print()
        print(f"汇总到 {len(tools)} 个工具：")
        for tool in tools:
            headline = tool.description.splitlines()[0] if tool.description else ""
            print(f"  {tool.name:<20} [{tool.server:<5}] {headline}")

        problems: list[str] = []
        if len(connected) != EXPECTED_SERVER_COUNT:
            problems.append(f"预期连上 {EXPECTED_SERVER_COUNT} 个 server，实际 {len(connected)} 个")
        if len(tools) != EXPECTED_TOOL_COUNT:
            problems.append(f"预期 {EXPECTED_TOOL_COUNT} 个工具，实际 {len(tools)} 个")

        print()
        if problems:
            for problem in problems:
                print(f"验收不通过：{problem}", file=sys.stderr)
            return 1
        print(f"验收通过：{len(connected)} 个 server、{len(tools)} 个工具。")
        return 0
    finally:
        await pool.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
