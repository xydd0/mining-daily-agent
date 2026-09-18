# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 当前状态

新闻与 PDF 两条链路已打通（`models` → `providers` → `servers`），`client` 仍是空白：

- `src/mining_daily_agent/config.py` — 环境变量集中读取与校验，缺失时抛 `ConfigError`
- `src/mining_daily_agent/models/news.py` — `NewsItem` / `Article`（pydantic）
- `src/mining_daily_agent/models/resources.py` — `ResourceCategory` / `ResourceItem` / `ResourceReport`
- `src/mining_daily_agent/providers/news/` — `base.py`（`NewsProvider` Protocol）、`rss.py`（真实实现，3 个 RSS 源）、`mock.py`（降级实现，8 条内置数据）
- `src/mining_daily_agent/providers/pdf/` — `base.py`（`PdfProvider` Protocol）、`parser.py`（pdfplumber + 正则抽取）、`mock.py`（Pilgangoora 风格资源表）
- `src/mining_daily_agent/servers/news_server.py` — `mining-news-mcp`：`search` / `fetch_article`
- `src/mining_daily_agent/servers/pdf_server.py` — `mineral-pdf-mcp`：`extract_resources`
- `src/mining_daily_agent/__init__.py` 的 `main()` 仍是占位实现，待接入实际流程
- 「代码组织」中的 `client` 是**目标结构，尚未创建**
- **没有 CI**；`README.md` 仍为空文件

启动 MCP server（stdio）：

```bash
uv run python -m mining_daily_agent.servers.news_server
uv run python -m mining_daily_agent.servers.pdf_server
```

`pyproject.toml` 里的依赖声明的是**意图**，不是已实现的架构。在补全代码前，不要假设「代码组织」中列出的任何子包或模块已存在——请先 `ls src/mining_daily_agent/` 确认。

## 工具链约定

- **包管理只能用 `uv`**（存在 `uv.lock`，build-backend 为 `uv_build`）。不要用 pip / poetry / conda 直接操作环境。
- **Python 3.12**（`.python-version` 与 `pyproject.toml` 的 `requires-python = ">=3.12"` 一致）。注意系统默认 `python` 是 3.14，务必通过 `uv run` 调用，不要裸跑 `python`。
- 采用 **src 布局**：代码放 `src/mining_daily_agent/`，`uv_build` 要求保持该结构。子包划分见「代码组织」。
- 入口点：`mining_daily_agent:main`（对应 `pyproject.toml` 的 `[project.scripts]`）。
- **依赖卫生问题**：代码直接 `import pydantic`，但 `pydantic` **没有**在 `pyproject.toml` 中声明，目前完全依赖 `mcp` 的传递依赖。若上游哪天去掉它，import 会直接失败。建议补一条显式声明（需用户确认，此前被要求"不新增依赖"）。

## 代码组织

固定使用 src layout，子包职责划分如下（**目录尚未创建**）：

```
src/mining_daily_agent/
├── servers/     # 三个 MCP server
├── providers/   # 数据源（真实实现 + mock 降级实现）
├── client/      # LLM / 外部服务客户端
└── models/      # 数据模型
```

- **禁止在仓库根目录堆放 `.py` 文件**，所有代码归入上述子包。
- 每个 Python 包都必须有 `__init__.py`。

## 代码质量与类型

- **所有函数必须有完整类型注解**，包括参数与返回值。
- **禁止裸 `Any`**。确实无法静态确定类型时，用 `object` 配合类型收窄、或定义具体的 Protocol / 类型别名，而不是 `Any`。
- `ruff` 与 `mypy` 必须**零错误**。
- **不得用 `# type: ignore` 掩盖真实的类型问题**。应先修类型；仅当确属第三方 stub 缺陷时才允许 ignore，且必须写明原因与对应的上游 issue。

## 测试

- pytest 覆盖率**不低于 70%**。
- **所有外部网络调用在测试中必须被 mock**，测试不得依赖公网——不得真实请求 RSS、网页、PDF、LLM 等任何端点。
- 测试放在 `tests/`。覆盖率阈值配在 `[tool.coverage.report] fail_under`；`addopts` 已含 `--cov`，直接 `uv run pytest` 即会校验，不需要额外传参。

## 可靠性：超时、重试与降级

- **所有 HTTP 调用必须显式设置 `timeout`**，不得依赖库的默认无超时行为。
- 必须实现**指数退避重试，最多 3 次**。
- **所有数据源必须同时提供 mock 实现作为降级兜底**：真实源失败时自动回退到 mock 实现，**不得抛异常中断整体流程**。
- 降级不能静默发生，必须按「日志」规范记录。

## 日志

- 统一使用标准库 `logging`，**禁止 `print`**。
- 关键节点需记录结构化字段：**server 名、工具名、耗时、是否降级**。

## MCP 约定

- 三个 MCP server **默认使用 stdio 传输**（当前已实现 1 个）。
- 工具函数必须有清晰的 docstring，说明**用途与各参数含义**——LLM 依赖这些描述来选择工具，描述不清会直接导致工具被选错或漏选。

### mcp 2.x 实现要点（三条踩过的坑）

