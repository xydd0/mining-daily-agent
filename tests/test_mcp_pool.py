"""McpConnectionPool 与 MCP client 配置的行为测试。

不拉起真实子进程：连接接缝 ``pool._open_session`` 被替换成假会话。假会话用真实
的 SDK 结果类型（``ListToolsResult`` / ``CallToolResult`` / ``Tool``）返回数据，
因此测的是「拿到真实 SDK 对象之后怎么处理」，而不是自造的鸭子类型
（见 CLAUDE.md「测试」）。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from mining_daily_agent.client import pool as pool_module
from mining_daily_agent.client.pool import (
    McpCallError,
    McpCallTimeoutError,
    McpConnectionPool,
    McpSession,
    ServerUnavailableError,
    UnknownServerError,
)
from mining_daily_agent.config import (
    DEFAULT_MCP_CALL_TIMEOUT_SECONDS,
    ConfigError,
    McpClientConfig,
    McpServerSpec,
    load_mcp_client_config,
)

MODULES = {
    "news": "mining_daily_agent.servers.news_server",
    "pdf": "mining_daily_agent.servers.pdf_server",
    "price": "mining_daily_agent.servers.price_server",
}
#: 三个 server 各自暴露的工具。
SERVER_TOOLS = {
    "news": ("search", "fetch_article"),
    "pdf": ("extract_resources",),
    "price": ("get_price", "get_trend"),
}
ALL_TOOL_NAMES = ["extract_resources", "fetch_article", "get_price", "get_trend", "search"]


@dataclass
class _FakeSpec:
    """单个假 server 的行为开关。"""

    name: str
    fail_connect: bool = False
    fail_list: bool = False
    fail_call: bool = False
    fail_close: bool = False
    hang: bool = False
    error_result: bool = False


@dataclass
class _Registry:
    """记录测试期间的连接、调用与关闭轨迹。"""

    specs: dict[str, _FakeSpec]
    opened: list[str] = field(default_factory=list)
    initialized: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    calls: list[tuple[str, str, dict[str, object]]] = field(default_factory=list)
    barrier: asyncio.Event = field(default_factory=asyncio.Event)
    barrier_timeout: float = 2.0
    #: 栅栏放行所需的连接数；0 表示本次不使用栅栏。由 _open_pool 按实际连接的
    #: server 数设置——若写死成注册表里的总数，只连一个 server 的用例会永远等不到。
    expected_opens: int = 0


class _RecordingStack(AsyncExitStack):
    """记录自己被关闭的退出栈，用来验证 aclose() 真的关掉了每个会话。"""

    def __init__(self, registry: _Registry, name: str) -> None:
        super().__init__()
        self._registry = registry
        self._name = name

    async def aclose(self) -> None:
        # 先记录再判断：模拟「关闭动作已发起但收尾失败」。
        self._registry.closed.append(self._name)
        if self._registry.specs[self._name].fail_close:
            msg = f"{self._name} close failed"
            raise RuntimeError(msg)
        await super().aclose()


class _FakeSession:
    """假冒的 MCP 会话，满足 pool.McpSession。"""

    def __init__(self, registry: _Registry, name: str) -> None:
        self._registry = registry
        self._name = name

    async def initialize(self) -> object:
        self._registry.initialized.append(self._name)
        return object()

    async def list_tools(self) -> ListToolsResult:
        if self._registry.specs[self._name].fail_list:
            msg = f"{self._name} list_tools failed"
            raise RuntimeError(msg)
        return ListToolsResult(
            tools=[
                Tool(
                    name=name,
                    description=f"{name} on {self._name}",
                    input_schema={"type": "object"},
                )
                for name in SERVER_TOOLS[self._name]
            ]
        )

    async def call_tool(self, name: str, arguments: dict[str, object] | None = None) -> object:
        spec = self._registry.specs[self._name]
        self._registry.calls.append((self._name, name, dict(arguments or {})))
        if spec.fail_call:
            msg = f"{self._name}.{name} failed"
            raise RuntimeError(msg)
        if spec.hang:
            await asyncio.sleep(30)
        return CallToolResult(
            content=[TextContent(type="text", text=f"{self._name}:{name}")],
            is_error=spec.error_result,
        )


def _install(monkeypatch: pytest.MonkeyPatch, registry: _Registry) -> None:
    """把连接接缝替换成假实现。"""

    async def _fake_open(spec: McpServerSpec) -> tuple[McpSession, AsyncExitStack]:
        fake_spec = registry.specs[spec.name]
        if fake_spec.fail_connect:
            msg = f"cannot spawn {spec.name}"
            raise RuntimeError(msg)
        registry.opened.append(spec.name)
        if registry.expected_opens:
            # 栅栏：所有应当连上的 server 都进入后才放行。若连接是串行的，第一个
            # 会一直等到超时——这正是「并发连接」这条测试要证伪的情形。
            if len(registry.opened) >= registry.expected_opens:
                registry.barrier.set()
            await asyncio.wait_for(registry.barrier.wait(), timeout=registry.barrier_timeout)
        session = _FakeSession(registry, spec.name)
        # 真实 _open_session 会在返回前完成握手，假实现同样要走这一步。
        await session.initialize()
        return session, _RecordingStack(registry, spec.name)

    monkeypatch.setattr(pool_module, "_open_session", _fake_open)


def _config(*names: str, timeout: float = 5.0) -> McpClientConfig:
    return McpClientConfig(
        servers=tuple(
            McpServerSpec(name=name, command="python", args=("-m", MODULES[name])) for name in names
        ),
        call_timeout_seconds=timeout,
    )


async def _open_pool(
    monkeypatch: pytest.MonkeyPatch, registry: _Registry, *names: str, timeout: float = 5.0
) -> McpConnectionPool:
    """装好假连接接缝，并建立只含 ``names`` 的连接池。"""
    registry.expected_opens = sum(1 for name in names if not registry.specs[name].fail_connect)
    _install(monkeypatch, registry)
    return await McpConnectionPool.create(_config(*names, timeout=timeout))


@pytest.fixture
def registry() -> _Registry:
    return _Registry(specs={name: _FakeSpec(name=name) for name in MODULES})


# --- 并发连接 ---------------------------------------------------------------


async def test_connects_all_servers_concurrently(
    monkeypatch: pytest.MonkeyPatch, registry: _Registry
) -> None:
    """三个 server 必须并发连接：串行的话第一个会卡在栅栏上直到超时，全部连不上。"""
    pool = await _open_pool(monkeypatch, registry, "news", "pdf", "price")
    try:
        assert pool.connected_servers == ("news", "pdf", "price")
        assert pool.failed_servers == {}
        assert sorted(registry.initialized) == ["news", "pdf", "price"]
    finally:
        await pool.aclose()


async def test_open_session_receives_the_configured_command(
    monkeypatch: pytest.MonkeyPatch, registry: _Registry
) -> None:
    """启动方式必须来自配置，而不是硬编码在连接池里。"""
    seen: list[tuple[str, tuple[str, ...]]] = []

    async def _fake_open(spec: McpServerSpec) -> tuple[McpSession, AsyncExitStack]:
        seen.append((spec.command, spec.args))
        return _FakeSession(registry, spec.name), _RecordingStack(registry, spec.name)

    monkeypatch.setattr(pool_module, "_open_session", _fake_open)
    config = McpClientConfig(
        servers=(McpServerSpec(name="news", command="uv", args=("run", "python", "-m", "x")),),
        call_timeout_seconds=5.0,
    )

    pool = await McpConnectionPool.create(config)
    try:
        assert seen == [("uv", ("run", "python", "-m", "x"))]
    finally:
        await pool.aclose()


# --- 部分失败不阻塞 ---------------------------------------------------------


async def test_partial_failure_does_not_block_the_rest(
    monkeypatch: pytest.MonkeyPatch, registry: _Registry, caplog: pytest.LogCaptureFixture
) -> None:
    registry.specs["pdf"].fail_connect = True

    with caplog.at_level(logging.WARNING):
        pool = await _open_pool(monkeypatch, registry, "news", "pdf", "price")
    try:
        assert pool.connected_servers == ("news", "price")
        assert "pdf" in pool.failed_servers
        assert "cannot spawn pdf" in pool.failed_servers["pdf"]
        assert "连接失败，跳过" in caplog.text

        tools = await pool.list_tools()
        assert {tool.server for tool in tools} == {"news", "price"}
    finally:
        await pool.aclose()


async def test_all_servers_failing_still_returns_a_usable_pool(
    monkeypatch: pytest.MonkeyPatch, registry: _Registry
) -> None:
    for spec in registry.specs.values():
        spec.fail_connect = True

    pool = await _open_pool(monkeypatch, registry, "news", "pdf", "price")
    try:
        assert pool.connected_servers == ()
        assert sorted(pool.failed_servers) == ["news", "pdf", "price"]
        assert await pool.list_tools() == []
    finally:
        await pool.aclose()


async def test_list_tools_skips_a_failing_server(
    monkeypatch: pytest.MonkeyPatch, registry: _Registry, caplog: pytest.LogCaptureFixture
) -> None:
    registry.specs["pdf"].fail_list = True
    pool = await _open_pool(monkeypatch, registry, "news", "pdf", "price")
    try:
        with caplog.at_level(logging.WARNING):
            tools = await pool.list_tools()
    finally:
        await pool.aclose()

    assert {tool.server for tool in tools} == {"news", "price"}
    assert "列出工具失败，跳过" in caplog.text


# --- list_tools 汇总 --------------------------------------------------------


async def test_list_tools_aggregates_all_five_tools(
    monkeypatch: pytest.MonkeyPatch, registry: _Registry
) -> None:
    pool = await _open_pool(monkeypatch, registry, "news", "pdf", "price")
    try:
        tools = await pool.list_tools()
    finally:
        await pool.aclose()

    assert sorted(tool.name for tool in tools) == ALL_TOOL_NAMES
    assert {tool.server for tool in tools} == {"news", "pdf", "price"}
    assert all(tool.description for tool in tools)


# --- call_tool 路由 ---------------------------------------------------------


async def test_call_tool_routes_to_the_right_server(
    monkeypatch: pytest.MonkeyPatch, registry: _Registry
) -> None:
    pool = await _open_pool(monkeypatch, registry, "news", "pdf", "price")
    try:
        result = await pool.call_tool("price", "get_price", {"commodity": "copper"})
    finally:
        await pool.aclose()

    assert registry.calls == [("price", "get_price", {"commodity": "copper"})]
    assert not result.is_error
    assert [block.text for block in result.content if isinstance(block, TextContent)] == [
        "price:get_price"
    ]


async def test_call_tool_without_arguments(
    monkeypatch: pytest.MonkeyPatch, registry: _Registry
) -> None:
    pool = await _open_pool(monkeypatch, registry, "pdf")
    try:
        await pool.call_tool("pdf", "extract_resources")
    finally:
        await pool.aclose()

    assert registry.calls == [("pdf", "extract_resources", {})]


async def test_tool_level_error_result_is_returned_not_raised(
    monkeypatch: pytest.MonkeyPatch, registry: _Registry
) -> None:
    """工具自身报错（is_error=True）属正常返回，不应被包装成连接池异常。"""
    registry.specs["news"].error_result = True
    pool = await _open_pool(monkeypatch, registry, "news")
    try:
        result = await pool.call_tool("news", "search", {"query": "x"})
    finally:
        await pool.aclose()

    assert result.is_error, "判定权留给调用方，连接池不代它抛错"


# --- 错误包装 ---------------------------------------------------------------


async def test_unknown_server_is_rejected(
    monkeypatch: pytest.MonkeyPatch, registry: _Registry
) -> None:
    pool = await _open_pool(monkeypatch, registry, "news")
    try:
        with pytest.raises(UnknownServerError, match="未知的 MCP server"):
            await pool.call_tool("ghost", "anything")
    finally:
        await pool.aclose()


async def test_unavailable_server_reports_the_connect_failure(
    monkeypatch: pytest.MonkeyPatch, registry: _Registry
) -> None:
    registry.specs["pdf"].fail_connect = True
    pool = await _open_pool(monkeypatch, registry, "news", "pdf")
    try:
        with pytest.raises(ServerUnavailableError, match="cannot spawn pdf"):
            await pool.call_tool("pdf", "extract_resources")
    finally:
        await pool.aclose()


async def test_call_failure_is_wrapped(
    monkeypatch: pytest.MonkeyPatch, registry: _Registry
) -> None:
    registry.specs["news"].fail_call = True
    pool = await _open_pool(monkeypatch, registry, "news")
    try:
        with pytest.raises(McpCallError, match=r"news\.search failed"):
            await pool.call_tool("news", "search", {"query": "x"})
    finally:
        await pool.aclose()


async def test_call_timeout_is_reported(
    monkeypatch: pytest.MonkeyPatch, registry: _Registry
) -> None:
    registry.specs["news"].hang = True
    pool = await _open_pool(monkeypatch, registry, "news", timeout=0.05)
    try:
        with pytest.raises(McpCallTimeoutError, match="超时"):
            await pool.call_tool("news", "search", {"query": "x"})
    finally:
        await pool.aclose()


# --- aclose -----------------------------------------------------------------


async def test_aclose_closes_every_session(
    monkeypatch: pytest.MonkeyPatch, registry: _Registry
) -> None:
    pool = await _open_pool(monkeypatch, registry, "news", "pdf", "price")

    await pool.aclose()

    assert sorted(registry.closed) == ["news", "pdf", "price"]
    assert pool.connected_servers == (), "关闭后不应再报告可用会话"


async def test_aclose_is_idempotent(monkeypatch: pytest.MonkeyPatch, registry: _Registry) -> None:
    pool = await _open_pool(monkeypatch, registry, "news", "pdf", "price")

    await pool.aclose()
    await pool.aclose()

    assert sorted(registry.closed) == ["news", "pdf", "price"], "重复关闭不应重复执行"


async def test_aclose_still_closes_others_when_one_close_fails(
    monkeypatch: pytest.MonkeyPatch, registry: _Registry, caplog: pytest.LogCaptureFixture
) -> None:
    registry.specs["news"].fail_close = True
    pool = await _open_pool(monkeypatch, registry, "news", "pdf", "price")

    with caplog.at_level(logging.WARNING):
        await pool.aclose()

    assert sorted(registry.closed) == ["news", "pdf", "price"], "一个关不掉不应影响其余"
    assert "关闭 MCP server 会话时出错" in caplog.text


# --- 配置 -------------------------------------------------------------------


def _clear_mcp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "MCP_SERVER_LAUNCHER",
        "MCP_SERVER_LAUNCHER_ARGS",
        "MCP_CALL_TIMEOUT_SECONDS",
        "MCP_NEWS_SERVER_MODULE",
        "MCP_PDF_SERVER_MODULE",
        "MCP_PRICE_SERVER_MODULE",
    ):
        monkeypatch.delenv(key, raising=False)


def test_mcp_config_defaults_to_the_three_project_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_mcp_env(monkeypatch)

    config = load_mcp_client_config(env_file=Path("absent.env"))

    assert [spec.name for spec in config.servers] == ["news", "pdf", "price"]
    assert [spec.args[-1] for spec in config.servers] == list(MODULES.values())
    assert all(spec.args[-2] == "-m" for spec in config.servers)
    assert config.call_timeout_seconds == DEFAULT_MCP_CALL_TIMEOUT_SECONDS == 120.0


def test_mcp_config_is_overridable_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_mcp_env(monkeypatch)
    monkeypatch.setenv("MCP_SERVER_LAUNCHER", "uv")
    monkeypatch.setenv("MCP_SERVER_LAUNCHER_ARGS", "run python")
    monkeypatch.setenv("MCP_NEWS_SERVER_MODULE", "custom.news")
    monkeypatch.setenv("MCP_CALL_TIMEOUT_SECONDS", "5")

    config = load_mcp_client_config(env_file=Path("absent.env"))

    news = config.servers[0]
    assert news.command == "uv"
    assert news.args == ("run", "python", "-m", "custom.news")
    assert config.call_timeout_seconds == 5.0


def test_mcp_timeout_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_mcp_env(monkeypatch)
    monkeypatch.setenv("MCP_CALL_TIMEOUT_SECONDS", "0")

    with pytest.raises(ConfigError, match="必须 > 0"):
        load_mcp_client_config(env_file=Path("absent.env"))
