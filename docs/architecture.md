# 架构

本文档记录**分层设计**、**降级链路**，以及开发过程中实测发现的一批 SDK 约束——
后者大多只在真实运行时才会暴露，读代码看不出来。

## 分层设计

```
src/mining_daily_agent/
├── models/      领域数据模型（pydantic）：NewsItem / Article / ResourceReport / PricePoint / TrendSeries
├── providers/   数据源：每个源都有「真实实现 + mock 降级实现」
├── servers/     三个 MCP server，只做协议适配，不含业务逻辑
├── client/      连接池：并发连接、工具汇总、调用路由
└── agent/       LangGraph 五节点流水线 + LLM 客户端
```

依赖方向是单向的：`models` ← `providers` ← `servers` ← `client` ← `agent`。
`models` 不依赖任何一层。

### 每个数据源都是三件套

```
providers/news/
├── base.py       NewsProvider Protocol：接口与共享的品种词表
├── rss.py        真实实现
├── mock.py       降级实现
└── __init__.py   工厂 get_news_provider()：真实实现失败即返回 mock
```

这条结构是硬性约定（见 CLAUDE.md「可靠性」）：**没有 mock 兜底的数据源不允许合并**。
三件套的存在让「真实源挂了」从「整条流程失败」变成「拿到一份带降级标注的数据」。

### 为什么三个 server 在同一个镜像里

它们不是独立服务，而是由连接池以 **stdio 子进程**拉起的
（`config.load_mcp_client_config()` 默认走 `sys.executable -m <module>`）。
这决定了：

- 拆成多个容器根本跑不通——它们必须能被同一个解释器 import 到；
- 容器里 console script 的 shebang 指向 `/app/.venv/bin/python`，`sys.executable`
  因此是正确的解释器（已在容器内实测）。

## 三级降级链路

失败会在三个层次上被逐级兜住，每一级都记 `logging.warning`：

| 层级 | 位置 | 例子 |
|---|---|---|
| 1. 源内多候选 | provider 内部 | 新闻依次试 Google News → Mining.com → Yahoo；行情依次试 Yahoo → Stooq |
| 2. 工厂降级 | `providers/*/__init__.py` | 真实实现**构造**失败 → 返回 mock |
| 3. 调用期降级 | `servers/*_server.py` | 工具**调用**抛异常 → 换 mock 重试，返回带标注的结果 |

### 有一类情况刻意不降级

「品种不支持」「该日期没有行情」「PDF 抽不到条目」是**业务的合法否定**，不是数据源故障。
把它们降级成 mock，等于凭空编造一个价格或一份资源量返回给用户——**比直接报错更糟**。
所以这类情况原样抛出，并且复用 `ValueError + ToolError` 双继承把说明送达 LLM
（MCP 只原样转发 `ToolError` 的 message，见下）。

PDF 那条更微妙：**抽不到条目不算失败**，真实实现会正常返回空 `resources` 加候选原文
`raw_snippets`，交给上层判断——这与「下载失败」是两回事，前者不该触发 mock 降级。

### 取数源的确定性排序

`agent/nodes.py` 的 `_pick_report_url` 按**确定性从高到低**选地址：

1. 新闻里真正的 `.pdf` 链接；
2. `config.default_report_url()`——可配置的确定性年报（内置默认值为 Pilbara Minerals
   2025 官方年报）；
3. 标题/链接带 resource、report 线索词的新闻条目（保底，当前不可达）。

**2 必须排在 3 前面**，这是修过的一个真实缺陷：Google News 返回的条目几乎不是 `.pdf`，
而线索词命中的绝大多数是普通新闻网页（矿企名里带 "Resources" 极常见，实测被
"Raiden Resources" 命中过）。新闻页喂给 PDF 解析器只会失败再降级——等于挑到哪条全看
运气，每次跑出来的结果都可能不同。回溯到确定的年报就没有这个问题。

