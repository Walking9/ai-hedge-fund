# AI Hedge Fund 使用与配置指南（中文）

> 适用版本：2.5.0 以后、已切换到 Yahoo Finance 免费数据源的代码。
> 本文说明项目怎么装、怎么配、怎么跑、怎么看结果，以及免费数据源的能力边界和应对方法。

---

## 1. 项目是什么

这是一个可以在终端里构建、回测、模拟运行的"AI 对冲基金"。一支基金由若干**策略**（pod）组成，每个策略配备若干**分析师**：

- **LLM 投资人格**：buffett、munger、graham、lynch、druckenmiller。它们读取某只股票在某个日期**当时已公开**的基本面快照，用各自的投资哲学给出看多、中性、看空的观点。
- **量化模型**：pead（盈利公告后漂移，Post-Earnings Announcement Drift）。纯数学，不调用 LLM。

每个交易日收盘后，分析师给出观点，策略内部加权合成，基金再按资金份额把各策略净额合并，经过风控限幅后形成目标仓位，**在下一个收盘价执行**。回测和模拟盘走的是同一条代码路径，所以回测结果是可信的。

数据流：

```
数据源（时点正确） → 分析师观点 → 组合构建 → 风控 → 执行 → 账本
```

---

## 2. 安装

```bash
git clone https://github.com/virattt/ai-hedge-fund.git
cd ai-hedge-fund
poetry install
poetry run aihf          # 不带参数：启动交互式终端界面
```

依赖里已包含 `yfinance`，装完即可取数，**不需要任何数据 API key**。

---

## 3. 配置

### 3.1 配置文件放哪里

密钥和开关放在 `.env` 文件里，两处都会被读取：

| 位置 | 用途 |
|------|------|
| `~/.hedge-fund/.env` | 用户级配置，终端界面首次要用某个 key 时会提示你输入并写到这里 |
| 项目目录下的 `.env` | 开发用，优先级高于用户级文件 |

规则：**shell 里已经 export 的环境变量永远优先**，`.env` 只是兜底。项目根目录有 `.env.example` 可以照抄。

### 3.2 数据源

| 变量 | 取值 | 说明 |
|------|------|------|
| `HEDGE_FUND_DATA_PROVIDER` | 不设，或 `yfinance` | **默认**。Yahoo Finance 免费接口，无需 key |
| `HEDGE_FUND_DATA_PROVIDER` | `financialdatasets` | 切换到付费的 Financial Datasets，需同时设置下一行 |
| `FINANCIAL_DATASETS_API_KEY` | 你的 key | 仅付费源需要 |

代码里所有入口都通过 `hedge_fund.data.make_data_client()` 取客户端，不要在业务代码里直接构造某个具体客户端。

### 3.3 LLM 模型

只有 LLM 人格需要模型 key，pead 不需要。

| 变量 | 说明 |
|------|------|
| `HEDGE_FUND_LLM_MODEL` | 分析师使用的模型名，例如 `claude-opus-5-5`、`coding-kimi-k3-free` |
| `aihf --model <名称>` | 命令行临时覆盖上一项 |

模型名与所需 key 的对应关系在 `hedge_fund/llm/api_models.json`，每个模型声明了 `provider`，按 provider 设置对应的 key：

| provider | 环境变量 | 备注 |
|----------|----------|------|
| Anthropic | `ANTHROPIC_API_KEY` | |
| OpenAI | `OPENAI_API_KEY` | 可用 `OPENAI_API_BASE` 指向兼容中转站 |
| DeepSeek | `DEEPSEEK_API_KEY` | |
| Google | `GOOGLE_API_KEY` | |
| xAI | `XAI_API_KEY` | |
| Kimi | `KIMI_API_KEY` 或 `MOONSHOT_API_KEY` | 大陆用户可设 `MOONSHOT_BASE_URL=https://api.moonshot.cn/v1` |
| TypeSafe | `TYPESAFE_API_KEY` | Jev 模型 |

一个常见组合：通过 AiHubmix 中转用 Kimi 免费模型。清单里 `coding-kimi-k3-free` 的 provider 标的是 `OpenAI`，所以要设的是 `OPENAI_API_KEY` 加 `OPENAI_API_BASE`，而不是 Kimi 自家的 key。

### 3.4 用户数据目录 `~/.hedge-fund/`

```
mandates/<name>.yaml        基金模版：策略、人员、风控、资金、调仓频率（不含股票）
research/<基金>-<起>-<止>-<时间戳>.json   每次回测的完整结果
paper/<name>/               一支已部署的模拟盘基金
  fund.yaml                 模版快照 + 股票池
  broker.json               模拟券商账本（现金、持仓）
  ledger/<日期>.json         每个交易日一条记录，哈希链式防篡改
  ledger/superseded/        被 --again 重跑替换下来的旧记录，保留可查
  control.json              紧急停止开关
  events.jsonl              运行事件日志
cache/data/                 数据接口的磁盘缓存
cache/llm/                  每次 LLM 决策的完整提示词与回复
.env                        密钥
```

首次运行会自动创建 `mandates/` 并放入一份 `example.yaml`。

---

## 4. 三种使用方式

### 4.1 交互式界面

```bash
poetry run aihf
```

两个模式：
- **模拟盘**：列出你的基金，高亮可预览下一次运行会做什么，回车经确认步骤后推进一个交易日；`s` 看历史，`h`/`r` 停止与恢复；也可以在界面里一步步新建基金。
- **回测**：选模版、选区间、填股票池，看净值曲线对比基准，结果自动存到 `research/`。

