"""环境变量的集中读取与校验。

业务代码必须经由本模块获取配置，不要直接调用 ``os.getenv`` 或 ``load_dotenv``
（见 CLAUDE.md「配置与安全」）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

DEFAULT_ENV_FILE = Path(".env")
DEFAULT_NEWS_DAYS = 1
REQUIRED_VARS: tuple[str, ...] = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")


class ConfigError(RuntimeError):
    """环境变量缺失或取值非法。"""


@dataclass(frozen=True, slots=True)
class Config:
    """已校验的运行时配置。"""

    llm_api_key: str
    llm_base_url: str
    llm_model: str
    news_days_default: int


def _collect_missing(names: tuple[str, ...]) -> list[str]:
    """收集取值缺失（未设置或仅空白）的变量名，保持传入顺序。"""
    return [name for name in names if not os.getenv(name, "").strip()]


def _validate_base_url(value: str) -> str:
    """校验 base_url 使用 http(s) 协议。"""
    if not value.startswith(("http://", "https://")):
        msg = f"环境变量 LLM_BASE_URL 必须以 http:// 或 https:// 开头，当前值为 {value!r}。"
        raise ConfigError(msg)
    return value


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
    path = DEFAULT_ENV_FILE if env_file is None else env_file
    if path.is_file():
        load_dotenv(dotenv_path=path, override=False)
    else:
        logger.debug("未找到环境文件 %s，仅使用进程环境变量。", path)

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
