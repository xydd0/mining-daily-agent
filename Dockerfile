# syntax=docker/dockerfile:1
#
# 多阶段构建：builder 装依赖并构建项目，runtime 只带 venv。
#
# **为什么镜像必须自包含**：三个 MCP server 不是独立服务，而是由 client 以 stdio
# 子进程拉起的（config.load_mcp_client_config 默认走 `sys.executable -m <module>`）。
# 它们必须与 agent 同处一个镜像、能被同一个解释器 import 到，不可能拆成别的容器。
#
# 容器是**一次性任务**：跑完输出简报即退出，没有常驻进程、没有端口。

# ---------- 构建阶段 ----------
FROM python:3.12-slim AS builder

# 从官方镜像取 uv。钉到与本机一致的版本，避免依赖解析行为漂移
# （本 action 没有大版本标签，只能钉完整版本号，理由同 CI 里的 setup-uv）。
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 依赖层：只拷清单，源码变动不会让这一层失效。
COPY pyproject.toml uv.lock ./
# --no-install-project 让这一步不需要源码——否则此时 src/ 尚未拷入，构建会直接失败。
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# 源码层：改代码只会让这一层及其之后失效，依赖层仍命中缓存。
COPY README.md ./
COPY src ./src
# --no-editable：把项目真正装进 site-packages，runtime 层因此不再依赖源码路径，
# 也就不必把 src/ 一并拷过去。
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---------- 运行阶段 ----------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 只带 venv。项目已装进 site-packages，测试与开发脚本都不进镜像。
COPY --from=builder /app/.venv /app/.venv

# 默认入口即项目 CLI（[project.scripts] 的 mining-daily-agent，
# 与 `python -m mining_daily_agent` 等价）。追加参数即可换主题：
#   docker compose run --rm agent "<主题>"
ENTRYPOINT ["mining-daily-agent"]