### 4.2 命令行回测

```bash
poetry run aihf backtest ~/.hedge-fund/mandates/broad-growth.yaml  \
    --universe AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA,AMD,AVGO,QCOM,CRM,ADBE,ORCL,IBM,INTC --start 2026-06-12 --end 2026-10-10

poetry run aihf backtest ~/.hedge-fund/mandates/example.yaml \
    --universe NVDA,GOOGL,AMZN,TSLA,AMD \
    --start 2025-09-30 \
    --end 2026-10-10
```

| 参数 | 说明 |
|------|------|
| `mandate` | 模版 YAML 路径（位置参数） |
| `--universe` | 股票池，逗号或空格分隔，**必填** |
| `--start` | 第一个交易日；缺省为 `--end` 往前 78 周 |
| `--end` | 最后一个交易日；缺省为最近一个已完成的交易日 |
| `--out` | 额外把结果 JSON 写到指定文件 |
| `--model` | 临时指定 LLM（写在 `backtest` 之前） |

stdout 输出完整结果 JSON，stderr 输出一行汇总，`research/` 下自动留一份副本。

回测时 LLM 的提示词会**隐去**股票代码、行业和具体日期，防止模型"记得"这家公司后来发生了什么；规模只以市值分位呈现，为此首次回测会下载一张市值分位表。

### 4.3 命令行模拟盘

```bash
# 部署：模版 + 股票池 → 一支基金
poetry run aihf paper create alpha --mandate ~/.hedge-fund/mandates/example.yaml --universe AAPL,MSFT
poetry run aihf paper create 20sister --mandate ~/.hedge-fund/mandates/broad-growth.yaml --universe AAAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA,AMD,AVGO,QCOM,CRM,ADBE,ORCL,IBM,INTC
# 每个收盘后推进一个交易日（幂等、不跳日，可以放 cron）
poetry run aihf paper tick 20sister
poetry run aihf paper tick alpha --again       # 重跑最近一天，旧记录移入 ledger/superseded/
poetry run aihf paper tick alpha --session 2026-10-09   # 显式指定要记录的那一天

poetry run aihf paper status 20sister               # 净值、最近交易日、待执行决策、是否停止
poetry run aihf paper list                       # 所有已部署基金
poetry run aihf paper halt alpha --reason "人工复核"   # 紧急停止，之后 tick 拒绝交易
poetry run aihf paper resume alpha               # 解除
```

模拟盘的节奏：T 日收盘做决策，T+1 日收盘执行。离开一周回来，要 tick 一周才能追平，不会跳过。

"已完成的交易日"只算到**纽约时间的昨天**，今天永远不算。所以周末 tick 会提示 `no completed SPY session after ...`，这是正常的；周一的交易日要等到纽约时间周二，也就是北京时间周二中午 12 点以后，才能被记录。

---

## 5. 查看运行结果

所有结果都是 JSON 文件，结构统一，用 Python 几行就能读。本节先讲屏幕上的一行汇总怎么看，再讲文件怎么拆，最后给几段现成的查看脚本。

### 5.1 回测：屏幕上的一行汇总

跑完 `aihf backtest` 后 stderr 会打印一行：

```
example-fund 2025-01-02 → 2025-06-27  ·  121 sessions  ·  26 rebalances  ·  0 orders  ·
return +0.0% vs SPY +5.2%  ·  sharpe 0.00  ·  max drawdown 0.0%
saved to ~/.hedge-fund/research/example-fund-2025-01-02-2025-06-27-20261011-200227.json
```

| 字段 | 含义 |
|------|------|
| `sessions` | 区间内基准（SPY）的交易日数，也是净值曲线的点数 |
| `rebalances` | 真正执行过的调仓次数（`n_cycles`） |
| `orders` | 总下单笔数；为 0 说明每次目标仓位都是 0，见第 7 节 |
| `return` | 基金区间收益 vs 基准同期收益 |
| `sharpe` | 年化夏普 |
| `max drawdown` | 最大回撤 |
| `saved to` | 完整结果文件，下面所有分析都从它开始 |

stdout 是完整 JSON。想同时留档又不刷屏，用 `--out` 或重定向：

```bash
poetry run aihf backtest ... --out result.json 2>summary.txt
```

### 5.2 回测结果文件的结构

找最新一份：

```bash
ls -t ~/.hedge-fund/research/*.json | head -1
```

顶层字段：

| 字段 | 内容 |
|------|------|
| `fund` `start` `end` `rebalance` `benchmark` `universe` `capital` | 本次回测的参数 |
| `dates[]` `nav[]` `benchmark_nav[]` | 三个等长数组：每个交易日、基金净值、按同样本金缩放的基准净值 |
| `metrics` | `total_return_pct` `annualized_return_pct` `sharpe_ratio` `max_drawdown_pct` `benchmark_return_pct` `excess_return_pct` `n_cycles` `n_orders` |
| `records[]` | **每个交易日一条**，是全部细节所在 |

每条 `records[]`（一个 SessionRecord）：

