"""LLM 客户端构造。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final, Protocol

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from mining_daily_agent.config import Config, load_config

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage

logger = logging.getLogger(__name__)

#: 单次请求超时（秒）。生成整篇简报比单轮问答慢，留足余量。
DEFAULT_TIMEOUT_SECONDS: Final = 120.0
#: 低温度：简报要求事实一致、少发挥。
DEFAULT_TEMPERATURE: Final = 0.3


class BriefLLM(Protocol):
    """节点实际用到的 LLM 能力。

    只声明这一小块，是为了让测试能替换成假实现：直接把假类赋给返回
    ``ChatOpenAI`` 的工厂会被 mypy 拒绝（不是子类），硬塞就得 ``# type: ignore``。
    """

    async def ainvoke(self, input: str) -> BaseMessage:  # noqa: A002 — 必须与 SDK 同名
        """给一段提示词，拿回一条消息。

        参数名必须是 ``input``：LangChain 的 ``ainvoke`` 就用这个名字，改成 ``prompt``
        会让通过 Protocol 发起的关键字调用在真实对象上失败。
        """
        raise NotImplementedError


def build_chat_openai(config: Config) -> ChatOpenAI:
    """构造真实的 ChatOpenAI 客户端。

    字段名用 langchain 的别名（``model`` / ``api_key`` / ``base_url`` / ``timeout``）。
    别名一旦失效，``base_url`` 会被静默丢掉、请求打到 api.openai.com 并失败，
    因此 tests 里有断言 ``openai_api_base`` 等取值的接线用例把它钉住。
    """
    return ChatOpenAI(
        model=config.llm_model,
        # 用 SecretStr 包一层：密钥不会被 repr / 日志带出去。
        api_key=SecretStr(config.llm_api_key),
        base_url=config.llm_base_url,
        temperature=DEFAULT_TEMPERATURE,
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )


def build_llm() -> BriefLLM:
    """按 config.py 里的环境变量构造 LLM。

    后端是 DeepSeek 的 OpenAI 兼容端点，``base_url`` 必须显式传入，否则会指向
    api.openai.com（见 CLAUDE.md「环境变量契约」）。
    """
    return build_chat_openai(load_config())
