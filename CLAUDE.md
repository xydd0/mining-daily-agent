# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 当前状态

`models` → `providers` → `servers` → `client` 四层**已全部打通**：

- `src/mining_daily_agent/config.py` — 环境变量集中读取与校验，缺失时抛 `ConfigError`
- `models/news.py` — `NewsItem` / `Article`
- `models/resources.py` — `ResourceCategory` / `ResourceItem` / `ResourceReport`
- `models/prices.py` — `PricePoint` / `TrendSeries`
- `providers/news/` — `base.py`（`NewsProvider` Protocol）、`rss.py`（3 个 RSS 源）、`mock.py`（8 条内置数据）
- `providers/pdf/` — `base.py`（`PdfProvider` Protocol）、`parser.py`（pdfplumber + 正则抽取，支持表头单位推断）、`mock.py`（Pilgangoora 风格资源表）
- `providers/prices/` — `base.py`（`PriceProvider` Protocol + 品种词表 + 走势计算）、`stooq.py`（Stooq→Yahoo 两源）、`mock.py`（4 品种合成序列）
- `servers/news_server.py` — `mining-news-mcp`：`search` / `fetch_article`
- `servers/pdf_server.py` — `mineral-pdf-mcp`：`extract_resources`
- `servers/price_server.py` — `lme-price-mcp`：`get_price` / `get_trend`
- `client/pool.py` — `McpConnectionPool`：并发连接三个 server、汇总工具、路由调用
- `agent/` — `state.py`（`BriefState` / `FetchPlan`）、`llm.py`（DeepSeek 客户端）、
  `nodes.py`（planner → fetch_data → analyze → synthesize → render）、`graph.py`（`run_daily_brief`）
- `src/mining_daily_agent/__main__.py` — CLI：`uv run python -m mining_daily_agent "<主题>"`
- `scripts/verify_pool.py` — 人工验收入口，真实拉起三个 server 打印工具清单
- `src/mining_daily_agent/__init__.py` 只有包说明；命令行入口统一在 `__main__.py`
  （`python -m mining_daily_agent` 与 console script `mining-daily-agent` 都走它）
- **CI 已接入**（`.github/workflows/ci.yml`，两个并行 job：`gate` 跑同一套四项门禁、
  `docker-build` 验证镜像可构建并启用 GHA 层缓存）
- **已容器化**：`Dockerfile`（多阶段）+ `docker-compose.yml`，见「容器化」
- 交付文档：`README.md`（简介 / 架构图 / 工具表 / 接入 MCP 客户端）、`RUN.md`
  （5 分钟跑通 + 数据源策略）、`docs/architecture.md`（分层与 SDK 约束）、
  `mcp-config.json`（Claude Desktop / Cursor 配置模板，含 `--directory` 占位符）

生成一份简报：

```bash
uv run python -m mining_daily_agent "给我生成一份关于 Pilbara 锂矿的今日简报"
```

产物写到 `reports/YYYY-MM-DD-<slug>.md`（该目录已 gitignore）。可选环境变量：
`REPORTS_DIR`、`DEFAULT_REPORT_URL`（新闻里找不到 PDF 时的兜底年报地址）。

启动单个 MCP server（stdio）：

```bash
uv run python -m mining_daily_agent.servers.news_server
uv run python -m mining_daily_agent.servers.pdf_server
uv run python -m mining_daily_agent.servers.price_server
```

验收连接池（真实拉起三个 server，应打印 5 个工具）：

```bash
uv run python scripts/verify_pool.py
```

`pyproject.toml` 里的依赖声明的是**意图**，不是已实现的架构。在补全代码前，不要假设「代码组织」中列出的任何子包或模块已存在——请先 `ls src/mining_daily_agent/` 确认。

## 工具链约定

- **包管理只能用 `uv`**（存在 `uv.lock`，build-backend 为 `uv_build`）。不要用 pip / poetry / conda 直接操作环境。
- **Python 3.12**（`.python-version` 与 `pyproject.toml` 的 `requires-python = ">=3.12"` 一致）。注意系统默认 `python` 是 3.14，务必通过 `uv run` 调用，不要裸跑 `python`。
- 采用 **src 布局**：代码放 `src/mining_daily_agent/`，`uv_build` 要求保持该结构。子包划分见「代码组织」。
- 入口点：`mining_daily_agent.__main__:main`（对应 `pyproject.toml` 的 `[project.scripts]`）。
  **只有这一个** CLI 实现；改动它时 `tests/test_entrypoint.py` 会校验脚本目标没有分叉。
- `pydantic` 已显式声明进 `dependencies`（此前只靠 `mcp` 传递依赖，属隐患）。

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

### MCP client 连接池的实现约束

