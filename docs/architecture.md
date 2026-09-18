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
4. **按类别求和会重复计入**。JORC 表把同一份资源量按 In-situ / Stockpiles / 全矿
   三种口径各列一遍（实测 Pilgangoora：436 + 9 + 445，逐块相加得 890，正好是真实值
   445 的两倍）。现在分两步收口：解析器**按分块去重**——含 stockpile 的分块整块剔除，
   其余分块里只采信自报合计最大的那一块；`analyze` 再用 `ResourceReconciliation`
   核一遍，偏差超过 ±5% 就以自报合计为准并留风险提示。

5. **空行不能跨行抢数字**。Stockpiles 的 `Inferred ‑ ‑ ‑ ‑` 行没有数值，早先按
   「类别词到下一个类别词」跨行切段会把它下面的 `Sub total 9` 抢来当吨位，凭空多出
   一条 9 Mt 的 Inferred。表格形态改为**逐行解析**（一行 = 一条记录）后消失。
6. **合计行结束当前分块**。真实年报把 Table 5（资源量）与 Table 6（储量）排在**同一个
   文本块**里，中间没有空行。储量表的行用 `Proved` / `Probable` 作类别词，解析器认不出、
   整行跳过，于是储量表的 `Sub total 207.2` 一路覆盖掉了资源量表的 `Sub total 445`——
   全矿口径凭空缩水一半还多。**这个缺陷在 fixture 上不可见**（fixture 恰好截到
   "Table 6:" 为止），是把整份年报跑通才暴露的。

**已知不足**（有测试记着，别当成已修）：**品位多为 None**——真实表的列头写的是
`LiO (%)` 这类**按元素命名的列**，不含 "Grade" 字样，解析器认不出哪一列是品位。
至于 `(≥0.2% Li2O)` 这类边界品位说明已被排除，不会再被误记成 0.2%。另外自报合计的
识别靠「取最大」，若资源表与储量表排在同一块里会混入储量合计——目前靠资源量更大
侥幸躲过，要稳妥需先做表格区域切分。

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

## Known Gaps / 已知取舍

