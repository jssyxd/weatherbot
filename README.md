# weatherbot：tree6yes 尾盘共识 YES 纸面策略

`tree6yes` 是独立于 `tree4` 的新分支。它移除了主流程中“死桶后买 NO”的候选与执行路径，改为在城市当地的尾盘时段寻找**单一、稳定、可执行的 YES 共识桶**。本仓库仍然只生成本地审计记录和纸面成交估算：它不会读取钱包、私钥或账户；不会签名；不会向 Polymarket 提交、撤销或修改真实订单。即使配置为 `mode: "live"`，程序也只会记录阻断结果。

> 这是一套自动化交易研究与纸面执行工具，不是收益保证。温度合约可能因观测修正、结算规则、行情流动性、数据延迟或剧烈反转产生全部损失。应先长期验证纸面记录，再自行决定是否把任何逻辑用于独立的真实交易系统。

## 已确认的策略规则

| 项目 | tree6yes 的规则 |
| --- | --- |
| 天气 API 频率 | CheckWX METAR/SPECI 每 **120 秒（2 分钟）**完整扫描一次。任何低于 120 秒的配置都会被拒绝。 |
| 市场结构频率 | Gamma 合约结构每 15 分钟或城市当地日切换时刷新，用于获得当日温度桶和对应 `yes_token_id`。 |
| 首次暖机 | 每个待暖机 ICAO 从 AWC 拉取过去 48 小时的**已发布** METAR/SPECI，再按 IANA 当地日重建高低温基线；不请求 CheckWX 的付费 `previous` 端点。每个两分钟周期最多暖机 8 城，未尝试城市优先，避免超时站阻塞其余城市。 |
| 主源灾备 | CheckWX 当前请求异常，或成功响应未包含某个目标 ICAO 时，仅为该 ICAO 从 AWC 查询近 2 小时已发布观测；所有来源单独审计。 |
| 盘口数据 | 保留 Polymarket **公共**市场数据流，接收订单簿快照和变动，不轮询订单簿。 |
| 高温尾盘 | 仅在城市当地时间 **12:00–17:00** 考虑高温合约。 |
| 低温尾盘 | 仅在城市当地时间 **01:00–05:00** 考虑低温合约。 |
| 共识稳定门 | 同一 YES token 的最优 ask 必须连续至少 **30 分钟不低于 90¢**；跌破、缺失、订单簿过期或 token 变更即重新计时。 |
| 初始入场 | 仅一个 YES 桶同时满足稳定、时间、深度与最优 ask **92¢–98¢（含端点）**时，生成 5 股纸面 FAK 买入。多个合格桶时拒绝，避免把不一致的市场误判为单一共识。 |
| 入场限价 | 默认 `best_ask_plus_one_tick`：`min(best ask + tick size, 98¢)`。可改为 `best_ask`，但永不超过 98¢。 |
| 85¢ 风险信号 | 已成交持仓的 YES 最优 bid 跌破 **85¢** 时立即写入 `market_reversal_alert`。**仅预警和审计**，不会在缺乏实测温度证据时卖出或换手。 |
| 温度证据换手 | 后续 15 分钟天气扫描确认新日内极值越出当前 YES 桶时，按旧 YES 的可执行 bid 纸面卖出；若存在唯一、可执行的新 YES 桶，则纸面买入初始股数的 **3 倍**。 |
| 换手次数 | 每个 `城市 × 当地日期 × 高/低温方向` 最多换手 **1 次**。之后再次反转只尝试纸面退出，绝不继续追单。 |

Polymarket 的显示价格是 bid/ask 中点或最近成交价，未必是可交易价格。tree6yes 的入场只使用 YES ask 和 ask 深度，退出只使用 YES bid 和 bid 深度；不把 midpoint、Gamma 展示价格或不存在的 NO ask 当作可执行流动性。[1]

## 延迟边界