- **每个 server 必须有一个长驻任务持有自己的会话**。`stdio_client` 内部用 anyio
  任务组，其 cancel scope 只能在**进入它的那个任务**里退出；跨任务关闭会抛
  "Attempted to exit cancel scope in a different task"。所以 `client/pool.py` 用
  `_ServerWorker` 让每个 server 在自己的 `asyncio.Task` 里建立并关闭会话，
  `aclose()` 只发停止信号再等它收尾。不要图省事改成在调用方任务里用
  `AsyncExitStack` 进出——那样在真实 server 上必炸（测试里的假会话不会暴露它）。
- **不要用 `monkeypatch.setattr(池模块, "ClientSession", 假类)` 来测**。假类不是
  `ClientSession` 子类，mypy 会拒绝赋值；硬塞就得 `# type: ignore`。正确做法是替换
  连接接缝 `pool._open_session`，用 `pool.McpSession` Protocol 描述连接池对会话的全部诉求。
- `ClientSession.call_tool` 返回**联合类型**（`CallToolResult | InputRequiredResult | Result`），
  调用方必须 `isinstance` 收窄，别假设它一定是 `CallToolResult`。
- server 的启动方式全部来自 `config.load_mcp_client_config()`，默认用当前解释器
  `sys.executable -m <module>`（连接池本就跑在项目 venv 里，不必再经 `uv run` 解析一层）。
  改回 `uv run`：设 `MCP_SERVER_LAUNCHER=uv`、`MCP_SERVER_LAUNCHER_ARGS="run python"`。

### Agent 编排的实现约束

- **`agent/state.py` 不能加 `from __future__ import annotations`**（也不能把模型导入塞进
  `TYPE_CHECKING`）。LangGraph 构建图时会运行时解析状态注解，实测会直接抛
  `NameError: name 'NewsItem' is not defined`——与 pydantic、MCP 是同一类陷阱。
- **工具返回的列表被 FastMCP 包了一层 `{"result": [...]}`**，且 `content` 里每个元素
  各占一个文本块——只读 `content[0]` 会静默丢掉除第一条以外的全部数据。解析一律走
  `structured_content`（`nodes._payload_from_result`）。
- **`FetchPlan.keywords` 要能接受数组**。实测 LLM 很自然地返回
  `["Pilbara lithium mine", "Pilgangoora", ...]`；只收字符串会让计划白白回退成默认值
  （默认关键词是整条中文主题，Google News 搜不到东西，进而整条数据链降级成 mock）。
- **降级数据必须显式披露**。`NewsItem` / `Article` / `ResourceReport` 都有 `degraded`
  字段，两个 mock provider 一律置 `True`；`fetch_data` 据此写风险提示、`build_citations`
  与资料块逐条加 `【降级示例数据】` 标记。**新建数据源时必须沿用这个字段**——mock 新闻
  带真实的标题、来源与域名，不标记的话上层根本分不出真伪。
- **挑 PDF 源要分两轮**：先找 `.pdf` 链接，再退回线索词匹配。矿业公司名里带
  "Resources" 极常见，一轮混判会把普通新闻页当成报告。

### 价格工具的代理品种陷阱（重要）

`lme-price-mcp` 返回的**不是 LME 金属价**。LME 现货行情没有免费 API，真实源只能用
**上市代理工具**（ETF / 矿业公司）的行情，单位是 `USD/share`，不是 `USD/t`。

- 每个 `PricePoint` 的 `source` 都会写明 `proxy quote, not an LME <commodity> spot price`，
  `unit` 也只会是 `USD/share`。**不要**在下游文案、摘要或报告里把它当作金属吨价引用。
- 只有 mock 合成数据才是 `USD/t`，且 `source` 标注 `mock: synthesized series`。
- 代理映射见 `providers/prices/stooq.py` 的 `PROXY_SYMBOLS`（lithium→LIT、nickel→VALE、
  copper→COPX、cobalt→REMX）。原定的镍代理 JJN 已无数据，故改用 VALE。

另：**Stooq 的 CSV 端点已被 JS 工作证明反爬挡住**，实测默认 UA 返回 404、浏览器 UA
返回挑战页，任何非 JS 客户端都拿不到数据。因此 **Yahoo Finance 是首选，Stooq 降为
兜底保留**（`SOURCES` 的顺序即优先级，其策略调整后调换两行即可恢复）。

不要把 Stooq 放回首位：它必然失败并耗尽三次重试与退避，等于每次调用先白跑约 5 秒。
实测对调后单次调用从 7588ms 降到 729ms。

## 容器化

```bash
docker compose build
docker compose run --rm agent "<主题>"    # 位置参数覆盖默认 command
```