| 字段 | 内容 |
|------|------|
| `session` | 交易日 |
| `nav` `cash` `positions` `marks` | 当日收盘后的净值、现金、持仓（股数，负数为空头）、各股收盘价 |
| `benchmark_close` | 基准当日收盘 |
| `rebalance` | 这一天是否在调仓日历上 |
| `decision` | 当日收盘做出的**决策**，非调仓日为 `null` |
| `executed` | 当日收盘**执行**的上一个决策，没有执行则为 `null` |
| `llm_model` `code_version` `mandate_hash` | 审计信息：用的哪个模型、哪个代码版本、哪份模版 |
| `hash` `prev_hash` | 哈希链，模拟盘账本用它防篡改 |

`decision` 里面从上到下就是一次决策的全过程：

```
marks            决策时各股的标记价
skipped[]        没取到价格而被排除的股票及原因
strategies[]     每条策略：
  name, slice    名称、资金份额
  signals[]      每位分析师对每只股票的观点
    model_name, ticker, value（-1 空 ~ +1 多，0 中性）
    reasoning    分析师的一句话理由
    metadata     abstained / abstain_reason / cached / prompt_key
  convictions    策略内合成后的观点
  weights        策略自己的目标仓位
  flat_reason    为什么空仓（例如 no_eligible_positions）
target_weights   各策略按份额净额合并后的仓位
clamps[]         风控限幅记录
final_weights    风控后的最终目标仓位
```

`executed` 在 `decision` 的全部字段之外，多了执行层的信息：`as_of`（对应哪一天的决策）、`execution_as_of`（在哪一天执行）、`equity_before` `cash_before`、`orders[]` 和 `fills[]`（每笔 `ticker` `side` `quantity` `price`）、执行后的 `positions` `cash` `nav`。

### 5.3 回测：现成的查看脚本

下面每段都自动取 `research/` 里最新的一份结果；要看指定文件，把 `p = ...` 那行换成文件路径即可。

**概览与指标：**

```bash
poetry run python - <<'PY'
import json, glob, os
p = max(glob.glob(os.path.expanduser("~/.hedge-fund/research/example-fund-2025-09-30-2026-10-09-20261013-235121.json")), key=os.path.getmtime)
d = json.load(open(p))
print(p)
print(d["fund"], d["start"], "→", d["end"], "|", d["rebalance"], "vs", d["benchmark"], "|", d["universe"])
for k, v in d["metrics"].items():
    print(f"  {k:24s} {v}")
PY
```

**净值曲线导出 CSV**（可以直接丢进 Excel 画图）：

```bash
poetry run python - <<'PY' > nav.csv
import json, glob, os, csv, sys
p = max(glob.glob(os.path.expanduser("~/.hedge-fund/research/*.json")), key=os.path.getmtime)
d = json.load(open(p))
w = csv.writer(sys.stdout); w.writerow(["date", "nav", "benchmark_nav"])
for row in zip(d["dates"], d["nav"], d["benchmark_nav"]):
    w.writerow(row)
PY
```

**所有成交记录**（哪天、按哪天的决策、买卖了什么、执行后持仓）：

```bash
poetry run python - <<'PY'
import json, glob, os
p = max(glob.glob(os.path.expanduser("~/.hedge-fund/research/example-fund-2025-09-30-2026-10-09-20261013-235121.json")), key=os.path.getmtime)
for r in json.load(open(p))["records"]:
    ex = r.get("executed")
    if not ex or not ex["fills"]:
        continue
    print(f"{r['session']}  (决策日 {ex['as_of']})  NAV {ex['nav']:,.2f}")
    for f in ex["fills"]:
        print(f"    {f['side']:4s} {f['ticker']:6s} {f['quantity']:>6d} @ {f['price']:.2f}")
    print("    持仓:", ex["positions"])
PY
```

**分析师观点与弃权统计**（排查"为什么没交易"最先看这个）：

```bash
poetry run python - <<'PY'
import json, glob, os, re, collections
p = max(glob.glob(os.path.expanduser("~/.hedge-fund/research/*.json")), key=os.path.getmtime)
reasons, views = collections.Counter(), collections.Counter()
for r in json.load(open(p))["records"]:
    dec = r.get("decision")
    if not dec:
        continue
    for s in dec["strategies"]:
        for g in s["signals"]:
            m = g.get("metadata") or {}
            if m.get("abstained"):
                # 去掉理由里的"股票 as of 日期: "，同类原因才能合并计数
                why = re.sub(r"\S+ as of \d{4}-\d{2}-\d{2}: ", "", m.get("abstain_reason") or "")
                reasons[(g["model_name"], why[:80])] += 1
            else:
                side = "多" if g["value"] > 0 else "空" if g["value"] < 0 else "中性"
                views[(g["model_name"], g["ticker"], side)] += 1
print("== 弃权原因（次数） ==")
for k, n in reasons.most_common(): print(f"{n:5d}  {k[0]:14s} {k[1]}")
print("== 有效观点（次数） ==")
for k, n in views.most_common(): print(f"{n:5d}  {k[0]:14s} {k[1]:6s} {k[2]}")
PY
```

**某一天的完整决策**（改 `DAY`）：

```bash
poetry run python - <<'PY'
import json, glob, os
DAY = "2025-01-02"
p = max(glob.glob(os.path.expanduser("~/.hedge-fund/research/*.json")), key=os.path.getmtime)
r = next(x for x in json.load(open(p))["records"] if x["session"] == DAY)
d = r["decision"] or {}
print("标记价:", d.get("marks")); print("跳过:", d.get("skipped")); print("风控限幅:", d.get("clamps"))
for s in d.get("strategies", []):
    print(f"\n策略 {s['name']}  份额 {s['slice']}  flat_reason={s['flat_reason']}")
    print("  合成观点:", s["convictions"]); print("  目标仓位:", s["weights"])
    for g in s["signals"]:
        print(f"  {g['model_name']:14s} {g['ticker']:6s} {g['value']:+.2f}  {(g.get('reasoning') or '')[:100]}")
print("\n最终权重:", d.get("final_weights"))
PY
```

