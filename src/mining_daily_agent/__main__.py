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
import io
import logging
import sys

from mining_daily_agent.agent import run_daily_brief

#: 不传 topic 时的默认主题。
DEFAULT_TOPIC = "Pilbara 锂矿"


def force_utf8_stdio() -> None:
    """把 stdout / stderr 切到 UTF-8，保证任何终端编码下打印都不会崩。

    Windows 中文控制台默认 **GBK**，而简报正文里带着资源报告原文照抄下来的字符——
    U+2011（非断行连字符，``In‑situ``）、U+2019（右单引号，``Fog’s Block``）、
    以及各种破折号。GBK 码表里没有 U+2011，``print`` 会抛 ``UnicodeEncodeError``：
    **文件已经落盘、退出码却是非 0**，Windows 上的评审者会直接当成运行失败。

    ``errors="replace"`` 是第二道保险：万一某台机器的控制台连 UTF-8 都写不出去
    （比如被重定向到只认 ASCII 的管道），也只是把个别字符打成 ``?``，不会让整条流程失败。

    只处理 ``io.TextIOWrapper``：pytest 的捕获对象等替身不是它，不该被这里改写。
    """
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


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
    # 放在最前面：logging 也可能往 stderr 写非 GBK 字符，日志必须和正文一样安全。
    force_utf8_stdio()
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