- **一个镜像必须装下 agent 与全部三个 MCP server**。它们不是独立服务，而是由 client
  以 stdio 子进程拉起的（默认 `sys.executable -m <module>`），拆成多个容器根本跑不通。
  已在容器内实测：`sys.executable = /app/.venv/bin/python`，三个 server 全部连上、5 个工具齐全。
- 容器是**一次性任务**：跑完输出简报即退出，没有常驻进程与端口。简报写到容器内的
  `reports/`，随容器销毁；要看内容就读 stdout。想留在宿主机就自己加卷
  （`volumes: [./reports:/app/reports]`，注意 Linux 上会产出 root 属主的文件）。
- **`.env` 绝不能进镜像**（`.dockerignore` 已排除，构建上下文里就没有），密钥由
  `docker-compose.yml` 的 `env_file` 在运行时注入。
- 构建用 `uv sync --no-editable`，项目真正装进 site-packages，runtime 层因此只拷 `.venv`、
  不带源码。先用 `--no-install-project` 单装依赖，改代码不会让依赖层缓存失效。

## 提交规范

- 使用 **Conventional Commits**（`feat:` / `fix:` / `refactor:` / `docs:` / `test:` / `style:` / `chore:` 等）。
- **每次提交前必须依次通过以下四项，全绿才允许提交**：

```bash
bash scripts/gate.sh
```

**`scripts/gate.sh` 是门禁命令的唯一来源**，它依次跑四项：`ruff check .` →
`ruff format --check` → `mypy` → `pytest`。CLAUDE.md、`.pre-commit-config.yaml` 与
`.github/workflows/ci.yml` 都只调这个脚本，**不要在任何地方重复列出具体命令**——
此前三处各写一份，改一处漏一处就会「本地过了但 CI 挂了」。

脚本刻意不加 `set -e`：四项都跑完再汇总，一次看到全部问题。任一项失败则以非零码退出。

其中两项是补上来的，原因都是「看起来过了，其实没查」：

- `ruff format --check`：原先三项**不覆盖格式**——`ruff check` 只管 lint，不看排版。曾因此把未格式化的代码提交进 `main`，事后才用 `style(news):` 补修。
- `mypy` 由 `mypy src` 放宽而来：带 `src` 参数会**覆盖 `pyproject.toml` 的 `files` 列表**，只检查 src；`tests/` 与 `scripts/` 的类型错误因此长期无人发现（实跑裸 `mypy` 才暴露两处）。

注意门禁是**只检查、不修改**的：提交被拦下时自己跑 `uv run ruff format` 与
`uv run ruff check --fix .` 修好再提交（早先 pre-commit 的自动修复钩子已随统一而移除）。

## 常用命令

```bash
uv sync                    # 安装/同步依赖
uv run mining-daily-agent "<主题>"   # 与 `python -m mining_daily_agent` 等价
uv add <package>           # 新增运行时依赖
uv add --dev <package>     # 新增开发依赖

# 提交前门禁：四项依次跑完并汇总，全绿才算过（见「提交规范」）
bash scripts/gate.sh

# 跑单个测试/单个文件：必须带 --no-cov，否则全局覆盖率门槛必然不达标而报错
uv run pytest tests/test_pdf_extract.py -v --no-cov
uv run pytest tests/test_pdf_extract.py::test_name -v --no-cov

uv run ruff format         # 格式化（门禁只检查不修改，被拦下时自己跑）
uv run ruff check --fix .  # 自动修可修的 lint 问题
```

`ruff` / `mypy` / `pytest` 的配置都在 `pyproject.toml`。其中 ruff 的 `ignore` **刻意关闭了 `RUF001`/`RUF002`/`RUF003`**——它们会把中文全角标点判为「易混淆字符」，对本项目纯属误报，不要重新开启。

`.pre-commit-config.yaml` 只有一个钩子，就是调 `bash scripts/gate.sh`；用 `language: system` 复用项目自身的 uv 环境（若改用单独的 hook 仓库，mypy 会在看不到依赖的隔离环境里全量报 unresolved import）。注意两点：

- **`--all-files` 在仓库还没有 commit 时会跳过全部钩子**（`git ls-files` 为空，显示 "no files to check"），要验证钩子得显式传路径：`uv run pre-commit run --files <路径...>`。
- 钩子带 `types_or: [python, pyi]`，**只改到 Python 文件时才跑**——门禁含 pytest（约 10 秒），纯文档提交不必付这个代价。

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

## 已知缺口

1. **`ResourceReport.raw_snippets` 条数无上限**。每条上限 1000 字符，但条数不限，理论上
   可能撑爆 LLM 上下文（`Article.text` 已有 8000 字符上限）。
2. **门禁脚本只检查、不修改**。早先 pre-commit 的 `ruff --fix` / `ruff format` 钩子会
   顺手改文件，统一后没有了；提交被拦下需要手工跑一次格式化。