### 5.4 模拟盘：命令行怎么看

**` poetry run aihf paper status 7sister_IBKR`** 打印一行状态加两段明细：

```
alpha              example-fund  ·  AAPL MSFT  ·  weekly  ·  3 sessions  ·  last 2026-10-09  ·  NAV $100,000.00 (+0.00%)
  pending decision, to execute at the next close:
    AAPL: +12.50%
    MSFT: +12.50%
  book:
    AAPL: +41 @ 243.85
```

- 第一行：基金名、模版名、股票池、调仓频率、已记录的交易日数、最近一天、净值与收益；若已停止会追加 `HALTED: 原因`。
- `pending decision`：最近一次决策的目标权重，**下一个收盘才会成交**。
- `book`：当前持仓股数与最近标记价。

**`poetry run aihf paper list`** 对每支基金各打一行同样格式的状态。

**`poetry run aihf paper tick alpha`** 成功时 stdout 输出这一天完整的 SessionRecord JSON（结构与第 5.2 节 `records[]` 的一条完全相同），stderr 一行汇总，有三种：

| 汇总 | 含义 |
|------|------|
| `decided on N signals, M clamps; executes next close` | 今天是调仓日，做了决策，等下个收盘执行 |
| `executed N orders from <日期>` | 今天执行了某日的决策，下了 N 笔单 |
| `marked the book; no rebalance due` | 非调仓日，只按收盘价重估了持仓 |

建议把每次 tick 的 stdout 落盘：`poetry run aihf paper tick alpha > tick-$(date +%F).json`。

### 5.5 模拟盘：文件怎么看

```bash
ls ~/.hedge-fund/paper/alpha/ledger/          # 每个交易日一个 <日期>.json
cat ~/.hedge-fund/paper/alpha/broker.json      # 券商账本：现金、持仓
cat ~/.hedge-fund/paper/alpha/control.json     # {"halted": false/true, "reason": ..., "since": ...}
tail ~/.hedge-fund/paper/alpha/events.jsonl    # 每行一个事件：time、kind（tick/失败/halt/resume 等）及细节
ls ~/.hedge-fund/paper/alpha/ledger/superseded/  # 被 --again 替换下来的旧记录，文件名带原哈希
```

`ledger/<日期>.json` 就是一条 SessionRecord，所以第 5.3 节的脚本把 `r` 换成这个文件的内容就能直接用。快速看最近一天：

```bash
poetry run python - <<'PY'
import json, glob, os
p = sorted(glob.glob(os.path.expanduser("~/.hedge-fund/paper/alpha/ledger/*.json")))[-1]
r = json.load(open(p))
print(p)
print("NAV", r["nav"], "现金", r["cash"], "持仓", r["positions"], "标记价", r["marks"])
print("今日决策:", "有" if r.get("decision") else "无", "| 今日执行:", "有" if r.get("executed") else "无")
if r.get("executed"):
    for f in r["executed"]["fills"]:
        print(f"  {f['side']:4s} {f['ticker']:6s} {f['quantity']:>6d} @ {f['price']:.2f}")
if r.get("decision"):
    print("  待执行权重:", r["decision"]["final_weights"])
PY
```

**账本的完整性。** 每次 tick 和界面里的每次运行都会从第一条记录开始重放整条哈希链并逐条校验。如果看到 `chain broken at session ...` 或 `has been altered since it was written`，说明 `ledger/` 里有文件被改过、删过或顺序乱了，程序会拒绝继续，这是设计使然。不要手工编辑账本文件。

### 5.6 查看 LLM 分析师到底说了什么

每位 LLM 分析师的每次判断，完整的系统提示词、用户提示词、原始回复和解析结果，都存在：

```
~/.hedge-fund/cache/llm/<prompt_key>.json
```

`prompt_key` 就是信号里的 `metadata.prompt_key`。从一条信号找到它的对话记录：

```bash
poetry run python - <<'PY'
import json, os
KEY = "在这里填 signal.metadata.prompt_key"
d = json.load(open(os.path.expanduser(f"~/.hedge-fund/cache/llm/{KEY}.json")))
print(d["agent"], d["model"], d["ticker"], d["as_of"], d["created_at"])
print("\n--- 用户提示词（基本面快照） ---\n", d["user"][:2000])
print("\n--- 原始回复 ---\n", d.get("response"))
print("\n--- 解析结果 ---\n", d.get("parsed") or d.get("parse_error") or d.get("call_error"))
PY
```

几点说明：

- 记录里有 `parsed` 表示成功；只有 `parse_error` 表示模型回了东西但不是合法 JSON；只有 `call_error` 表示调用本身失败（限流、key 错、超时），`diagnostics` 里有细节。这两种失败都会让分析师**弃权**而不是中断运行。
- 信号 `metadata.cached: true` 表示这次没有新调用 LLM，直接复用了同一份快照的旧结论。基本面没变，提示词就一字不差，所以不花钱也不重复问。
- 回测里的提示词是"盲化"的，看不到股票代码和日期，但记录文件本身仍存着 `ticker` 和 `as_of`，方便你对照。
- 想看最近几次调用：`ls -t ~/.hedge-fund/cache/llm/ | head`。

