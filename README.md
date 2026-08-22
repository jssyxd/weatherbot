# METAR/SPECI Candidate Edge Observer

这是一个面向**已发布航空观测**的本地候选信号工具。它每分钟从 [AviationWeather.gov Data API](https://aviationweather.gov/data/api/) 批量读取 METAR 与 SPECI；按每个机场的 IANA 时区维护当地自然日的候选最高温与最低温；并将预报边缘与新观测到的温度桶候选排除分开记录。

> **重要边界：**METAR/SPECI 是候选观测，TAF 与 Wunderground Forecast 是预测边缘输入，三者都不是 Polymarket 的最终结算确认来源。本仓库默认只产生 paper 订单意图；没有钱包、私钥、签名器、CLOB 下单器或真实交易代码。

## 已确认范围

项目使用 49 个经当前合同规则与 AWC 覆盖核验的 ICAO 站。它故意不把下列两类城市混入严格 METAR/SPECI 范围：香港的合同结算基于香港天文台 Daily Extract，而非机场 METAR；济南 ZSJN 在核验窗口内没有 AWC METAR/SPECI 覆盖。

每个合同城市配置在 [`config/contract_cities.json`](config/contract_cities.json)，包含实际 ICAO、市场当地时区、市场原生温度单位和 Wunderground Forecast 入口。它不是永久市场目录；市场、站点与规则应定期重新核验。

## 工作流

| 层 | 频率 | 数据 | 作用 |
|---|---:|---|---|
| 事实观测层 | 60 秒 | AWC METAR/SPECI | 去重、当地日归属、更新候选日高/日低。 |
| 边缘配置层 | 启动时一次；之后 15 分钟 | AWC TAF 优先；WU Forecast 回退 | 按每城、每当地日、每方向创建预测激活边缘。 |
| 公开市场规则层 | 与边缘配置刷新同周期 | Polymarket Gamma 公开事件元数据 | 解析可交易温度桶、NO token ID 与公开市场状态；不读账户、不下单。 |
| 信号层 | 新报文到达时 | 本地确定性状态机 | 仅在边缘内出现新的候选日高/日低且紧邻桶被候选排除时生成审计信号。 |
| 执行层 | 无 | 本地 JSONL | 默认 paper 订单意图；`live` 仍为显式阻断记录。 |

### 边缘来源优先级

边缘按 **城市 × 当地市场日 × 最高/最低方向** 独立配置，而不是整座城市只选一个来源。

1. 若 TAF 中对应当地日有 `TX`，最高温方向使用该值；有 `TN`，最低温方向使用该值。
2. 缺失的方向从同一合同站的 Wunderground Forecast 获取预测日高/日低。
3. 两者均不可用时，方向标记为 `edge_source_unavailable` 并 fail-closed，不产生候选边缘信号。

`high_activation = forecast_high − 1 度`，`low_activation = forecast_low + 1 度`。这两个值是**进入关注区的门槛**，不是硬上/下限；实际温度显著超出预报时仍持续评估新的候选极值。摄氏市场保存 ℃；华氏市场保存 ℉。从 TAF ℃转换到 ℉时保留小数，绝不提前取整。

Wunderground Forecast 适配器首次从公开 Forecast 页面发现其页面使用的日预报数据地址，之后缓存该地址，避免每 15 分钟重复抓取大型动态 HTML。它不把网页内前端 key 写入配置或 Git；端点/页面解析失败时即 fail-closed。

## 温度桶候选排除

温度桶按半开区间 `[lo, hi)` 处理，边界来自当前公开市场问题文本，而不是按固定 1 度或 2 度假设。

| 方向 | 候选排除条件 |
|---|---|
| 当日最高温桶 `[lo, hi)` | 当前候选日高 `H ≥ hi`。 |
| 当日最低温桶 `[lo, hi)` | 当前候选日低 `L < lo`。 |

系统只选本次新增排除集合中**紧邻当前极值的一个桶**。例如精确 `30°C` 桶是 `[30,31)`，候选日高从 30°C 升至 31°C 时，该桶才被候选排除；读到 31°C 不会排除 `30–31°C` 这种 `[30,32)` 的双度桶。

候选信号仍带有 `candidate_invalidated_by_metar` 标签，不能等同于市场结算确认。美国整数 ℉市场特别受 METAR 整数 ℃正文和可选 `RMK T...` 十分之一 ℃精度影响；应将信号视为候选审计，不要将其表述为确定结算结果。

## 安装与运行

项目只使用 Python 标准库，要求 Python 3.10+。

```bash
git clone https://github.com/jssyxd/weatherbot.git
cd weatherbot
cp config.example.json config.json
python3 metar_observer.py once
```

`once` 会执行一轮扫描并写入本地状态。连续运行时，启动先刷新一次边缘配置，再按分钟扫描事实报文：

```bash
# 连续运行（默认 paper；不会交易）
python3 metar_observer.py run

# 查看当前状态、边缘配置数量和模式
python3 metar_observer.py status

# 使用其他本地配置
python3 metar_observer.py once --config /path/to/config.json
```

运行时数据保存在被 Git 忽略的 `data/` 下：`observations/` 保存原始新报文，`signals/` 保存每个 signal/no-signal 理由，`state.json` 保存去重、当地日极值、边缘配置、公开市场规则、候选桶幂等键和 paper 记账。初次启动需要形成当日基线；不应将启动前的历史报文误判为“刚到达的实时机会”。

## 配置

```json
{
  "mode": "paper",
  "scan_interval_seconds": 60,
  "history_hours": 1,
  "stations_per_request": 49,
  "edge_refresh_interval_seconds": 900,
  "max_report_age_seconds": 600,
  "failure_pause_after_seconds": 1800,
  "contract_cities_path": "config/contract_cities.json",
  "market_rules_path": "data/market_rules.json",
  "state_path": "data/state.json",
  "event_dir": "data/observations",
  "signal_dir": "data/signals"
}
```

| 字段 | 含义 | 约束 |
|---|---|---|
| `mode` | `paper` 或 `live` | 默认 `paper`。当前 `live` 仅写入阻断审计，不存在真实执行器。 |
| `scan_interval_seconds` | AWC METAR/SPECI 扫描间隔 | 不得低于 60 秒。 |
| `edge_refresh_interval_seconds` | TAF、WU Forecast 与公开市场规则刷新 | 不得低于 900 秒。 |
| `max_report_age_seconds` | 报文时刻至抓取的最大接受延迟 | 默认 600 秒；超时只记录 `no_signal`。 |
| `failure_pause_after_seconds` | 连续扫描失败的暂停提示阈值 | 默认 1,800 秒。 |
| `stations_per_request` | 每个 AWC 请求合并的合同站数 | 默认 49，单次批量请求。 |

## Paper 与 Live 边界

`paper` 模式只在信号 JSONL 中写入 `paper_order_intent_pending_price_gate`。它记录用户确认的 1 USDC 名义金额、每城市每当地日 2 USDC 上限、FAK、BUY_NO 和裸价格 `<0.96` 的目标约束，但**不会**读取 CLOB 订单簿、验证价格、模拟成交、加载凭据或提交订单。

`live` 模式当前产生 `blocked_no_live_executor` 记录。这是刻意的安全边界：即使用户以后设置了 `mode=live`，本版本仍不含真实下单能力。任何后续真实执行器都需要单独审查价格/费用、订单簿、余额、账户权限、失败暂停和明确的逐次确认流程；切勿把密钥提交到 Git。

## 测试

```bash
python3 -m py_compile metar_observer.py edge_engine.py market_adapter.py
python3 -m unittest discover -s tests -v
```

测试覆盖 49 城严格配置、当地日映射、TAF TX/TN 对齐、相邻桶选择、候选信号幂等、paper 城市日上限和 live 阻断。

## 持续运行

每分钟扫描需要长期在线的进程和持久化 `data/` 目录。可先在个人电脑的后台进程中运行以核验日志和延迟；如需 24/7，使用能够提供持久磁盘、自动重启与日志监控的常驻托管环境。不要使用每分钟新建一个完整 AI 会话的定时任务；这套判断是本地确定性状态机，不需要 LLM 或 token 消耗。

## 数据来源与许可证

上游观测与 TAF 来自 [AviationWeather.gov Data API](https://aviationweather.gov/data/api/)。当前市场规则仅读取 [Polymarket Gamma](https://docs.polymarket.com/market-data/discover-markets) 的公开事件元数据。Wunderground Forecast 仅作为边缘预测输入，和市场结算使用的历史 Daily Observations 数据用途不同。

本仓库保留源仓库的 [MIT License](LICENSE)。初始派生自 [technosheen/weatherbot](https://github.com/technosheen/weatherbot)，之后重构为候选气象观测与 paper 信号工具。
