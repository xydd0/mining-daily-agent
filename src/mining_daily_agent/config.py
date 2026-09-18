"""环境变量的集中读取与校验。

业务代码必须经由本模块获取配置，不要直接调用 ``os.getenv`` 或 ``load_dotenv``
（见 CLAUDE.md「配置与安全」）。
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

DEFAULT_ENV_FILE = Path(".env")
DEFAULT_NEWS_DAYS = 1
REQUIRED_VARS: tuple[str, ...] = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")

#: 三个 MCP server 的名称与默认模块路径。名称同时是连接池里的路由键。
DEFAULT_MCP_SERVERS: Final[tuple[tuple[str, str], ...]] = (
    ("news", "mining_daily_agent.servers.news_server"),
    ("pdf", "mining_daily_agent.servers.pdf_server"),
    ("price", "mining_daily_agent.servers.price_server"),
)
#: 单次工具调用的默认超时秒数。
DEFAULT_MCP_CALL_TIMEOUT_SECONDS: Final = 30.0
#: 简报输出目录的默认值，可经 REPORTS_DIR 覆盖。
DEFAULT_REPORTS_DIR: Final = Path("reports")

#: 兜底年报的内置默认值：Pilbara Minerals 2025 年报（含官方 Mineral Resource 表）。
#: 实测可下载（15.6 MB / 186 页），且 pdfplumber 能从中抽出资源量条目。
#: 注意这是**公司专项**地址，见 default_report_url() 的说明。
DEFAULT_REPORT_URL_BUILTIN: Final = (
    "https://www.pls.com/storage/announcements/"
    "2025-annual-report-incorporating-appendix-4e-2025-08-25.pdf"
)


class ConfigError(RuntimeError):
    """环境变量缺失或取值非法。"""


@dataclass(frozen=True, slots=True)
class Config:
    """已校验的运行时配置。"""

    llm_api_key: str
    llm_base_url: str
    llm_model: str
    news_days_default: int


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    """一个 MCP server 的启动方式。"""

    name: str
    command: str
    args: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class McpClientConfig:
    """MCP client 连接池的配置。"""

    servers: tuple[McpServerSpec, ...]
    call_timeout_seconds: float


def _collect_missing(names: tuple[str, ...]) -> list[str]:
    """收集取值缺失（未设置或仅空白）的变量名，保持传入顺序。"""
    return [name for name in names if not os.getenv(name, "").strip()]


def _validate_base_url(value: str) -> str:
    """校验 base_url 使用 http(s) 协议。"""
    if not value.startswith(("http://", "https://")):
        msg = f"环境变量 LLM_BASE_URL 必须以 http:// 或 https:// 开头，当前值为 {value!r}。"
        raise ConfigError(msg)
    return value


def _load_env_file(env_file: Path | None = None) -> Path:
    """加载 .env（若存在），返回实际使用的路径。

    ``load_dotenv`` 用 ``override=False``：进程环境变量优先于文件，便于测试与 CI 覆盖。
    """
    path = DEFAULT_ENV_FILE if env_file is None else env_file
    if path.is_file():
        load_dotenv(dotenv_path=path, override=False)
    else:
        logger.debug("未找到环境文件 %s，仅使用进程环境变量。", path)
    return path


def _positive_int(name: str, default: int) -> int:
    """读取正整数环境变量；未设置时返回 default。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        msg = f"环境变量 {name} 必须是整数，当前值为 {raw!r}。"
        raise ConfigError(msg) from exc
    if value < 1:
        msg = f"环境变量 {name} 必须 >= 1，当前值为 {value}。"
        raise ConfigError(msg)
    return value


