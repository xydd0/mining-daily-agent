"""CLI 入口：生成一份矿业每日简报。

用法::

    uv run python -m mining_daily_agent "给我生成一份关于 Pilbara 锂矿的今日简报"

本文件用 ``print`` 而非 ``logging``：CLI 的产出**就是**给终端看的简报正文，
不是需要被采集的服务日志。CLAUDE.md 的「禁止 print」针对库与 server 代码，
故 ``pyproject.toml`` 对 ``__main__.py`` 放行了 T201。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from mining_daily_agent.agent import run_daily_brief

#: 不传 topic 时的默认主题。
DEFAULT_TOPIC = "Pilbara 锂矿"


def build_parser() -> argparse.ArgumentParser:
    """构造参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="mining-daily-agent",
        description="生成一份矿业每日简报（新闻 + 资源量 + 价格）",
    )
    parser.add_argument(
        "topic",
        nargs="?",
        default=DEFAULT_TOPIC,
        help=f"简报主题（默认：{DEFAULT_TOPIC}）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """解析参数、跑流程、把简报打到终端。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    args = build_parser().parse_args(argv)
    topic = str(args.topic)

    document = asyncio.run(run_daily_brief(topic))
    print(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