2 分钟的 CheckWX 拉取和“实测温度发生越界的瞬间即确认”不能同时做到。没有天气推送源时，实测温度越界只能在下一轮成功扫描、报文新鲜且当天历史暖机完成时得到确认，通常延迟接近一个扫描周期。公共市场数据流可以近实时提示盘口下跌，但**盘口信号不是实测温度证据**，因此已按确认要求限制为 85¢ 仅预警。[2]

## 安装与配置

```bash
git clone --branch tree6yes https://github.com/jssyxd/weatherbot.git
cd weatherbot
cp config.example.json config.json
python3 -m pip install -r requirements.txt
export CHECKWX_API_KEY='仅在启动环境中设置，不要写入 config.json'
```

`CHECKWX_API_KEY` 只从进程环境变量读取，绝不应写入 Git、`config.json`、请求 URL、审计记录或截图。首次暖机不再使用需要额外订阅权限的 CheckWX 历史接口，而是使用 AWC 的已发布 METAR/SPECI 数据。若 AWC 不可达、返回 204/异常格式、当前本地日无有效观测、订单簿缺失/过期或价格/深度不达标，策略保持失败关闭，不产生纸面入场、退出或换手。

关键配置均已写入 `config.example.json`：

```json
{
  "execution_engine": "tree6yes",
  "scan_interval_seconds": 120,
  "awc_warmup_hours": 48,
  "awc_fallback_hours": 2,
  "warmup_max_stations_per_cycle": 8,
  "market_rules_max_age_seconds": 900,
  "tail_consensus_stability_seconds": 1800,
  "tail_consensus_stable_min_price": "0.90",
  "tail_consensus_entry_min_price": "0.92",
  "tail_consensus_entry_max_price": "0.98",
  "tail_consensus_market_alert_bid": "0.85",
  "tail_consensus_rotation_multiplier": "3",
  "tail_consensus_max_rotations": 1
}
```

`execution_engine` 必须为 `tree6yes`；原有 `legacy`、`tree2` 与 `tree3` 的 NO 侧主流程不再被接受。`tail_consensus_max_rotations` 被固定为 1，`tail_consensus_rotation_multiplier` 被固定为 3，以防通过配置取消已确认的风险上限。

## 运行

```bash
# 一次性天气/市场结构扫描；因没有常驻盘口流，任何需要盘口的动作都会失败关闭。
python3 metar_observer.py once

# 方案 A：2 分钟 CheckWX 扫描 + 常驻公共盘口流 + 85¢即时仅预警。
python3 metar_observer.py run

# 查看本地状态，不请求天气或市场数据。
python3 metar_observer.py status
```

`run` 会在首次成功的市场结构刷新后订阅所有当日 YES tokens，并按官方要求每 10 秒发送应用层心跳。每逢市场结构刷新或城市当地日切换，订阅集合会重建；重连和订单簿未完成基线期间都不会产生交易意图。[2]

## 审计与状态

所有新观测写入 `data/observations/`，策略判定写入 `data/signals/`，同时追加至 `data/audit.sqlite3`。`data/state.json` 会保存 `tail_consensus`（稳定窗口）及 `tail_positions`（纸面仓位、入场价、持股数、温度证据、换手次数）。这些目录及本地密钥配置应保持在 `.gitignore` 中。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

新增测试覆盖 2 分钟最低轮询间隔、AWC 已发布 METAR/SPECI 的历史暖机、CheckWX 缺站的 AWC 灾备回退、YES token 映射、30 分钟稳定门、92¢/98¢ 边界、加一跳限价上限、多个共识桶失败关闭、85¢ 仅预警、温度确认后“卖旧 YES + 买新 YES 三倍”、一次换手上限，以及公共市场数据流字段兼容性。

## 参考资料

[1] [Polymarket：价格与订单簿](https://docs.polymarket.com/concepts/prices-orderbook)

[2] [Polymarket：实时市场数据](https://docs.polymarket.com/market-data/realtime-data)

[3] [CheckWX API 文档](https://www.checkwxapi.com/documentation/introduction)

[4] [AviationWeather.gov Data API](https://aviationweather.gov/data/api/)