第 3 级保留但**当前不可达**（`default_report_url()` 有内置值、不返回空串）。留着是为了
把「确定性优先」写死在代码里：万一将来内置默认值被去掉或可关闭，它仍接得住，而不至于
退回「看运气」。测试用 monkeypatch 把它打成空串来覆盖这条分支。

### 工具超时与文档体量的冲突

第 2 级那个确定性年报有个副作用：它**很大**。实测 15.6 MB / 186 页，下载 26.6 秒 +
`extract_text` 29.1 秒，合计约 56 秒，**超过 MCP 工具调用的默认超时**，于是在
`client` 层的 `asyncio.timeout` 处被切断，走到调用期降级。

`MCP_CALL_TIMEOUT_SECONDS` 的默认值因此从 30 提到了 **120**。教训是：**降级链路的
正确性不保证数据的可获得性**——链路可以诚实地告诉你它失败了，但那不等于它能拿到数据。

### 从真实年报学到的解析规则

拿一份真实年报（Pilbara Minerals 2025）当基准跑，四条规则是被实测逼出来的：

1. **类别只认首字母大写**。全文 185 个文本块里，7 块命中关键词，其中 6 块是误报——
   "measured at fair value"、"where indicated in the Annual Report"、
   "measured against the Baseline"。而资源表里的 15 处类别词**全部**大写。
   加大小写敏感后：87 条 → 16 条，真表行一条不少。
2. **吨位的量级区间要覆盖 1e8**。典型资源量是 349 Mt = 3.49e8 吨；区间定成 1e7 上限
   会把所有真实值判为可疑。现用 1e5–1e10 吨。
3. **表头单位只沿用到紧邻的下一块**。"Tonnes (Mt)" 一路沿用会让它泄漏进后面几十块
   会计正文，把任意数字都变成吨位。
4. **按类别求和会重复计入**。JORC 表同时列分块小计与全矿总计（In-situ 349 +
   Stockpiles 8 + 总计 356 是同一份资源量），逐行相加把 356 Mt 加成 760 Mt。
   现在与报告自报合计交叉核对，超出 1.2 倍即以自报为准并留风险提示。

**已知不足**（有测试记着，别当成已修）：品位多不可靠——表把单位放在列头，段内第一个
带 `%` 的数字常来自 "(≥0.2% Li2O)" 这类注脚；Stockpiles 的 Inferred 行没有数值，
会误取到同块的 "Sub total" 数字。另外自报合计的识别靠「取最大」，若资源表与储量表
排在同一块里会混入储量合计——目前靠资源量更大侥幸躲过，要稳妥需先做表格区域切分。

### 降级数据必须留痕

三条链路的 mock 数据都在模型上打 `degraded=True`，agent 据此：

- 写风险提示（「不得当作真实报道引用」「数值不可用于任何判断」）
- 给来源小节的条目加 `【降级示例数据】` 后缀

早期版本只在 PDF 侧用 `raw_snippets[0]` 里的一段文案做文本匹配，脆弱且新闻侧完全没有。
现在是结构化字段——**新增数据源时必须沿用**。

## 开发中发现的 SDK 约束

以下每一条都是实际跑出来的，附上当时的症状。

### 1. anyio cancel scope：会话必须在同一个任务里进出

**症状**：连接池按常规写法在调用方任务里用 `AsyncExitStack` 进出 `stdio_client`，
真实 server 上抛 `Attempted to exit cancel scope in a different task than it was entered in`。

**原因**：`stdio_client` 内部用 anyio 任务组，其 cancel scope 只能在**进入它的那个任务**里退出。

**修复**：每个 server 由一个长驻 `asyncio.Task`（`_ServerWorker`）持有自己的会话，
建立与关闭都发生在那个任务内；`aclose()` 只发停止信号再等各任务收尾。

**教训**：这个问题**测试用假会话时完全不暴露**，只有真实子进程才触发。
这也是为什么验收脚本 `scripts/verify_pool.py` 要真的拉起三个 server，而不是只跑单测。