def _positive_float(name: str, default: float) -> float:
    """读取正浮点环境变量；未设置时返回 default。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        msg = f"环境变量 {name} 必须是数字，当前值为 {raw!r}。"
        raise ConfigError(msg) from exc
    if value <= 0:
        msg = f"环境变量 {name} 必须 > 0，当前值为 {value}。"
        raise ConfigError(msg)
    return value


def _mcp_launcher() -> tuple[str, tuple[str, ...]]:
    """解析启动器可执行文件与附加参数。

    - ``MCP_SERVER_LAUNCHER``：启动器，默认当前解释器 ``sys.executable``。连接池本身
      就跑在项目 venv 里，直接用该解释器 ``-m`` 启动最省事，不必再经 ``uv run`` 解析一层。
    - ``MCP_SERVER_LAUNCHER_ARGS``：插在 ``-m`` 之前的参数，按空白切分。例如设
      ``MCP_SERVER_LAUNCHER=uv``、``MCP_SERVER_LAUNCHER_ARGS="run python"``，
      即改回 ``uv run python -m ...`` 的启动方式。
    """
    command = os.getenv("MCP_SERVER_LAUNCHER", "").strip() or sys.executable
    return command, tuple(os.getenv("MCP_SERVER_LAUNCHER_ARGS", "").split())


def load_config(env_file: Path | None = None) -> Config:
    """读取 .env 与进程环境变量，返回校验后的配置。

    进程环境变量优先于 ``.env`` 中的同名项（``load_dotenv(override=False)``），
    因此测试与 CI 可以用环境变量覆盖文件取值。

    Args:
        env_file: 要加载的 .env 路径；为 None 时使用当前目录下的 ``.env``。
            文件不存在不会报错，此时仅使用进程环境变量。

    Returns:
        校验通过的 Config 实例。

    Raises:
        ConfigError: 必填项缺失或取值非法。所有缺失项会一次性列出，
            而不是遇到第一个就中断。
    """
    path = _load_env_file(env_file)

    missing = _collect_missing(REQUIRED_VARS)
    if missing:
        msg = (
            f"缺少必需的环境变量：{'、'.join(missing)}。"
            f"请复制 .env.example 为 {path} 并填入真实值。"
        )
        raise ConfigError(msg)

    config = Config(
        llm_api_key=os.environ["LLM_API_KEY"].strip(),
        llm_base_url=_validate_base_url(os.environ["LLM_BASE_URL"].strip()),
        llm_model=os.environ["LLM_MODEL"].strip(),
        news_days_default=_positive_int("NEWS_DAYS_DEFAULT", DEFAULT_NEWS_DAYS),
    )
    # 只记录非敏感字段：绝不输出 llm_api_key。
    logger.debug(
        "配置加载完成：llm_base_url=%s llm_model=%s news_days_default=%d",
        config.llm_base_url,
        config.llm_model,
        config.news_days_default,
    )
    return config


def reports_dir() -> Path:
    """简报输出目录；经 ``REPORTS_DIR`` 覆盖，默认 ``reports/``。

    做成可配置的另一个原因：测试要把它指到临时目录，否则跑一次测试就会往仓库里
    写文件。
    """
    raw = os.getenv("REPORTS_DIR", "").strip()
    return Path(raw) if raw else DEFAULT_REPORTS_DIR


def default_report_url() -> str:
    """兜底年报 URL：环境变量 ``DEFAULT_REPORT_URL`` 优先，未配置时用内置默认值。

    内置值让「新闻里没有 PDF 线索」时仍有一条**确定性**的取数路径——否则每次挑到
    哪条新闻全看运气（实测关键词匹配经常命中的是普通新闻页，传给 PDF 工具必然解析
    失败并降级成 mock）。

    ⚠️ 内置默认值是 **Pilbara Minerals 专项**（该公司的官方年报）。非锂矿主题应当
    自行设置 ``DEFAULT_REPORT_URL``，或让取数计划把 ``needs_pdf`` 设为 false——
    否则会拿一份锂矿年报去回答铜、镍之类的问题。
    """
    configured = os.getenv("DEFAULT_REPORT_URL", "").strip()
    return configured or DEFAULT_REPORT_URL_BUILTIN


def load_mcp_client_config(env_file: Path | None = None) -> McpClientConfig:
    """读取 MCP client 连接池的配置。

    三个 server 的模块路径经 ``MCP_<NAME>_SERVER_MODULE`` 覆盖
    （``NAME`` 取 ``NEWS`` / ``PDF`` / ``PRICE``），启动器经 ``MCP_SERVER_LAUNCHER``
    与 ``MCP_SERVER_LAUNCHER_ARGS`` 覆盖，调用超时经 ``MCP_CALL_TIMEOUT_SECONDS`` 覆盖。
    全部有默认值，因此不配置也能直接跑。

    Args:
        env_file: 要加载的 .env 路径；为 None 时使用当前目录下的 ``.env``。

    Returns:
        连接池配置。

    Raises:
        ConfigError: 超时等取值非法。
    """
    _load_env_file(env_file)
    command, extra_args = _mcp_launcher()
    servers = tuple(
        McpServerSpec(
            name=name,
            command=command,
            args=(
                *extra_args,
                "-m",
                os.getenv(f"MCP_{name.upper()}_SERVER_MODULE", "").strip() or module,
            ),
        )
        for name, module in DEFAULT_MCP_SERVERS
    )
    return McpClientConfig(
        servers=servers,
        call_timeout_seconds=_positive_float(
            "MCP_CALL_TIMEOUT_SECONDS", DEFAULT_MCP_CALL_TIMEOUT_SECONDS
        ),
    )
