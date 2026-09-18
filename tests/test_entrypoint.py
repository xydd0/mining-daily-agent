"""入口点接线测试。

两条命令必须等价：

    uv run python -m mining_daily_agent "<主题>"
    uv run mining-daily-agent "<主题>"

前者走 ``__main__``，后者的目标写在 ``[project.scripts]`` 里——一旦两者分叉
（比如脚本又指回某个占位实现），这里会立刻失败。
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import mining_daily_agent
from mining_daily_agent import __main__ as cli

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _script_target() -> str:
    with PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    return str(data["project"]["scripts"]["mining-daily-agent"])


def test_console_script_points_at_the_cli_module() -> None:
    assert _script_target() == "mining_daily_agent.__main__:main"


def test_console_script_target_resolves_to_the_cli_main() -> None:
    """``包.模块:函数`` 必须真的解析到 CLI 的 main，而不是同名占位。"""
    module_path, _, attribute = _script_target().partition(":")

    module = importlib.import_module(module_path)

    assert getattr(module, attribute) is cli.main


def test_package_root_has_no_second_entry_point() -> None:
    """包根不再保留自己的 main。

    原先 ``[project.scripts]`` 指向 ``mining_daily_agent:main``——一个只打印问候语的
    占位实现，而真正的 CLI 在 ``__main__``。留着一个不被引用的入口只会让人走错。
    """
    assert not hasattr(mining_daily_agent, "main")


def test_cli_main_is_directly_invocable() -> None:
    """两条命令最终都调 ``__main__.main(argv)``，因此它必须可注入 argv。"""
    assert callable(cli.main)
    assert cli.build_parser().prog == "mining-daily-agent"
