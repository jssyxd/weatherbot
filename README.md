# weatherbot — tree5

**tree5** 在 Tree4 的 CheckWX METAR/SPECI 观察器和 Polymarket 市场规则解析之上，新增了一个**默认仅观察、可重放、失败关闭**的 TAF 温度桶策略状态机。它按每个合同城市的 IANA 时区管理当地自然日，在当地 01:00 后取得一次当日 TAF 的 `TX` / `TN` 极值组，将预测最高温、最低温映射至相应温度桶，并生成固定 **5 shares** 的买入意图。该实现不加载钱包、不读取私钥、不生成 CLOB 认证头、不向 Polymarket 提交或取消订单。

> **交易安全边界：**本分支记录的是订单意图，不是成交或持仓。任何真实下单、撤单和卖出都必须先完成账户仓位对账；若缺少可信的交易所订单 ID、已成交数量或当前可卖持仓，Tree5 必须失败关闭，绝不猜测仓位。

| 模块 | Tree5 行为 |
|---|---|
| TAF 获取 | 每城市 IANA 当地 **01:00** 后调用一次 `GET /v2/taf/{ICAOs}/short`，经 `X-API-Key` 请求头认证；失败按 900 秒间隔重试。没有覆盖当地当日的 `TX` / `TN` 则不产生入场。 |
| TAF 解析 | 只解析标准 `TX[ M ]DD/DDHHZ` 与 `TN[ M ]DD/DDHHZ` 温度极值组，按温度实际预报时刻归属当地日期，再转换为合约单位 C/F。不会从普通 TAF 温度段臆测日极值。 |
| 预测入场 | 选取唯一包含预测值的桶，对 **YES token** 的可执行 `best_ask` 取快照，并向下取整至 `tick_size` 后下浮 **5%**；订单意图为 5 shares、`BUY`、`GTC`。 |
| 实况证伪 | 每条新的 METAR/SPECI/COR 温度都会检查现有 TAF 入场。高温桶仅在观测温度 `>= bucket.hi`、低温桶仅在观测温度 `< bucket.lo` 时才被证明为不可能。仅“高于 TAF 点预测但仍在同一桶”不会错误止损。 |
| 快速退出 | 一旦桶被证明不可能，计划取消原 GTC（前提是有已对账的外部订单 ID），随后仅对已对账持仓生成 `SELL YES` 的 FAK 意图。退出在第 0、5、20、60、120 秒读取新 `best_bid`，以 **10%、20%、35%、60%、90%** 的保护性下浮报价；FAK 的未成交余量会自动取消。 |
| 时间闭合 | 高温在当地 13:00–17:00、低温在 01:00–05:00 每分钟复核。仅当“至少 1 个本地温度单位未触及桶”“最近 3 条观测出现至少 0.5 单位反转”“YES `best_bid` 较入场 ask 下跌至少 20%”三项同时成立，才给出概率性取消/退出意图。 |
| 审计 | 每个 TAF、入场、取消、FAK 追价和时间闭合判断同时写入 `data/tree5_actions/*.jsonl` 与 SQLite 的 append-only 审计表。 |

## 订单类型的必要区分

**FAK 与 GTC 不能合并为同一笔订单。**GTC 是挂在簿上直至成交或撤销的限价单；FAK 则只立即吃掉可用流动性并自动取消剩余数量。Tree5 因而使用“**入场 GTC，已证伪后退出 FAK**”的两阶段设计。[3]

对于退出，所谓“对盘口价下浮”是**最低可接受价格的保护线**，而不是强制以该价格成交。若当前最优 bid 仍有深度，卖出 FAK 会先在更优 bid 成交；较大的下浮幅度只是允许它继续扫到更深的 bid，从而提高快速出手概率。每次 FAK 前都必须重新对账实际剩余持仓，以避免部分成交后再次卖出超过持仓的数量。[3] [4]

## 安装与观察模式

```bash
git clone --branch tree5 https://github.com/jssyxd/weatherbot.git
cd weatherbot
cp config.example.json config.json
export CHECKWX_API_KEY='在此设置你的密钥'
python3 metar_observer.py status
python3 metar_observer.py run
```

`config.example.json` 默认 `tree5_enabled=false` 且 `mode=observe`。先以 `tree5_enabled=true`、`mode=observe` 持续观察至少数周，复核 TAF 的 `issued`、`forecast_time_utc`、本地日归属、盘口快照、计划订单与实际可交易流动性。供应商文档要求 METAR/TAF 至少缓存 15 分钟；因此 60 秒 METAR 扫描主要用于记录报文可用性，不能据此预先假设上游会在观测后秒级更新。[1]

## 持续运行方式

下表给出两种可行的运行方案。两者都应将 `CHECKWX_API_KEY` 和未来的 CLOB 认证材料保存在进程环境或受管密钥服务中，而不是写进仓库或 `config.json`。

| 方式 | 适合场景 | 取舍 | 成本 | 配置复杂度 |
|---|---|---|---|---|
| 托管的持续后台服务 | 希望浏览器关闭后仍每秒处理已触发退出、每分钟扫描 METAR | 省运维，适合目前 49 城与少量活跃退出；需另行配置受管密钥和持仓对账适配器 | 按托管资源用量 | 中等 |
| 自有持续在线 Linux 主机 | 已有稳定的运行主机、需要系统级进程管理和自定义网络控制 | 控制力高，可用系统服务守护；机器、网络、密钥与监控由使用者负责 | 使用现有主机的边际成本 | 较高 |

本仓库不会在默认沙箱中作为持续交易服务运行；这类会话环境可能休眠，不能承诺 5 秒、20 秒的退出时点。

## 真实执行尚未启用

Tree5 已完整实现**信号与订单生命周期决策**，但有意未实现真实执行器。要启用真实交易，后续还需在单独、安全审查过的适配器中实现下列能力：

1. 从安全密钥存储读取钱包签名材料及 CLOB L2 凭证；
2. 在每个提交、撤单和重试前拉取并对账开放订单、成交和可卖 YES 持仓；
3. 将计划 GTC/FAK 订单签名、提交、取消，并将交易所返回的 `order_id`、实际 `size_matched` 写入 `confirmed_positions`；
4. 对网络错误和不确定响应采用“查询后决定”，而不是盲目重发；
5. 在第一次真实资金动作前明确确认目标市场、最大数量、价格底线和损失后果。

CLOB 的订单价格必须遵守市场 `tick_size`，且数量必须满足 `min_order_size`；卖单的 maker amount 是 shares，taker amount 是价格乘以 shares。[3]

## 测试

```bash
sudo pip3 install eth-account
python3 -m unittest discover -s tests -v
```

Tree5 专项测试覆盖：当地 01:00 幂等调度、TAF `TX`/`TN` 与 IANA 日期映射、5% 快照入场、同桶内 TAF 突破不退出、桶边界实况证伪、0/5/20/60/120 秒 FAK 追价，以及高温/低温的时间闭合三重条件。完整测试集涵盖 84 项测试且不调用真实 API、不提交订单。

## References

[1]: https://www.checkwxapi.com/documentation/introduction "CheckWX Introduction"
[2]: https://www.checkwxapi.com/documentation/taf "CheckWX TAF API"
[3]: https://docs.polymarket.com/trading/place-orders "Polymarket Place Orders"
[4]: https://docs.polymarket.com/trading/manage-orders "Polymarket Manage Orders"