### 2. MCP 列表返回被包了一层 `result`

**症状**：`news.search` 明明返回多条，解析出来只有第一条——或直接解析失败。

**原因**：两层叠加。

1. MCP 的结构化内容必须是**对象**，所以返回列表的工具被 FastMCP 包成 `{"result": [...]}`，
   而返回单个对象的工具直接给对象本身——两种形态都要认；
2. `content` 里**每个列表元素各占一个文本块**，只读 `content[0]` 会静默丢掉其余全部。

**修复**：`agent/nodes.py` 的 `_payload_from_result` 优先读 `structured_content`，
识别 `{"result": [...]}` 单键包装并拆开；仅在无结构化内容时才回退解析文本块。

测试里的假结果**刻意保留了真实形态**（列表包一层、每个元素一个文本块），
这样「改回读 content[0]」会立刻被测出来。

### 3. `from __future__ import annotations`：三处运行时解析注解

**症状**：把只用于注解的导入移进 `TYPE_CHECKING` 后，模块导入或类定义**直接抛
`NameError`**——而且是在运行时报错，静态检查完全看不出来。

三处都踩过，机理相同：这些框架会在运行时求值注解。

| 位置 | 谁在运行时解析 | 后果 |
|---|---|---|
| `models/*.py` | pydantic 建模型 | `NameError` 或字段类型解析失败 |
| `servers/*_server.py` | MCP 生成工具 JSON schema | 工具注册失败 |
| `agent/state.py` | **LangGraph 构建图**（含 `Annotated` 里的 reducer） | `NameError: name 'NewsItem' is not defined` |

**修复**：这些文件刻意**不使用** `from __future__ import annotations`，让注解立即求值；
ruff 的 TC001/TC002/TC003 因此也不会再报「可以移进 TYPE_CHECKING」。

`agent/state.py` 那条是实测确认的：构造一个注解只存在于 `TYPE_CHECKING` 下的 TypedDict，
`StateGraph(...).compile()` 直接抛 `NameError`。

### 4. Dockerfile：层缓存与那个漏掉的 COPY

**症状**：镜像构建失败于 `uv sync --frozen --no-dev --no-install-project`，
报 `No pyproject.toml found in current directory or any parent directory`。

**原因**：Dockerfile 漏了 `COPY pyproject.toml uv.lock ./`——清单根本没进构建上下文。
**读代码看不出来，只有真正构建才会暴露**（这也是 CI 里那个 `docker-build` job 存在的理由）。

**层缓存的正确用法**：分成两层，让依赖层只在清单变化时失效。

```dockerfile
COPY pyproject.toml uv.lock ./
RUN  uv sync --frozen --no-dev --no-install-project   # 依赖层：不依赖源码
COPY README.md ./ && COPY src ./src
RUN  uv sync --frozen --no-dev --no-editable          # 源码层：真正装项目
```

`--no-install-project` 是关键：没有它，第一次 `uv sync` 会因为 `src/` 尚未拷入而失败，
把依赖层和源码层绑死。**缓存的「欺骗性」也在这里**——热缓存下所有层显示 `CACHED`，
看不出冷构建是否真的能过；所以本地验证用了
`docker buildx build --no-cache --output type=cacheonly .` 复现 CI 的确切路径。

## 工程约束

- **门禁命令的唯一来源是 `scripts/gate.sh`**（ruff check → ruff format --check →
  mypy → pytest）。CLAUDE.md、pre-commit、CI 都只调它，不再各写一份。
- **`mypy` 是全量的**。早先用 `mypy src`，带路径参数会覆盖 `pyproject.toml` 的 `files`，
  导致 `tests/` 与 `scripts/` 的类型错误长期无人发现。
- **提交前四项全绿**，CI 里 `gate` 与 `docker-build` 两个 job 并行。
