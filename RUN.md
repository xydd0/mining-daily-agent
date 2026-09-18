# 5 分钟跑通指南

两条路，任选其一。都需要一个 DeepSeek API key（<https://platform.deepseek.com>）。

> 这个项目会真的发起网络请求并调用 LLM。测试则完全相反——全部 mock，不触网。

## A. 本地 uv（推荐先走这条）

```bash
git clone https://github.com/xydd0/mining-daily-agent.git
cd mining-daily-agent

cp .env.example .env
# 编辑 .env，把 LLM_API_KEY 换成你的 DeepSeek key

uv sync
uv run mining-daily-agent "给我生成一份关于 Pilbara 锂矿的今日简报"
```

首次运行约 30–60 秒（要拉起三个 MCP server、抓新闻与行情、再调一次 LLM）。
简报会打到终端，同时写入 `reports/YYYY-MM-DD-<主题>.md`。

另有一条等价命令，两条走的是同一个入口：

```bash
uv run python -m mining_daily_agent "给我生成一份关于 Pilbara 锂矿的今日简报"
```

## B. Docker

```bash
git clone https://github.com/xydd0/mining-daily-agent.git
cd mining-daily-agent

cp .env.example .env
# 同样把 LLM_API_KEY 换成你的 key

docker compose run --rm agent "给我生成一份关于 Pilbara 锂矿的今日简报"
```

位置参数会覆盖 compose 里的默认主题，因此换主题不用改文件：

```bash
docker compose run --rm agent "铜价最近怎么样"
```

**关于容器的一点重要说明**：简报写到**容器内**的 `reports/`，随容器一起销毁
（`--rm` 更是如此）——终端里的输出才是持久的。要留存文件，挂一个卷或事后拷出来：

```bash
# 方式一：挂卷（Linux 上会产出 root 属主的文件，注意权限）
#   在 docker-compose.yml 里加：volumes: [./reports:/app/reports]

# 方式二：从已退出的容器里拷
docker compose run --name brief agent "..."   # 不加 --rm
docker cp brief:/app/reports ./reports
docker rm brief
```

## 数据源策略（读之前请务必了解）

这个项目的取数链路是**能降级、且降级一定留痕**的。三个数据源的真实情况各不相同：

| 数据源 | 真实实现 | 失败时 |
|---|---|---|
| 新闻 | Google News RSS → Mining.com → Yahoo Finance 依次尝试 | 全部失败则降级为内置示例数据 |
| 资源量 | 下载 PDF，用 pdfplumber + 正则抽取（取源优先级见下） | 抽不到条目返回空 + 原文片段；下载失败则降级为内置示例数据 |
| 价格 | **代理品种**行情（见下） | 全部失败则降级为合成序列 |

### 资源量：取源优先级与确定性年报

`extract_resources` 的输入地址按**确定性从高到低**挑：

1. 新闻里**真正以 `.pdf` 结尾**的链接；
2. `DEFAULT_REPORT_URL`（未配置时用内置默认值：**Pilbara Minerals 2025 官方年报**）；
3. 标题/链接带 resource、report 等线索词的新闻条目。

第 2 级排在第 3 级前面是要害：Google News 返回的条目**几乎不是 `.pdf`**，线索词命中的
大多是普通新闻网页（矿企名里带 "Resources" 极常见）。把新闻页喂给 PDF 解析器只会解析
失败再降级——挑到哪条全看运气，每跑一次结果都可能不同。

内置年报是 **Pilbara 专项**的。换标的时请设 `DEFAULT_REPORT_URL` 指向该公司的年报，
否则会拿锂矿年报去回答别的品种。

> **这份年报很大**：15.6 MB / 186 页，实测下载 27 秒 + 文本抽取 29 秒 ≈ 56 秒。
> `MCP_CALL_TIMEOUT_SECONDS` 的默认值因此定为 **120 秒**（早先的 30 秒必然切断）。
> 若你换了一份更大的报告，记得同步调大它。

> **汇总口径**：JORC 资源表同时列分块小计与全矿总计，按类别逐行相加会把同一份资源量
> 算两遍。`analyze` 会与报告自报的合计交叉核对，不一致时以**报告自报合计**为准并在
> 风险提示里说明——所以简报里出现的是它自报的数字，不是求和值。

### 价格：没有免费 LME 行情

LME 金属现货报价**没有免费 API**。真实实现取的是与对应金属有相关性的**上市代理工具**
的行情，单位是 **USD/share，不是 USD/t**：

| 品种 | 代理工具 |
|---|---|
| lithium | LIT（Global X Lithium & Battery Tech ETF） |
| nickel | VALE（淡水河谷，镍业务占营收约 20-25%） |
| copper | COPX（Global X Copper Miners ETF） |
| cobalt | REMX（VanEck Rare Earth & Strategic Metals ETF） |

**所以不要把价格当成金属吨价引用。** 每个数据点的 `unit` 与 `source` 字段都写明了这一点，
`source` 里带 `proxy quote, not an LME <commodity> spot price`。

行情源顺序：Yahoo Finance 优先，Stooq 兜底。Stooq 的免费 CSV 端点已被 JavaScript
反爬挡住，任何非 JS 客户端都拿不到数据，放在首位只会让每次调用先白跑约 5 秒。

### 降级数据一定带标注

新闻、PDF、价格三条链路的 mock 数据都会**结构化标记**（`degraded` 字段），简报里会：

- 在风险提示小节写明「不得当作真实报道引用」「数值不可用于任何判断」
- 在来源小节给降级条目加 `【降级示例数据】` 后缀

**看到这些标注，就说明那份数据是编造的**——链路降级了，但至少不会骗你。

---

跑通之后想看内部结构，见 [`docs/architecture.md`](docs/architecture.md)；
想把这个项目接到 Claude Desktop / Cursor，见 [`README.md`](README.md)。
