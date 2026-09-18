"""LangGraph 编排：planner → fetch_data → analyze → synthesize → render。

线性流，没有分支与循环：每一步只依赖上一步写进状态的字段。
"""

from __future__ import annotations

import logging
from itertools import pairwise
from typing import TYPE_CHECKING, Final, Protocol

from langgraph.graph import END, StateGraph

from mining_daily_agent.agent.nodes import (
    AgentError,
    ToolCaller,
    analyze,
    default_plan,
    fetch_data,
    planner,
    render,
    set_pool,
    synthesize,
)
from mining_daily_agent.agent.state import BriefState
from mining_daily_agent.client import McpConnectionPool

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)

#: 节点顺序，也是图的边的顺序。
NODE_SEQUENCE: Final[tuple[str, ...]] = (
    "planner",
    "fetch_data",
    "analyze",
    "synthesize",
    "render",
)


class PoolHandle(ToolCaller, Protocol):
    """连接池对编排层的最小契约：能调工具，也能关闭。"""

    async def aclose(self) -> None:
        """关闭全部会话。"""
        raise NotImplementedError


def _create_pool() -> Awaitable[PoolHandle]:
    """建立连接池。

    单独抽成函数是为了给测试留一个接缝——直接 patch ``McpConnectionPool.create``
    会因为返回的是测试替身（不是子类）而被 mypy 拒绝。
    """
    return McpConnectionPool.create()


def build_graph() -> CompiledStateGraph[BriefState, None, BriefState, BriefState]:
    """构建并编译线性流程图。"""
    graph = StateGraph(BriefState)
    graph.add_node("planner", planner)
    graph.add_node("fetch_data", fetch_data)
    graph.add_node("analyze", analyze)
    graph.add_node("synthesize", synthesize)
    graph.add_node("render", render)

    graph.set_entry_point(NODE_SEQUENCE[0])
    for current_node, next_node in pairwise(NODE_SEQUENCE):
        graph.add_edge(current_node, next_node)
    graph.add_edge(NODE_SEQUENCE[-1], END)
    return graph.compile()


def initial_state(topic: str) -> BriefState:
    """构造流程的初始状态。

    ``plan`` 先放一份默认计划占位：TypedDict 是全量的，而 planner 无论如何都会覆盖
    它——即使 planner 节点因故没跑，fetch_data 也拿得到可用的计划而不是 KeyError。
    """
    return BriefState(
        topic=topic,
        plan=default_plan(topic),
        news=[],
        article=None,
        resource_report=None,
        price_trend=None,
        risk_notes=[],
        highlights=[],
        markdown="",
        citations=[],
    )


async def _run_pipeline(topic: str, pool: ToolCaller) -> str:
    """把连接池注入节点后跑完整张图。"""
    set_pool(pool)
    try:
        result = await build_graph().ainvoke(initial_state(topic))
    finally:
        set_pool(None)

    document = result["markdown"]
    if not isinstance(document, str):
        msg = "render 节点未产出 Markdown 正文。"
        raise AgentError(msg)
    return document


async def run_daily_brief(topic: str, pool: ToolCaller | None = None) -> str:
    """跑完整条流程，返回最终 Markdown 简报。

    Args:
        topic: 简报主题。
        pool: 可选的连接池。为 None 时自建并在结束时关闭（CLI 路径）；传入时
            生命周期由调用方负责，本函数不会关闭它，便于测试与嵌入其它调用方。

    Returns:
        含正文与「来源」小节的 Markdown。

    Raises:
        AgentError: render 节点没有产出正文。
    """
    if pool is not None:
        return await _run_pipeline(topic, pool)

    handle = await _create_pool()
    try:
        return await _run_pipeline(topic, handle)
    finally:
        await handle.aclose()