这一节是**交付边界**的正式声明，也是 [README](../README.md#limitations) 里
Limitations 小节的引用来源。写下来是为了挡住两种误读：以为它比实际可靠，
以及反过来——反复打磨一个本来就划定了边界的启发式解析器。

### 解析器是启发式的，不保证任意年报都能解

`extract_resources` 用正则 + 表头单位推断覆盖 **JORC / NI 43-101 的常见表格形态**，
不是通用表格理解。它认得：

- 行内写法 `Indicated 214 Mt at 1.15% Li2O`；
- 表头写单位、单元格只放数字（`Category  Tonnage (Mt)  Grade (% Li2O)`）；
- 分块小计与全矿总计（`Sub total` / `Total`），并剔除 stockpile 分块。

它认不得：多列并排的宽表里选对列、跨页续表丢了表头、单位写在脚注里、
资源表与储量表排在同一块时的合计混淆。

**解析不出来或结果可疑时的行为是明确的**：`resources` 留空并在 `raw_snippets`
里给出原文；或按 `reconciliation` 的 ±5% 容差改用报告自报合计。只有**下载失败**
才降级 mock，且降级数据一律带 `degraded=true`、在简报里逐条标【降级示例数据】——
**任何情况下都不会拿合成数据冒充真实披露值**。

### 报告含多个项目时只取一张表

内置年报除 Pilgangoora（Table 5，445 Mt）外还有 **Colina 项目**（Table 7，70.9 Mt）。
两张表都抽再相加会得到 515.9 Mt——**那不是重复计入，而是两个项目的资源量被加在了一起**，
分类别数字也会加不出「合计」那一行。

现在解析器**全局**挑一张：取报告自报合计最大的那张表（即报告的旗舰口径），其余列进
`ResourceReport.excluded_tables` 并在风险提示里说明。±5% 的 `reconciliation` 仍保留，
兜住「挑错了块」这种更细的错。

**剩下的一层不够精细**：披露只有表名与总吨位（如「Colina — 70.9 Mt」），简报里的
分类别数字并不标注属于哪张表。要更细需要给 `ResourceItem` 加项目 / 分块归属字段，
本轮没做。

### Google News 的文章正文取不到

Google News RSS 的 `<link>` **不是发布方 URL**，而是
`news.google.com/rss/articles/CBMi…` 的 Google 中转页。带浏览器 UA 请求它**不会
403**（返回 200），但页面是 JS 壳：实测 `fetch_article` 拿到 title `"Google News"`、
**正文 0 字符**。

于是走 Google News 这条源时，`search` 有结果、`fetch_article` 基本拿不到正文——
来源列表里有标题，正文却是空的。要真正取到正文，得先解出中转页里编码的目标 URL
（Google 的 batchexecute 接口），属独立工作量，本轮未做。

另一类限制是**部分发布方站点对非浏览器 UA 直接 403**（实测 mining.com 文章页），
已统一带浏览器 UA；这不保证所有站点都抓得到。

### 价格没有免费 LME 行情

LME 金属现货**没有免费 API**。真实源取的是相关 **ETF / 矿业公司**的行情，单位是
`USD/share` 而不是 `USD/t`（映射见 `providers/prices/stooq.py` 的 `PROXY_SYMBOLS`）。
全部失败时降级为合成序列并标 `degraded=true`。**不要在任何下游文案里把它当金属吨价引用。**

### 连接池是模块级全局的

`agent/nodes.py` 用模块级变量 `_pool` 持有连接池，由 `run_daily_brief` 注入、跑完清空。

**单进程 CLI 的交付形态没有并发场景**，所以这个写法够用；代价是同一个进程里同时跑
两份简报会互相覆盖（后注入的把前一个顶掉）。

生产化方向（本轮不做）：改用 `ContextVar`（并发安全，语义也仍是「当前上下文的池」），
或把连接池作为**图配置**注入而不是模块状态。两者都要动节点签名，收益只在多租户/服务化
场景才体现得出来。

### LLM 是「有则更好」的依赖

LLM 走 **DeepSeek 的 OpenAI 兼容端点**（`LLM_BASE_URL` 可覆盖，不是 OpenAI 官方服务），
只用在两处：

1. **planner**：把主题转成取数计划。输出无法解析成 JSON、或调用直接失败时，回退到
   `default_plan(topic)` 并记入风险提示。
2. **新闻小节的小标题与导语**：这两样确实需要读正文。调用失败或回复不成形状时，
   逐条退回「标题当小标题、摘要当导语」的确定性写法。

**其余小节全部由代码按写死的模板渲染**（`nodes.py` 的 `_render_*`）：小节名、编号对应、
「矿石量 X Mt」这类措辞是硬性约束——靠提示词保证不了，靠代码可以。所以**没有 LLM 时
简报仍然完整**，只是新闻措辞退化成原始标题。

### 输出模板是写死的

```
# 矿权日报 · {主体} · {日期}
## 一、新闻摘要     ← LLM 写小标题与导语
## 二、储量数据     ← 代码渲染，吨位一律「矿石量 X Mt」
## 三、价格走势     ← 代码渲染，尾注原样带出代理品种的免责声明
## 四、风险提示     ← 代码渲染，无风险时写「本轮无重大风险事件」
## 引用源         ← 代码渲染，[n] 与正文一一对应
```

三处与「写死」配套的护栏：

- **新闻进简报前先过滤**：只保留讲主题主体的条目（`filter_relevant_news`）。实测拿
  "Pilbara" 检索，Google News 回来的 8 条里有 6 条与 Pilbara 无关。剔除条数写进风险
  提示，不静默丢。
- **回溯天数有下限**（`MIN_PLAN_DAYS = 7`）。主题写的是「今日简报」，实测 LLM 会据此
  返回 `days=1`——一天的窗口搜回条数极少，再被主体过滤后整个新闻小节空掉。下限兜在
  代码里，不只写在提示词里。
- **一张表只取一张**：报告含多个项目的资源表时（内置年报有 Pilgangoora 与 Colina），
  只取报告自报口径最大的那张，其余列进 `ResourceReport.excluded_tables` 并在风险提示
  里说明——否则分类别数字加不出「合计」那一行。
