"""config.load_config 的校验行为。

全部通过 monkeypatch 与临时文件驱动，不触网，也不读取仓库里真实的 .env
（见 CLAUDE.md「测试」）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from mining_daily_agent.config import Config, ConfigError, load_config

ENV_KEYS = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "NEWS_DAYS_DEFAULT")
VALID_ENV = {
    "LLM_API_KEY": "test-key",
    "LLM_BASE_URL": "https://api.deepseek.com",
    "LLM_MODEL": "deepseek-chat",
}


@pytest.fixture
def clean_os_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """清空相关环境变量，并在测试结束后整体还原 os.environ。

    ``load_dotenv`` 会直接写入 ``os.environ``，monkeypatch 无法撤销这类新增项，
    因此这里显式快照并还原，避免污染后续用例。
    """
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


@pytest.fixture
def env_file(clean_os_env: None, tmp_path: Path) -> Path:
    """返回一个不存在的 .env 路径，确保不会读到仓库里真实的 .env。"""
    return tmp_path / "absent.env"


def _set(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_loads_valid_config(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, VALID_ENV)

    config = load_config(env_file=env_file)

    assert config == Config(
        llm_api_key="test-key",
        llm_base_url="https://api.deepseek.com",
        llm_model="deepseek-chat",
        news_days_default=1,
    )


def test_news_days_is_overridable(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, {**VALID_ENV, "NEWS_DAYS_DEFAULT": "7"})

    assert load_config(env_file=env_file).news_days_default == 7


def test_strips_surrounding_whitespace(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, {**VALID_ENV, "LLM_MODEL": "  deepseek-chat  "})

    assert load_config(env_file=env_file).llm_model == "deepseek-chat"


def test_reports_every_missing_var_at_once(env_file: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(env_file=env_file)

    message = str(excinfo.value)
    for key in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        assert key in message
    assert ".env.example" in message


def test_blank_value_counts_as_missing(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, {**VALID_ENV, "LLM_API_KEY": "   "})

    with pytest.raises(ConfigError, match="LLM_API_KEY"):
        load_config(env_file=env_file)


def test_rejects_base_url_without_scheme(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, {**VALID_ENV, "LLM_BASE_URL": "api.deepseek.com"})

    with pytest.raises(ConfigError, match="http"):
        load_config(env_file=env_file)


def test_rejects_non_integer_news_days(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, {**VALID_ENV, "NEWS_DAYS_DEFAULT": "many"})

    with pytest.raises(ConfigError, match="整数"):
        load_config(env_file=env_file)


def test_rejects_non_positive_news_days(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, {**VALID_ENV, "NEWS_DAYS_DEFAULT": "0"})

    with pytest.raises(ConfigError, match=">= 1"):
        load_config(env_file=env_file)


def test_reads_values_from_env_file(clean_os_env: None, tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "LLM_API_KEY=from-file\n"
        "LLM_BASE_URL=https://example.invalid\n"
        "LLM_MODEL=file-model\n"
        "NEWS_DAYS_DEFAULT=3\n",
        encoding="utf-8",
    )

    config = load_config(env_file=path)

    assert config.llm_api_key == "from-file"
    assert config.llm_model == "file-model"
    assert config.news_days_default == 3


def test_process_env_wins_over_env_file(
    clean_os_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / ".env"
    path.write_text("LLM_API_KEY=from-file\n", encoding="utf-8")
    _set(monkeypatch, {**VALID_ENV, "LLM_API_KEY": "from-process"})

    assert load_config(env_file=path).llm_api_key == "from-process"