### 5.7 在交互式界面里看

- **模拟盘页面**：高亮一支基金，右侧 `NEXT RUN` 显示下一次运行会执行哪天的决策；按 `s` 进入会话历史，逐日看决策与成交。
- **回测页面**：运行时实时画基金与基准的净值曲线，底部滚动分析师的工作状态；结束后结果同样落在 `research/`。
- **模版选择页**会读取 `research/` 展示每份模版的历史回测记录。

---

## 6. 基金模版（mandate YAML）详解

以自带的 `example.yaml` 为例：

```yaml
schema_version: 2
name: example-fund
strategies:
  - name: deep-value          # 策略一：自主判断型，Graham 主导，只做多
    blend: {mode: long_only}
    weight: 0.6               # 资金份额，与兄弟策略相对
    models:
      - name: graham
        weight: 2.0           # 策略内部合成权重
      - name: buffett
      - name: munger
  - name: earnings-drift      # 策略二：系统化，盈利公告后漂移
    blend: {mode: long_short}
    weight: 0.4
    models:
      - name: pead
risk:
  max_position_pct: 0.25      # 单只股票不超过权益的 25%
  max_gross_exposure: 1.0     # 不加杠杆
capital: 100000
rebalance: weekly             # daily | weekly | monthly
benchmark: SPY                # 基准，同时决定回测的交易日历
```

字段要点：

- **模版不含股票**。股票池在回测或部署时用 `--universe` 给出，同一模版可以指向任何股票。
- `blend.mode` 三选一：`long_only` 只做多、`long_short` 可做空、`dollar_neutral` 多空美元中性。做空想法必须由声明支持做空的分析师提出（pead、druckenmiller 等）。
- `models[].params` 会**原样传给模型的构造函数**。例如放宽 pead 的信号窗口（默认 4 天，公告后漂移只在窗口内出观点）：

  ```yaml
  models:
    - name: pead
      params: {signal_window_days: 10}
  ```

- 现成的策略库在 `hedge_fund/strategies/`：`fundamental-ls`、`deep-value`、`inflections`、`earnings-drift`，可以直接引用或照着写。
- 随附的第二个模版在 `~/.hedge-fund/mandates/broad-growth.yaml`：单票上限放宽到 0.35，并注释了推荐的 12~20 只宽股票池，改动依据见第 7 节对照二。
- 未知字段会被拒绝，写错会在启动前报错并指出位置。

---

## 7. 解读一次典型结果：为什么 0 orders

你跑的这条：

```
example-fund 2025-01-02 → 2025-06-27 · 121 sessions · 26 rebalances · 0 orders
return +0.0% vs SPY +5.2% · sharpe 0.00 · max drawdown 0.0%
```

含义：121 个交易日、26 次按周调仓都正常发生了，但**每一次目标仓位都是 0**，于是一笔单子都没下，净值一条直线，同期 SPY 涨了 5.2%。

打开 `research/` 里的 JSON（方法见第 5 节），看 `records[].decision.strategies[].signals[]`，原因一目了然：

- 三位 LLM 分析师全部弃权，`abstain_reason` 都是 `insufficient data: ... only 1 filed periods (need 4)`。分析师开口前要看到至少 4 个已申报的财报期。Yahoo 只提供截至今天最近约 5 个季度，站在 2025 年上半年它们一个都还没申报；那"1 个"其实是数据层在没有任何季度可用时退回的一行**当前公司概况**（`period` 为 `ttm`、日期等于决策日），不是真实季度。所以整个区间内可见期数始终是 1，分析师从头到尾弃权。
- pead 全程为 0 且无理由：它默认只认 8-K 公告记录，Yahoo 没有。
- 两条策略的 `flat_reason` 都是 `no_eligible_positions`。

这**不是 bug**，是免费数据源的覆盖边界。证据是数据链路本身是对的：2025-01-02 的标记价 AAPL 243.85、MSFT 418.58 是真实收盘价，SPY 基准曲线和交易日历都正确。

排查任何一次"没交易"的结果，都按这个顺序看：`skipped`（哪些股票没取到价格）→ 每个 signal 的 `metadata.abstain_reason` → 策略的 `flat_reason` → `clamps`（风控是否把仓位压成了 0）。第 5.3 节的"分析师观点与弃权统计"脚本一次就能把前三步看完。

### 对照：一次有交易的结果

同一模版换成近期窗口和三只股票：

```
example-fund 2026-05-12 → 2026-10-09 · 105 sessions · 22 rebalances · 15 orders
return +2.82% vs SPY +5.47% · sharpe 0.87 · max drawdown 2.71%
```

用第 5.3 节的脚本看：

- 弃权只剩 5 次，全部是 NVDA 在最早 5 个调仓日的 `only 3 filed periods`。NVDA 的财季结束在 1、4、7、10 月末，第 4 个季度比 AAPL、MSFT 晚申报，所以它晚几周才"够数"。
- 首笔成交 2026-06-16：买入 120 股 NVDA @ 207.41，正好是 NVDA 数据够数之后的第一个执行日。
- 期末持仓只有 108 股 NVDA，现金 78,063，NAV 102,825。