- **`FastMCP` 不存在**。装的是 mcp 2.x，类名已改为 `MCPServer`：
  `from mcp.server.mcpserver import MCPServer`。写 `from mcp.server.fastmcp import FastMCP` 会直接 ImportError。
- **定义工具函数/模型的模块不能加 `from __future__ import annotations`**。MCP 注册工具时要解析真实类型注解来生成 JSON schema，注解被延迟求值会导致解析失败；pydantic 同理。`servers/news_server.py` 与 `models/news.py` 都因此刻意省略了这行，并写了注释说明。
- **工具内故意抛的异常必须是 `ToolError`**（`mcp.server.mcpserver.exceptions`）。SDK 只原样转发 `ToolError` 的 message，其余异常一律被替换成 `Error executing tool xxx`，异常文本留在服务端。`InvalidArticleUrlError` 因此同时继承 `ValueError`（框架无关的入参语义）与 `ToolError`（消息可传递），不要"简化"成裸 `ValueError`——那会让提示信息静默消失。

## 提交规范

- 使用 **Conventional Commits**（`feat:` / `fix:` / `refactor:` / `docs:` / `test:` / `style:` / `chore:` 等）。
- **每次提交前必须依次通过以下四项，全绿才允许提交**：

```bash
uv run ruff check .
uv run ruff format --check
uv run mypy src
uv run pytest
```

`ruff format --check` 是后补的一项。原先三项**不覆盖格式**——`ruff check` 只管 lint，不看排版。曾因此把未格式化的代码提交进 `main`，事后才用 `style(news):` 补修。pre-commit 的 `ruff format` 钩子会自动改文件，但**用 `git commit` 绕过 pre-commit 时就没有这道网**，此时 `--check` 是唯一拦截点。

## 常用命令

```bash
uv sync                    # 安装/同步依赖
uv run mining-daily-agent  # 运行入口点（当前仅打印占位字符串）
uv add <package>           # 新增运行时依赖
uv add --dev <package>     # 新增开发依赖

# 提交前三件套，必须全绿（见「提交规范」）
uv run ruff check .
uv run mypy src
uv run pytest --cov=mining_daily_agent --cov-fail-under=70

uv run pytest tests/test_x.py::test_name -v   # 跑单个测试（tests/ 尚未创建）
uv run ruff format         # 格式化
```

`ruff` / `mypy` / `pytest` 的配置都在 `pyproject.toml`。其中 ruff 的 `ignore` **刻意关闭了 `RUF001`/`RUF002`/`RUF003`**——它们会把中文全角标点判为「易混淆字符」，对本项目纯属误报，不要重新开启。

`.pre-commit-config.yaml` 用的是 `language: system` + `uv run` 的本地钩子：复用项目自身的 uv 环境，避免 mypy 在隔离环境里看不到依赖而全量报 unresolved import。注意 **`--all-files` 在仓库还没有 commit 时会跳过全部钩子**（`git ls-files` 为空，显示 "no files to check"），要验证钩子得显式传路径：`uv run pre-commit run --files <路径...>`。

## 环境变量契约

配置通过 `python-dotenv` 从 `.env` 读取。已在 `.env` 中声明的变量：

| 变量 | 当前值 | 说明 |
|---|---|---|
| `LLM_API_KEY` | （见 `.env`） | LLM 密钥 |
| `LLM_BASE_URL` | `https://api.deepseek.com` | **OpenAI 兼容端点** |
| `LLM_MODEL` | `deepseek-chat` | 模型名 |
| `NEWS_DAYS_DEFAULT` | `1` | 默认回溯天数 |

**关键点**：虽然 LLM 依赖用的是 `langchain-openai`，但实际后端是 **DeepSeek**（经其 OpenAI 兼容接口）。配置 `ChatOpenAI` 时必须显式传入 `base_url=LLM_BASE_URL`，否则会指向 api.openai.com 而失败。不要假设这是 OpenAI 官方服务。

## 配置与安全

- **禁止硬编码任何密钥、URL、模型名**，统一经由 `src/mining_daily_agent/config.py` 的 `load_config()` 读取（它是上表变量的**唯一入口**，业务代码不应直接调 `os.getenv` / `load_dotenv`）。进程环境变量优先于 `.env`（`override=False`），便于测试与 CI 覆盖。
- `.env` 必须被 `.gitignore` 忽略，**只提交 `.env.example`**。
- 不要在任何文件、提交信息、日志或输出中回显 `LLM_API_KEY` 的值。

**当前状态**：`.gitignore` 已创建并验证生效（`git check-ignore .env` 命中规则），`.env` 不会进入版本库。仓库此前没有任何 commit，该密钥**从未进入 git 历史，无需轮换**。换新环境时从模板复制：`cp .env.example .env`。

若日后有克隆、打包或分享本目录的操作，请确认 `.env` 未被一并带出。

## 依赖所声明的意图

这些包已声明但**尚未被使用**，是理解项目方向的线索：

- 采集：`feedparser`（RSS/Atom）、`httpx`（异步 HTTP）、`beautifulsoup4` + `lxml`（HTML 解析）、`pdfplumber`（PDF 文本抽取）
- 编排：`langgraph`（agent 图/工作流）、`langchain-openai`（LLM 客户端）
- 交付：`mcp[cli]` —— 将暴露为三个 MCP server（stdio 传输），见「MCP 约定」
