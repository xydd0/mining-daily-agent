"""MCP 连接池：并发连接多个 server，统一列出并调用工具。

**为什么每个 server 有一个长驻任务**：``stdio_client`` 内部用 anyio 任务组，
其 cancel scope 必须在**进入它的那个任务**里退出，跨任务关闭会抛
"Attempted to exit cancel scope in a different task"。因此这里不用
``AsyncExitStack`` 在调用方任务里进出，而是让每个 server 在自己的任务里
建立并持有会话（见 ``_ServerWorker``），``aclose()`` 只发停止信号再等它收尾。
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, Protocol

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, ListToolsResult

from mining_daily_agent.config import (
    McpClientConfig,
    McpServerSpec,
    load_mcp_client_config,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)


class McpPoolError(RuntimeError):
    """连接池层面的错误基类。"""


class UnknownServerError(McpPoolError):
    """请求的 server 名字不在配置里。"""


class ServerUnavailableError(McpPoolError):
    """该 server 存在，但当初没能连上。"""


class McpCallTimeoutError(McpPoolError):
    """工具调用超时。"""


class McpCallError(McpPoolError):
    """工具调用失败（超时以外的原因）。"""


class McpSession(Protocol):
    """连接池实际需要会话提供的能力。

    真实 ``ClientSession`` 与测试替身都满足它。把需求写成 Protocol 而不是直接
    依赖 ``ClientSession``，是为了让连接接缝 ``_open_session`` 可替换——测试因此
    不必真的拉起子进程，也不必去伪造 anyio 的读写流。
    """

    async def initialize(self) -> object:
        """完成 MCP 握手。"""
        raise NotImplementedError

    async def list_tools(self) -> ListToolsResult:
        """列出该 server 暴露的工具。"""
        raise NotImplementedError

    async def call_tool(self, name: str, arguments: dict[str, object] | None = None) -> object:
        """调用工具。

        返回类型写成 ``object``：SDK 的 ``call_tool`` 声明为联合类型，调用方必须
        自行收窄，这里不假装它一定是 ``CallToolResult``。
        """
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class McpTool:
    """某个 server 暴露的一个工具。"""

    server: str
    name: str
    description: str


async def _close_quietly(stack: AsyncExitStack, server_name: str) -> None:
    """关闭退出栈并忽略过程中的异常。

    连接本就失败、或已进入关闭流程，此时再抛异常只会盖住真正的错误。
    """
    try:
        await stack.aclose()
    except Exception as exc:  # 关闭失败不应影响其它 server 的收尾
        logger.warning(
            "关闭 MCP server 会话时出错（已忽略）：server=%s error=%s: %s",
            server_name,
            type(exc).__name__,
            exc,
        )


async def _open_session(spec: McpServerSpec) -> tuple[McpSession, AsyncExitStack]:
    """建立到某个 server 的会话。

    这是本模块**唯一的连接接缝**，测试在这里替换以避开真实子进程。退出栈与
    会话一起返回：它必须由进入它的那个任务关闭（anyio 约束，见模块 docstring）。

    Raises:
        Exception: 连接或握手失败；失败时退出栈已在内部关闭，不会泄漏。
    """
    stack = AsyncExitStack()
    try:
        params = StdioServerParameters(command=spec.command, args=list(spec.args))
        read, write = await stack.enter_async_context(stdio_client(params))
        session: McpSession = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
    except Exception:
        await _close_quietly(stack, spec.name)
        raise
    return session, stack


class _ServerWorker:
    """持有单个 server 会话的长驻工作协程。

    会话的建立与关闭都发生在 ``self._task`` 这一个任务里，以满足 anyio 对
    cancel scope 的同任务约束。
    """

    def __init__(self, spec: McpServerSpec) -> None:
        self.spec = spec
        self.session: McpSession | None = None
        self.failure: str | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """启动工作协程，并等它把连接结果定下来。"""
        self._task = asyncio.create_task(self._run())
        await self._ready.wait()

    async def stop(self) -> None:
        """发出停止信号并等任务收尾。"""
        self._stop.set()
        if self._task is None:
            return
        try:
            await self._task
        except Exception as exc:  # 收尾阶段的异常已在 _run 内记录，这里兜底
            logger.warning(
                "等待 MCP server 任务退出时出错（已忽略）：server=%s error=%s: %s",
                self.spec.name,
                type(exc).__name__,
                exc,
            )
        finally:
            # 会话已随退出栈关闭，清掉引用，使 connected_servers 不再把它算作可用。
            self.session = None

    async def _run(self) -> None:
        try:
            session, stack = await _open_session(self.spec)
        except Exception as exc:  # 单个 server 失败不能拖垮连接池
            self.failure = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "MCP server 连接失败，跳过：server=%s command=%s args=%s error=%s",
                self.spec.name,
                self.spec.command,
                list(self.spec.args),
                self.failure,
            )
            self._ready.set()
            return

        self.session = session
        self._ready.set()
        logger.info(
            "MCP server 已连接：server=%s command=%s args=%s",
            self.spec.name,
            self.spec.command,
            list(self.spec.args),
        )
        try:
            await self._stop.wait()
        finally:
            await _close_quietly(stack, self.spec.name)


class McpConnectionPool:
    """并发连接多个 MCP server 的连接池。

    任意一个 server 连接失败都不阻塞整体：失败的记 ``logging.warning``，
    其余 server 照常可用（见 CLAUDE.md「可靠性」）。
    """

    def __init__(self, config: McpClientConfig) -> None:
        self._config = config
        self._workers: list[_ServerWorker] = []
        self._sessions: dict[str, McpSession] = {}
        self._failures: dict[str, str] = {}
        self._closed = False

    @classmethod
    async def create(cls, config: McpClientConfig | None = None) -> McpConnectionPool:
        """建立连接池并并发连上全部 server。

        Args:
            config: 连接池配置；为 None 时从环境变量读取。

        Returns:
            已就绪的连接池。即使部分 server 连接失败也会正常返回。
        """
        pool = cls(config if config is not None else load_mcp_client_config())
        await pool._connect_all()
        return pool

    @property
    def connected_servers(self) -> tuple[str, ...]:
        """成功连上的 server 名，按配置顺序。"""
        return tuple(worker.spec.name for worker in self._workers if worker.session is not None)

    @property
    def failed_servers(self) -> Mapping[str, str]:
        """连接失败的 server 名到失败原因摘要。"""
        return dict(self._failures)

    async def _connect_all(self) -> None:
        """并发启动全部工作协程，汇总连接结果。"""
        self._workers = [_ServerWorker(spec) for spec in self._config.servers]
        await asyncio.gather(*(worker.start() for worker in self._workers))
        for worker in self._workers:
            if worker.session is not None:
                self._sessions[worker.spec.name] = worker.session
            else:
                self._failures[worker.spec.name] = worker.failure or "未知原因"

    async def list_tools(self) -> list[McpTool]:
        """汇总全部已连接 server 的工具。

        某个 server 列出工具失败时记 warning 并跳过，不影响其余 server。
        """
        names = list(self._sessions)
        results = await asyncio.gather(
            *(self._sessions[name].list_tools() for name in names),
            return_exceptions=True,
        )
        tools: list[McpTool] = []
        for name, result in zip(names, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "列出工具失败，跳过：server=%s error=%s: %s",
                    name,
                    type(result).__name__,
                    result,
                )
                continue
            tools.extend(
                McpTool(server=name, name=tool.name, description=tool.description or "")
                for tool in result.tools
            )
        logger.info("已汇总工具：count=%d servers=%s", len(tools), names)
        return tools

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: Mapping[str, object] | None = None,
    ) -> CallToolResult:
        """把工具调用路由到对应 server 的会话执行。

        Args:
            server_name: 连接池里的 server 名（news / pdf / price）。
            tool_name: 工具名。
            arguments: 工具入参。

        Returns:
            SDK 的调用结果。``is_error`` 为 True 表示工具自身执行失败——这属于
            正常返回而非本方法抛错，调用方需自行判断。

        Raises:
            UnknownServerError: server 名不在配置里。
            ServerUnavailableError: 该 server 当初没能连上。
            McpCallTimeoutError: 调用超时。
            McpCallError: 其它调用失败，或返回了非预期的结果类型。
        """
        session = self._sessions.get(server_name)
        if session is None:
            self._raise_unavailable(server_name)

        params = dict(arguments) if arguments is not None else None
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._config.call_timeout_seconds):
                result = await session.call_tool(tool_name, params)
        except TimeoutError as exc:
            msg = (
                f"调用 {server_name}.{tool_name} 超时"
                f"（超过 {self._config.call_timeout_seconds} 秒）。"
            )
            raise McpCallTimeoutError(msg) from exc
        except Exception as exc:
            msg = f"调用 {server_name}.{tool_name} 失败：{type(exc).__name__}: {exc}"
            raise McpCallError(msg) from exc

        if not isinstance(result, CallToolResult):
            # SDK 的 call_tool 返回联合类型，正常路径应始终是 CallToolResult。
            msg = f"调用 {server_name}.{tool_name} 返回了非预期的结果类型 {type(result).__name__}。"
            raise McpCallError(msg)

        logger.info(
            "工具调用完成：server=%s tool=%s is_error=%s duration_ms=%.1f",
            server_name,
            tool_name,
            result.is_error,
            (time.perf_counter() - started) * 1000,
        )
        return result

    def _raise_unavailable(self, server_name: str) -> NoReturn:
        """抛出不存在的 server 或连接失败的 server 对应的错误。"""
        if server_name in self._failures:
            msg = (
                f"MCP server {server_name!r} 未能建立连接：{self._failures[server_name]}；"
                f"当前可用：{list(self._sessions) or '（无）'}。"
            )
            raise ServerUnavailableError(msg)
        msg = f"未知的 MCP server {server_name!r}；当前可用：{list(self._sessions) or '（无）'}。"
        raise UnknownServerError(msg)

    async def aclose(self) -> None:
        """优雅关闭全部会话。重复调用无副作用。"""
        if self._closed:
            return
        self._closed = True
        results = await asyncio.gather(
            *(worker.stop() for worker in self._workers),
            return_exceptions=True,
        )
        for worker, result in zip(self._workers, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "关闭 MCP server 失败（已忽略）：server=%s error=%s: %s",
                    worker.spec.name,
                    type(result).__name__,
                    result,
                )
        closed = sorted(self._sessions)
        self._sessions.clear()
        logger.info("MCP 连接池已关闭：servers=%s", closed)