跑输基准的原因也能从信号里直接读出来：graham、munger 对 AAPL 持续看空，buffett 中性，而 deep-value 是**只做多**策略，看空只能等于不持有；三位里只有 buffett、munger 对 NVDA 看多，于是全部多头观点集中在一只股票上，又被 `max_position_pct: 0.25` 压在四分之一仓位；占 40% 资金的 earnings-drift 策略因当时 pead 还没有 8-K 数据而全程中性（Yahoo 财报日历接入后 pead 会在公告次日出观点，清缓存重跑结果会不同）。四分之三的钱趴在现金上，涨幅自然追不上满仓的 SPY。这不是数据问题，是这份模版在这段行情下的真实表现。

### 对照二：低投入导致的跑输（按投入资金算，选股其实赢了）

同一模版换成宽一点的股票池、更长的窗口：

```
example-fund 2025-09-30 → 2026-10-09 · 259 sessions · 54 rebalances · 31 orders
return +6.33% vs SPY +16.87% · sharpe 1.00 · max drawdown 2.79%
```

用下面的归因脚本读它：

- **钱没投出去。** 平均投入只有 15.9%，259 个交易日里只有 120 天有持仓；空仓的前四周 SPY +3.13%，基金 0。
- **但选股是赢的。** 持仓期基金 +6.33% vs SPY +13.32%，而那是平均约两成仓位下的成绩；收益除以平均投入，投入资金的回报粗算约 40%，是 SPY 的两倍多。这个对比说明问题在仓位结构，不在判断质量。
- **风控上限在替分析师做决定。** 54 次调仓触发 30 次削仓：NVDA 从 60% 削到 25% 出现 8 次，最高一次从 76% 削到 25%。
- **前半年分析师集体弃权。** `only 1~3 filed periods (need 4)` 大量出现，约 2026 年 5 月后才够 4 期；窗口起点（2025-09-30）对免费数据太早。
- **AMD 全区间 +290%，基金一股没买。** 前 32 个决策日弃权（数据不够），之后 22 个决策日三位分析师全部看空，格雷厄姆的理由是"134 倍市盈率配 2% 的 ROE，这个价格资本化的是希望而不是已被证明的盈利能力"。按价值标准没错，但这是动量股：**人格与股票池错配**，只做多策略里"看空"等于空仓。
- **pead 这次真正工作了。** 14 条以上带 8-K 依据的观点，包括 2025-10-27 对 TSLA 的一次做空（MISS，事后 TSLA 全区间 -14.8%，方向正确）。
- 据此的两个结构性改动（放宽上限到 0.35、股票池扩到 12~20 只）已写进 `~/.hedge-fund/mandates/broad-growth.yaml`。

**归因脚本。** 自动取 `research/` 里最新的一份结果，回答四个问题：钱投出去了吗、风控削了多少、选股对不对、哪条策略在空转。

```bash
poetry run python - <<'PY'
"""回测归因：投入比例、风控削仓、成交、个股走势、pead 观点、弃权、分段。"""
import collections, glob, json, os, re

path = max(glob.glob(os.path.expanduser("~/.hedge-fund/research/*.json")), key=os.path.getmtime)
data = json.load(open(path))
records, dates, m = data["records"], data["dates"], data["metrics"]
print(path)
print(f"{data['fund']} {data['start']}→{data['end']} | {data['rebalance']} vs {data['benchmark']} | {data['universe']}")
print(f"sessions={len(dates)} cycles={m['n_cycles']} orders={m['n_orders']} "
      f"return={m['total_return_pct']:+.2%} bench={m['benchmark_return_pct']:+.2%} "
      f"sharpe={m['sharpe_ratio']:.2f} maxdd={m['max_drawdown_pct']:.2%}")

nav = {r["session"]: r["nav"] for r in records}
spy = {r["session"]: r["benchmark_close"] for r in records}
ret = lambda series, a, b: series[b] / series[a] - 1

# 1) 钱投出去了吗
invested = [sum(abs(s) * r["marks"][t] for t, s in r["positions"].items()) / r["nav"]
            if r["positions"] else 0.0 for r in records]
print(f"\n平均投入 {sum(invested)/len(invested):.1%} | 最大 {max(invested):.1%} | "
      f"有持仓交易日 {sum(1 for x in invested if x > 0)}/{len(invested)}")

# 2) 风控削了多少
clamps = collections.Counter()
for r in records:
    for c in (r.get("decision") or {}).get("clamps", []):
        clamps[(c.get("limit"), c.get("ticker"), round(c.get("before", 0), 2), round(c.get("after", 0), 2))] += 1
print("\n风控削仓:")
for key, n in clamps.most_common(10):
    print(f"  {n:3d}x {key}")

# 3) 成交与首笔成交日
fills = collections.Counter()
first_fill = next((r["session"] for r in records if (r.get("executed") or {}).get("fills")), None)
for r in records:
    for f in (r.get("executed") or {}).get("fills", []):
        fills[(f["side"], f["ticker"])] += 1
print(f"\n首笔成交日: {first_fill}")
for key, n in sorted(fills.items()):
    print(f"  {n:3d}x {key[0]} {key[1]}")

# 4) 个股走势（决策日标记价，全区间）
marks = {r["session"]: r["decision"]["marks"] for r in records if r.get("decision")}
print("\n个股走势（决策日标记价，全区间）:")
for t in data["universe"]:
    series = {d: v[t] for d, v in marks.items() if t in v}
    days = sorted(series)
    if days:
        print(f"  {t}: {series[days[-1]] / series[days[0]] - 1:+.2%}")

# 5) pead 观点
views = [(r["session"], g["ticker"], g["value"], (g.get("reasoning") or "")[:60])
         for r in records for st in (r.get("decision") or {}).get("strategies", [])
         for g in st["signals"] if g["model_name"] == "pead" and g["value"] != 0]
print(f"\npead 观点 {len(views)} 条:")
for row in views[:10]:
    print("  ", row)

# 6) 弃权统计（把"股票 as of 日期"剥掉再合并计数）
reasons = collections.Counter()
for r in records:
    for st in (r.get("decision") or {}).get("strategies", []):
        for g in st["signals"]:
            md = g.get("metadata") or {}
            if md.get("abstained"):
                why = re.sub(r"\S+ as of \d{4}-\d{2}-\d{2}: ", "", md.get("abstain_reason") or "")
                reasons[(g["model_name"], why[:60])] += 1
print("\n弃权统计:")
for key, n in reasons.most_common(8):
    print(f"  {n:3d}x {key}")

# 7) 分段：空仓期 / 持仓期，以及按投入资金的粗算
print("\n分段:")
print(f"  SPY 全程 {dates[0]}→{dates[-1]}: {ret(spy, dates[0], dates[-1]):+.2%}")
if first_fill:
    print(f"  基金 空仓期 {dates[0]}→{first_fill}: {ret(nav, dates[0], first_fill):+.2%} | "
          f"SPY: {ret(spy, dates[0], first_fill):+.2%}")
    print(f"  基金 持仓期 {first_fill}→{dates[-1]}: {ret(nav, first_fill, dates[-1]):+.2%} | "
          f"SPY: {ret(spy, first_fill, dates[-1]):+.2%}")
    avg = sum(invested) / len(invested)
    if avg > 0:
        print(f"  投入资金回报粗算: {m['total_return_pct'] / avg:+.0%}（收益 ÷ 平均投入，仅作量级参考）")
PY
```

