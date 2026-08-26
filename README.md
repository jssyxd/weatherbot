# METAR/SPECI Dead-Bucket Observer (tree1)

这是一个面向**已发布航空观测**的本地候选信号工具（**纯观察、零预报**）。它每 2 分钟从 [AviationWeather.gov Data API](https://aviationweather.gov/data/api/) 批量读取 METAR 与 SPECI；按每个机场的 IANA 时区把报文归入当地自然日（0:00~24:00）；每当出现**当日新的日高或日低**，立即把"前一个极值所在的温度桶"判定为已死，并对该桶输出 BUY_NO 候选信号。

> **重要边界：**METAR/SPECI 是候选观测，不是 Polymarket 最终结算源。本仓库没有钱包、私钥、签名器、余额读取器或真实交易代码。`live` 永远只产生阻断审计记录。**tree1 不再获取任何预测边缘**（TAF TX/TN、Open-Meteo ECMWF、Wunderground Forecast 均已删除）。

## 已确认范围

项目使用 49 个经合同规则与 AWC 覆盖核验的 ICAO 站（香港 VHHH 与济南 ZSJN 明确排除）。每个城市配置在 [`config/contract_cities.json`](config/contract_cities.json)，包含实际 ICAO、市场 IANA 时区、市场原生温度单位与固定机场坐标。

## 工作流

| 层 | 频率 | 数据 | 作用 |
|---|---:|---|---|
| IANA warm-up 层 | 启动、恢复及每个城市跨本地午夜后 | AWC 最近 30 小时 METAR/SPECI | 按 `reportTime UTC → ICAO IANA 时区` 重放当前当地日，只重建 high/low，**绝不生成信号**。未完成即失败关闭。 |
| 事实观测层 | 120 秒 | AWC METAR/SPECI | 去重、按 `reportTime UTC → ICAO IANA 时区` 归属当地日、更新候选日高/日低。 |
| 公开市场规则层 | 30 分钟，或任何城市跨本地午夜后 | Polymarket Gamma 公开事件元数据 | 按整个事件聚合全部温度桶，解析每个桶的 NO token ID；不读账户、不下单。 |
| 信号层 | 新报文到达时 | 本地确定性状态机 | 当日第一个报文只初始化基线；之后的每个新日高/日低立即判定前一个极值桶已死并输出候选。 |
| Paper 成交层 | 仅候选信号时 | CLOB 公开订单簿和公开费率 | 逐档模拟 FAK 的可成交份额、平均价、费用与城市—当地日总现金上限；不签名、不提交订单。 |

实时关键路径不包含 LLM。报文判重、IANA 当地日、历史重放、候选极值、死桶判定、精度保护、幂等和纸面预算均由确定性本地状态机处理。

## 死桶判定规则（tree1 核心策略）

- **日期窗口**：`market_local_date = reportTime_utc.astimezone(ZoneInfo(city_timezone)).date()`。当日 = 机场城市当地时间 0:00~24:00。`receiptTime` 只用于 AWC 延迟审计；`fetched_at` 只用于新鲜度门槛；它们**绝不**决定日归属。
- **初始化**：当地日的第一个 METAR/SPECI 只开始记录（`daily_baseline_initialized`），不交易。
- **触发**：之后的报文若出现**当日从未出现过的温度**——高于此前日高（新日高）或低于此前日低（新日低）——即触发。
- **死桶**：新日高 12→13°C ⇒ "最高 12°C" 桶 `[12,13)` 明确不可能 ⇒ 买该桶 NO；新日低 12→11°C ⇒ "最低 12°C" 桶 `[12,13)` 明确不可能 ⇒ 买该桶 NO。**所有因此新失效的桶都扫描并下注**：快速升温/降温一次性跨过 2~3 度时（如 24→27°C），`[24,25)`、`[25,26)`、`[26,27)` 三个桶同时判死，全部买入 NO。已买过的桶（幂等键）不重复买。
- **价格门**：NO 盘口 ask 严格位于 `(0.05, 0.95)`（5~95 美分，防守性门槛）才下注；**下单量固定 5 股**（交易所最小订单规模），从最优价逐档吃满 5 股。
- **当日单城市上限**：含费用总现金支出最多 20 USDC。
- **不动作**：温度在当日已见范围内波动（中间桶首次出现）不触发。

## IANA 当地日与恢复安全

启动、恢复和每个城市进入新的当地日时，扫描器先请求最近 30 小时历史报文（每批默认 10 站，避免历史响应截断），只重建该城市当前 IANA 日的候选 high/low。此 warm-up 不标记实时事件、不输出候选、不读取订单簿。没有当前当地日报文、抓取失败或重放未完成时，实时层只写入 `daily_extrema_untrusted_warmup_incomplete`，不产生候选。

市场规则有 30 分钟最大有效期，且任何城市跨入新当地日后立即强制刷新（避免午夜后最多 30 分钟用旧日规则静默压制候选）。`data/health.json` 持续记录 warm-up、扫描新鲜度与市场规则状态。

## 新鲜度门槛（AWC 延迟适配）

AWC 对整点 METAR 的发布（接收）延迟实测可达 ~490 秒。tree1 的延迟基准使用 AWC `receiptTime`：`age = fetched_at − receipt_time`（我们的真实反应延迟，通常 ~2 分钟）；另以 `max_report_age_seconds = 900`（15 分钟）作为从 `reportTime` 计的绝对兜底（`report_age > 3 × 900` 才拒绝），给整点报文留足余量，同时拒绝真正的陈旧数据。

## 温度桶候选排除与观测安全边界

温度桶按半开区间 `[lo, hi)` 处理，边界来自当前公开市场问题文本。

| 方向 | 死桶条件 |
|---|---|
| 当日最高温桶 `[lo, hi)` | 新日高 `H ≥ hi`（此前 `previous < hi`）。 |
| 当日最低温桶 `[lo, hi)` | 新日低 `L < lo`（此前 `previous ≥ lo`）。 |

开放桶（`lo=None` 或 `hi=None`，如 "X°C or below"）永远不会被本规则判死，也不会导致崩溃。

华氏合约有更严格的保护：若触发桶边界的观测只来自 METAR 正文整数 °C 换算、没有 `RMK T...` 的 0.1°C 温度组，系统输出 `f_unit_precision_ambiguous` 并拒绝候选。`COR` 报文当前只保留审计记录并输出 `correction_requires_full_day_rebuild`，不产生候选信号。

## 单实例保护

连续运行会通过 `data/observer.lock`（flock）保证同一时间只有一个扫描进程，防止旧进程在重启期间把内存里的旧状态写回、覆盖新状态（2026-08-23 toronto 审计事故的修复）。

## 安装与运行

项目只使用 Python 标准库，要求 Python 3.10+。

```bash
git clone https://github.com/jssyxd/weatherbot.git
cd weatherbot
cp config.example.json config.json
python3 metar_observer.py once
python3 metar_observer.py run   # 连续运行（默认 paper；绝不提交真实交易）
python3 metar_observer.py status
```

运行时数据保存在被 Git 忽略的 `data/` 下：`observations/` 保存新报文，`signals/` 保存每个 signal/no-signal 理由，`state.json` 保存去重、当地日极值、warm-up 审计、公开市场规则、候选桶幂等键和 paper 总现金账本，`health.json` 保存健康快照。

## 配置

| 字段 | 含义 | tree1 默认 |
|---|---|---|
| `mode` | `paper` 或 `live` | `paper`。`live` 仅写入阻断审计，不存在真实执行器。 |
| `scan_interval_seconds` | AWC METAR/SPECI 扫描间隔 | **120**（原 60）。不得低于 60。 |
| `history_hours` | 每轮 AWC 拉取的历史窗口 | **2**（原 1；覆盖整点报文 ~490s 发布延迟）。 |
| `max_report_age_seconds` | 报文时刻至抓取的最大接受延迟 | **900**（原 600）。绝对兜底为 3 倍。 |
| `failure_pause_after_seconds` | 连续扫描失败的暂停提示阈值 | 1,800。 |
| `stations_per_request` | 每轮 AWC 请求合并的合同站数 | 49。 |
| `warmup_history_hours` | 启动/恢复的历史重放窗口 | 30；至少 25，覆盖 IANA 夏令时回拨日。 |
| `warmup_stations_per_request` | 历史重放的 AWC 分批站数 | 10；最大 20。 |
| `warmup_retry_seconds` | 未完成 warm-up 的重试间隔 | 60。 |
| `market_rules_max_age_seconds` | 公开市场规则最大有效期 | 1,800。 |

## Paper 与 Live 边界

`paper` 模式只发出公开 GET 请求，且仅在候选信号出现时：读取对应 **NO token** 的 [CLOB order book](https://docs.polymarket.com/api-reference/market-data/get-order-book) 和 [fee rate](https://docs.polymarket.com/api-reference/market-data/get-fee-rate)。纸面门槛：

| 门槛 | Paper 行为 |
|---|---|
| 方向 | 仅 `BUY_NO`。 |
| 价格 | 最优 ask 严格位于 `(0.05, 0.95)`；每个纳入档位也必须满足。 |
| 下单量 | **固定 5 股**（交易所最小订单规模），从最优 ask 逐档吃满 5 股，不再按金额分档。 |
| 价格精度与最小份额 | 使用当前 `tick_size` 与 `min_order_size`；不满足即拒绝。 |
| 订单语义 | `FAK`：估算可立即成交的部分，未成交部分视作取消。 |
| 城市—当地日上限 | 含估算费用的总现金支出最多 **20 USDC**；5 股订单成本超出剩余额度即拒绝。 |
| 费用 | 使用 token 的当前 `base_fee`，按官方 taker fee 公式估算。 |

`live` 模式只输出 `blocked_no_live_executor` 审计记录，永不读取订单簿、永不提交订单。

## tree2 生产安全改进（当前仍为只读/纸面）

`tree2` 将执行模式明确为 `observe`、`paper` 和受阻断的 `live`。示例配置默认使用 `observe` + `execution_engine=tree2`；该组合会产生候选与纸面成交诊断，但**不会签名、提交或撤销任何真实订单**。`live` 仍然返回 `LIVE_EXECUTOR_DISABLED`，不能通过配置绕过。

tree2 新增 `clob_market_data.py`，对 CLOB 订单簿进行 token 一致性校验，保存 `asset_id`、`market`、`timestamp`、`hash`、`min_order_size`、`tick_size`、bids、asks 和本地抓取时间。订单簿层优先尝试批量读取，失败时才退回单 token REST 读取，并使用短期缓存。对于交易决策，`NO asks=[]` 始终记录为 `EMPTY_ASK`；NO bids 或页面显示价不会被当作 NO 买入流动性。

`execution_policy.py` 将决策拆成可审计的拒绝码，包括 `STALE_OR_MISSING_BOOK`、`EMPTY_ASK`、`ASK_OUTSIDE_LIMIT`、`DEPTH_LT_TARGET`、`DEPTH_LT_MIN_ORDER` 和 `LIVE_EXECUTOR_DISABLED`。`tree2_execution.py` 只基于带时间戳的 CLOB 快照模拟 5 股 FAK 纸面成交，并记录盘口快照、费用响应、逐档深度、平均价和总成本。

`audit_store.py` 提供 SQLite WAL 审计账本，用于后续保存信号、快照、决策和风险账本；当前 tree2 尚未将所有 legacy JSONL 历史自动迁移到 SQLite，部署时应先使用新配置运行只读观察并核对数据，再进行迁移。

### tree2 使用方式

```bash
cp config.example.json config.json
python3 metar_observer.py once
python3 metar_observer.py run
python3 -m unittest discover -s tests -v
```

真实执行器、账户余额/授权读取、签名、订单状态机、撤单和 market WebSocket 订阅仍然是后续独立阶段，不能将当前 tree2 的 `paper_fill_estimate` 解释为真实成交。生产上线前必须完成人工放行、账户级限额、kill switch、订单回报和恢复演练。
