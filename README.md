# METAR/SPECI Candidate Edge Observer

这是一个面向**已发布航空观测**的本地候选信号工具。它每分钟从 [AviationWeather.gov Data API](https://aviationweather.gov/data/api/) 批量读取 METAR 与 SPECI；按每个机场的 IANA 时区维护当地自然日的候选最高温与最低温；并将预测边缘、市场桶候选排除和纸面成交估算分开记录。

> **重要边界：**METAR/SPECI 是候选观测；TAF、Open-Meteo ECMWF IFS 0.25° 与 Wunderground Forecast 只用于预测边缘；它们均不是 Polymarket 最终结算确认来源。本仓库没有钱包、私钥、签名器、余额读取器或真实交易代码。`live` 永远只产生阻断审计记录。

## 已确认范围

项目使用 49 个经当前合同规则与 AWC 覆盖核验的 ICAO 站。每个城市配置在 [`config/contract_cities.json`](config/contract_cities.json)，包含实际 ICAO、市场 IANA 时区、市场原生温度单位、Wunderground Forecast 入口，以及固定机场坐标。

这些坐标不是城市中心，也不是运行时地理编码结果。它们来自 2026-08-22 对 AWC Data API 最近 METAR/SPECI JSON 的 `lat`、`lon`、`elev` 站点字段审计：49/49 站均有且在报文间一致。配置仍不是永久市场目录；合同规则、站点和坐标来源应定期重新核验。

范围故意不包含香港和济南：香港合同结算基于香港天文台 Daily Extract，非机场 METAR；济南 ZSJN 在核验窗口内没有 AWC METAR/SPECI 覆盖。

## 工作流

| 层 | 频率 | 数据 | 作用 |
|---|---:|---|---|
| 事实观测层 | 60 秒 | AWC METAR/SPECI | 去重、按 `reportTime UTC → ICAO IANA 时区` 归属当地日、更新候选日高/日低。 |
| 边缘配置层 | 启动时一次；之后 15 分钟 | AWC TAF → Open-Meteo ECMWF IFS 0.25° → WU Forecast | 按城市、当地市场日、最高/最低方向独立创建预测激活边缘。 |
| 公开市场规则层 | 与边缘配置刷新同周期 | Polymarket Gamma 公开事件元数据 | 按整个事件聚合全部温度桶，解析每个桶的 NO token ID；不读账户、不下单。 |
| 信号层 | 新报文到达时 | 本地确定性状态机 | 仅在边缘内出现新的候选日高/日低且紧邻桶被候选排除时生成审计信号。 |
| Paper 成交层 | 仅候选信号时 | CLOB 公开订单簿和公开费率 | 逐档模拟 FAK 的可成交份额、平均价、费用与城市—当地日总现金上限；不签名、不提交订单。 |

实时关键路径不包含 LLM。报文判重、当地日、候选极值、边缘、桶、精度保护、幂等和纸面预算均由确定性本地状态机处理。

## 边缘来源优先级

边缘按 **城市 × 当地市场日 × 最高/最低方向** 独立配置，而不是整座城市只选一个来源。

1. 若 TAF 中对应当地日有 `TX`，最高温方向使用该值；有 `TN`，最低温方向使用该值。
2. 对 TAF 缺失的方向，使用 [Open-Meteo Forecast API](https://open-meteo.com/en/docs) 的明确模型 `models=ecmwf_ifs025`、`daily=temperature_2m_max,temperature_2m_min` 和该机场固定坐标。
3. 对 ECMWF 仍缺失或失败的方向，使用同一合同站的 Wunderground Forecast 日高/日低。
4. 三层均不可用时，移除旧边缘配置、记录失败原因并 fail-closed；该方向不产生候选信号。

Open-Meteo 请求始终使用合同城市的 IANA `timezone`，并只取 `daily.time == market_local_date` 的值。C 市场请求摄氏度；F 市场请求华氏度。每个成功 ECMWF 边缘会记录明确模型 ID、请求/返回网格坐标、返回海拔、时区、UTC 偏移、原始响应哈希、端点和抓取时刻。绝不使用 `models=auto` / best-match，也不会用别的模型、前一日、气候值、相邻站或城市中心坐标静默填补。

`high_activation = forecast_high − 1 度`，`low_activation = forecast_low + 1 度`。这是**进入关注区的门槛**，不是硬上/下限；实际温度显著超出预报时仍持续评估新的候选极值。Wunderground Forecast 适配器首次从公开 Forecast 页面发现其日预报地址，之后缓存该地址；不会把页面前端 key 写入配置或 Git。

## 温度桶候选排除与观测安全边界

温度桶按半开区间 `[lo, hi)` 处理，边界来自当前公开市场问题文本，而不是固定 1°C 或 2°F 假设。

| 方向 | 候选排除条件 |
|---|---|
| 当日最高温桶 `[lo, hi)` | 当前候选日高 `H ≥ hi`。 |
| 当日最低温桶 `[lo, hi)` | 当前候选日低 `L < lo`。 |

系统先按整个事件的所有桶计算本次新增排除集合，再选离当前候选极值最近的一个桶；不会依赖 Gamma 市场数组的任意顺序。候选仍带 `candidate_invalidated_by_metar`，不能等同于最终结算确认。

华氏合约有更严格的保护：若触发桶边界的观测只来自 METAR 正文整数 °C 换算、没有 `RMK T...` 的 0.1°C 温度组，系统输出 `f_unit_precision_ambiguous` 并拒绝候选；不会把单位精度歧义写成华氏桶失效。`COR` 报文当前只保留审计记录并输出 `correction_requires_full_day_rebuild`，在完整的修订账本与当地日重放尚未实现前，不产生候选信号。

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
# 连续运行（默认 paper；绝不提交真实交易）
python3 metar_observer.py run

# 查看当前状态、边缘配置数量和模式
python3 metar_observer.py status

# 使用其他本地配置
python3 metar_observer.py once --config /path/to/config.json
```

运行时数据保存在被 Git 忽略的 `data/` 下：`observations/` 保存新报文，`signals/` 保存每个 signal/no-signal 理由，`state.json` 保存去重、当地日极值、边缘配置、公开市场规则、候选桶幂等键和 paper 总现金账本。初次启动需要形成当日基线；不应将启动前的历史报文误判为“刚到达的实时机会”。

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
| `edge_refresh_interval_seconds` | TAF、ECMWF、WU Forecast 与公开市场规则刷新 | 不得低于 900 秒。 |
| `max_report_age_seconds` | 报文时刻至抓取的最大接受延迟 | 默认 600 秒；超时只记录 `no_signal`。 |
| `failure_pause_after_seconds` | 连续扫描失败的暂停提示阈值 | 默认 1,800 秒。当前没有 live 执行器。 |
| `stations_per_request` | 每个 AWC 请求合并的合同站数 | 默认 49，单次批量请求。 |

## Paper 与 Live 边界

`paper` 模式只发出公开 GET 请求，且仅在候选信号出现时：读取对应 **NO token** 的 [CLOB order book](https://docs.polymarket.com/api-reference/market-data/get-order-book) 和 [fee rate](https://docs.polymarket.com/api-reference/market-data/get-fee-rate)。它按 ask 从低到高累计，强制执行以下纸面门槛：

| 门槛 | Paper 行为 |
|---|---|
| 方向 | 仅 `BUY_NO`。 |
| 价格 | 最优 ask 严格位于 `(0.10, 0.96)`；每个纳入档位也必须满足。 |
| 价格精度与最小份额 | 使用当前 `tick_size` 与 `min_order_size`；不满足即拒绝。 |
| 订单语义 | `FAK`：估算可立即成交的部分，未成交部分视作取消。 |
| 金额 | 每个意图的**含费用总现金预算**最多 1 USDC。 |
| 城市—当地日上限 | 含估算费用的总现金支出最多 2 USDC；剩余额度不足以容纳完整 1 USDC 意图即拒绝。 |
| 费用 | 使用 token 的当前 `base_fee`，按官方 taker fee 公式估算。 |

纸面结果命名为 `paper_fill_estimate`，并保存档位、估算份额、平均价、费用、总现金支出、订单簿时间戳/哈希和端点。它不是成交保证：订单簿可能在网络往返中变化，真实提交还需要余额、授权、签名和额外市场校验。本仓库**不具备也不会调用**这些能力。

`live` 模式始终产生 `blocked_no_live_executor`。任何未来真实执行器都需要独立审查、单独确认和重新验证；切勿把私钥或 API 凭据提交到 Git。

## 测试

```bash
python3 -m py_compile metar_observer.py edge_engine.py market_adapter.py paper_execution.py
python3 -m unittest discover -s tests -v
```

测试覆盖 49 城严格配置与固定坐标、当地日映射、TAF TX/TN 对齐、显式 ECMWF 回退与失败关闭、全事件桶聚合、相邻桶选择、华氏精度保护、COR 阻断、纸面订单簿/费用/总现金上限和 live 阻断。

## 持续运行

每分钟扫描需要长期在线的进程和持久化 `data/` 目录。可先在个人电脑后台运行以核验日志和延迟；如需 24/7，应选择提供持久磁盘、自动重启与日志监控的常驻托管环境。不要使用每分钟新建一个完整 AI 会话的定时任务；这套判断是本地确定性状态机，不需要 LLM 或 token 消耗。

## 数据来源与许可证

上游观测与 TAF 来自 [AviationWeather.gov Data API](https://aviationweather.gov/data/api/)。固定机场坐标也来自该 API 返回的站点字段。日极值回退使用 [Open-Meteo Forecast API](https://open-meteo.com/en/docs) 的显式 `ecmwf_ifs025` 模型。市场规则仅读取 [Polymarket Gamma](https://docs.polymarket.com/market-data/discover-markets) 的公开事件元数据，paper 价量估算仅读取公开 CLOB 市场数据。Wunderground Forecast 仅是末级预测边缘输入；它与合同指定的最终历史日数据用途不同。

本仓库保留源仓库的 [MIT License](LICENSE)。初始派生自 [technosheen/weatherbot](https://github.com/technosheen/weatherbot)，之后重构为候选气象观测与 paper 信号工具。