---

## 8. Yahoo Finance 免费源的能力边界

| 数据 | 付费源 Financial Datasets | 免费源 yfinance |
|------|---------------------------|-----------------|
| 日/周/月 K 线 | 全历史 | **全历史，完全可用** |
| 财报指标 | 多年、带真实 SEC 申报日期 | **只有截至今天最近约 4~5 个季度**；申报日期用"报告期 + 40 天"（年报 + 60 天）近似 |
| 盈利实际 vs 预期 | 有，含 8-K 公告日 | 有：公告日期来自 Yahoo 财报日历（美国公司的业绩发布本身就是 8-K），生成 **8-K 行**，盘后发布顺延到下一交易日；另有按季度末估计的 10-Q 行；EPS 实际/预期并推导 BEAT/MISS/MEET |
| 内幕交易 | 有 | **无，返回空** |
| 新闻 | 历史可查 | 仅近期少量 |
| 公司信息 | 含 CIK、SIC | 行业、板块、交易所、所在地 |

两个直接后果：

1. **LLM 分析师在历史回测里会大面积弃权。** 它们需要 4 个已申报期，而 Yahoo 只给最近几个季度，所以回测起点越早，可见的季度越少。
2. **pead 能正常出观点。** 8-K 行来自 Yahoo 的财报日历，可回溯约 5 年。它依赖可选包 `lxml`（已加入项目依赖，`poetry install` 会装上）；缺了它日历取不到，就只剩 10-Q 行，pead 回到不出观点的状态，日志里会有一条 warning 提示。

另外，申报日期的近似方向是保守的：季报规定 40 天内申报，实际往往更早，所以用 40 天**不会把未来泄露进回测**；年报用 60 天，早申报的公司可能有几天误差。

---

## 9. 怎样让基金真正交易起来

按推荐顺序：

**路径一：直接做模拟盘（最推荐）。** 模拟盘的"当前日期"就是今天，Yahoo 的 4~5 个季度全部已申报，LLM 分析师有足够数据，能正常给观点。这是免费源最合适的用法。

```bash
poetry run aihf paper create alpha --mandate ~/.hedge-fund/mandates/example_daily.yaml --universe AAPL,MSFT
poetry run aihf paper create 7sister_IBKR_daily --mandate ~/.hedge-fund/mandates/example_daily.yaml --universe AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA,AMD,AVGO,QCOM

# 让 alpha 模拟盘执行一次当前的行情获取、信号生成与调仓指令
poetry run aihf paper tick alpha
poetry run aihf paper tick 7sister_IBKR
```

**路径二：回测只用最近几个月的窗口。** 先查当前对某只股票能看到哪些申报期：

```bash
poetry run python -c "
from hedge_fund.data import make_data_client
with make_data_client() as c:
    for r in c.get_financial_metrics('AAPL', '2026-10-10', limit=10):
        print(r.report_period, '->', r.filing_date)
"
```

从最新往旧数，**第 4 条的 `filing_date`** 就是 LLM 分析师能开口的最早日期，回测 `--start` 不要早于它。按 Yahoo 目前通常提供 5 个季度估算，可用窗口大约是最近 4 到 5 个月，具体以上面命令的输出为准。

**路径三：pead 不需要再开备用模式。** 自 Yahoo 财报日历接入后，pead 默认的 `announcement_only=True` 就能在真实公告的下一个交易日触发。升级后请先清一次数据缓存（第 10 节），否则旧缓存里没有 8-K 行的记录会让 pead 继续沉默。`params: {announcement_only: false}` 仍然可用，但那只是滞后几周的替代，没有必要。

**路径四：切回付费源做深度历史回测。** 设 `HEDGE_FUND_DATA_PROVIDER=financialdatasets` 和对应 key，然后**清缓存**（见第 10 节）。

**路径五（改代码，不推荐）：** `hedge_fund/features/snapshot.py` 里的 `MIN_PERIODS = 4` 是分析师的最低数据要求，调低能让它们更早开口，但分析质量随之下降。**尤其不要调到 1**：在历史回测里，那唯一一行是用**今天**的公司概况填的，日期却标成了决策日，分析师会拿未来的比率去判断过去，这是时点泄露。

---

## 10. 缓存注意事项

所有数据接口的响应都缓存在 `~/.hedge-fund/cache/data/`，重跑同一区间几乎零耗时、可离线。两点要知道：

- 当日的日线不会被永久缓存，等纽约时间过了那一天才算完整。
- **缓存键里不含数据源名称。** 用付费源跑过的区间再切到 yfinance 跑同样参数，会直接命中旧缓存。切换数据源后请清一次：

  ```bash
  rm -rf ~/.hedge-fund/cache/data
  ```

**升级数据层代码后也要清一次。** 缓存会原样复用旧响应：接入 Yahoo 财报日历之前缓存的 `get_earnings_history` 结果里没有 8-K 行，不清掉，pead 就会继续沉默。

LLM 的提示词缓存在 `~/.hedge-fund/cache/llm/`，它同时是审计记录（见第 5.6 节），一般不要删；删了只是下次会重新付费问一遍。

---

## 11. 测试与验证

```bash
# 离线测试，不联网，不需要任何 key
poetry run pytest hedge_fund -q

# 真实联网取一次数，确认数据层可用
poetry run python -c "
from hedge_fund.data import make_data_client
with make_data_client() as c:
    print(type(c).__name__)                                 # 应为 YFinanceClient
    print(c.get_prices('AAPL', '2025-01-15', '2025-01-15'))  # 应返回 1 根 K 线，不能为空
    print(c.get_company_facts('AAPL'))
"
```

`hedge_fund/data/test_yfinance_client.py` 里的 27 个用例专门锁定免费源的关键行为：同一天 `start == end` 的请求必须返回那一根 K 线、网络错误必须抛 `YFinanceError` 而不是悄悄返回空、财务指标按申报日期而不是报告期过滤。`hedge_fund/data/test_client.py` 是付费源的在线冒烟测试，没有 key 时自动跳过，属正常。

---

## 12. 常见问题

**Q：回测 0 orders，净值一条直线。**
看第 7 节的排查顺序，用第 5.3 节的弃权统计脚本定位。十有八九是 LLM 分析师因数据不足弃权，解法见第 9 节。

**Q：回测前几个调仓日弃权，理由是 `only 3 filed periods (need 4)`，之后就正常了。**
窗口起点比第 4 个季度的申报日早了几周，分析师在那之前只能看到 3 期。无害，后面的决策都是正常的；想干净一点就把 `--start` 挪到第 9 节路径二查出的那个日期之后。

**Q：pead 一直是中性，从不出观点。**
先看 `signals` 里 pead 的记录有没有 `source_type: 8-K`。没有的话有两种可能：一是数据缓存是接入财报日历之前留下的，清一次 `~/.hedge-fund/cache/data` 重跑；二是日志里出现 `earnings calendar unavailable ... Missing optional dependency 'lxml'`，执行 `poetry install` 补齐依赖。两者都正常后，pead 会在每次业绩公告的下一个交易日起 4 天内给出 ±1 的观点。

**Q：pead 出了观点但回测收益还是不理想。**
这是策略本身的问题，不是数据问题。盈利公告后漂移是个弱效应，代码注释记录的早年回测夏普只有 0.33 左右，它的价值在于让那 40% 的资金有事可做，而不是单独跑赢基准。

**Q：`paper tick` 提示 `no completed SPY session after <日期>`。**
账本里最近一天之后还没有新的"已完成"交易日。已完成只算到纽约时间的昨天，所以周末、以及北京时间周二中午之前都会这样提示，等下一个交易日过去再跑即可。退出码 1 是设计行为，放在 cron 里无害。

**Q：报 `XXX_API_KEY not found`。**
某个 LLM 人格需要对应 provider 的 key，按第 3.3 节的表设置；或者先把模版改成只用 pead 验证数据层。

**Q：想对比付费源和免费源的差异。**
切换 `HEDGE_FUND_DATA_PROVIDER`，**先清缓存**，跑同一区间，对比 `research/` 里两个 JSON 的 `signals` 和 `n_orders`。

**Q：某只股票被 `skipped`。**
那只股票在决策日前 7 天内没有取到任何收盘价，通常是代码写错、已退市或停牌。Yahoo 的代码规则与美股一致，港股、A 股需要带后缀（如 `0700.HK`、`600519.SS`）。

**Q：能否同时跑多支模拟盘？**
可以，每支基金一个目录，互不影响。把 `aihf paper tick <name>` 逐个放进 cron 即可。

**Q：数据源抛 `YFinanceError`。**
这是故意的"大声失败"：Yahoo 限流或网络故障时宁可中断，也不把空数据当作"没有信号"去污染回测。等一会重跑即可，缓存会让已完成的部分秒过。
