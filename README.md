# weatherbot — tree13-allno

`tree13-allno` 是 Polymarket 天气预测市场的**严格筛选 all-NO 纸面交易策略**。本分支继承 tree5/tree11 的气象观测、市场规则解析、CLOB WebSocket 盘口、审计和失败关闭思想，但删除 YES 入场语义；所有新增交易候选均明确针对 **NO token**。

> **当前状态：只允许 paper trading。** 本分支不读取私钥，不签名，不提交真实订单，不撤销真实订单，也不把本地意图假设为成交。真实执行只能在策略经过长期数据验证、稳定盈利、完成账户对账与安全审查，并满足本文末尾上线门槛后另行开发。

## 1. 策略目的与核心假设

目标是在天气市场仍有流动性时，提前超过 24 小时挂出多个低概率温度桶的 NO 限价单，赚取大量小额、近似 5% 的边角收益。策略接受一个清晰的非对称风险：若买入 NO 价格为 0.90，正确时约赚 0.10，错误时约亏 0.90；因此单笔价格为 `p` 时，忽略费用和滑点，盈亏平衡所需真实胜率约为 `p`。在 0.95 附近买入时，约 20 笔中一次归零就可能吞掉其余收益，这正是本策略必须优先控制的结构性风险，而不是靠增加下注数量解决。

策略不把“市场价格高”误认为“必然正确”。TAF 的修订、市场共识迁移以及 METAR 形成的新 running extreme 均可能推翻原先的 NO 判断。因而本策略的核心不是一次性买入，而是持续重算候选、撤销过时挂单、对账实际持仓，并在天气事实证伪时优先止损。

## 2. 已冻结的用户决策

| 决策项 | tree13-allno 规则 |
|---|---|
| GitHub 形态 | 在 `jssyxd/weatherbot` 现有仓库中创建 `tree13-allno` 分支 |
| 基线 | 综合 tree5 的气象/市场/WebSocket/审计基础与 tree11 的数据记录和 L2 证据；系统删除 YES 入场语义 |
| 交易方向 | 只买符合条件的 NO token；不无条件买所有 NO |
| 入场模式 | 只做 paper trading；候选、虚拟 GTC、虚拟撤单、虚拟 FAK 卖出均写审计 |
| 时间窗口 | 扫描发现的 D+1、D+2、D+3 及更远当地日均可处理；唯一 24 小时锚点为该城市当地日 00:00，必须仍超过 24 小时 |
| 每桶数量 | 5 股 |
| 资金上限 | 每个城市 × 当地日 × 单一方向最多 40 USDC；高温和低温相互独立，因此同城同日总上限可为 80 USDC；全局最多 1000 USDC |
| METAR | 每 2 分钟扫描；使用该城市市场的有效 METAR/SPECI/COR 观测集合维护 running high/running low |
| TAF | 接受每 15 分钟轮询；发现新版本或 AMD/COR 后立即重算和调仓 |
| 无 TAF | 先按共识前二与价格门筛选；TAF 出现后立即重新管理，不因缺少 TAF 猜测桶 |
| 真实天气优先级 | METAR 形成的新、未见过的最高/最低极值是最高优先级事实来源 |
| 共识前二 | 同一城市 × 当地日 × 方向内，按 NO best ask 从低到高排列，最便宜和次便宜桶不买 |
| 可执行盘口 | 只要存在 best ask 即视为有报价；不要求盘口能完整成交 5 股，但盘口必须新鲜且市场覆盖完整 |
| 入场价格 | 使用 6 小时可执行 best ask 时间加权均价，不使用稀疏 last-trade VWAP |
| 入场限价 | `floor_to_tick(min(0.95 × current_best_ask, (ask_TWAP_6h + current_best_ask)/2))`，且有效范围为 0.85–0.98 |
| 错误 NO 退出 | 卖出 NO，不是卖出 YES；沿用 FAK 追价，时间为 0/5/20/60/120 秒，下浮为 10%/20%/35%/60%/90% |
| 共识变化 | 未成交 GTC 撤销；已成交仓若进入共识前二则退出；退出后不得自动恢复旧仓 |
| TAF 变化 | 新 TAF 指向持仓桶时撤单并退出；TAF 移开后旧仓不买回，只允许新候选独立评估 |
| METAR 事实 | 若极值落入持有 NO 桶，说明可能成为结算桶，立即止损；若极值已经跨过桶，则该 NO 已不可能成为结算桶，继续持有到结算 |
| 测试数据 | 不允许模拟天气、错误天气或虚假 API 数据；完成后只用真实 API 做约 5 分钟纸面观测 |

## 3. 四项入场筛选

对每个城市、当地日和方向分别处理温度桶。高温与低温绝不因为方向相反而共享天气判断或资金额度。

第一，排除最新可见、且覆盖该当地日的 TAF 所指向温度桶。若该方向尚无覆盖当地日的 TAF，暂不执行这一项，但 TAF 出现后立即重算。只解析标准 `TX` 和 `TN` 极值组，并按预测实际时刻归属当地日，不从普通 TAF 温度段臆测最高或最低温。

第二，在同一方向完整市场中按 NO 可执行 best ask 从低到高排序，禁入最便宜和次便宜两个桶。ask 越低表示市场越认为该桶发生，策略不去卖市场最有共识的两个 NO。任一桶盘口缺失、过期、无法解析或市场覆盖不完整时，本轮该方向失败关闭，不猜测共识前二。

第三，只接受当前 best ask 大于等于 `0.85` 的 NO。未成交订单若后来跌破 0.85，立即撤销；已成交仓位不因价格单独退出，除非 METAR 事实证伪或其他更高优先级风险事件触发。

第四，每个合格桶必须有新鲜 WebSocket 盘口和足够的 6 小时可执行 ask 历史。当前 best ask 不能直接追高；限价取“95% ask”和“6 小时 ask-TWAP 与当前 ask 的中点”两者的较低值，再按 tick 向下取整。中点本身不是可成交价，只用于生成保守限价。历史不足、WebSocket 断线、时间戳不单调或盘口快照过期时，失败关闭，不挂单。

## 4. METAR 事实状态机

城市的有效报文集合维护当天的 `running_high` 与 `running_low`。重复事件、过期事件、无法解析时间的事件、站点不属于该城市的事件和数据源失败不改变极值。一次报文“碰到”某温度不等于当天极值已经确定，只有形成新的未见过的高点或低点才进入持仓检查。

对高温桶 `[lo, hi)`，若 `running_high ∈ [lo, hi)`，该 NO 可能成为结算桶，状态为 `FACT_INVALIDATED_EXIT`，必须尽快卖出；若 `running_high >= hi`，该桶已经被事实跨过，不可能成为结算桶，状态为 `PROVEN_IMPOSSIBLE_HOLD`，持有到结算。对低温桶同样使用 `[lo, hi)`：若 `running_low ∈ [lo, hi)` 则止损；若 `running_low < lo` 则证明不可能并持有到结算。`METAR` 是最高优先级事实来源；TAF 或市场共识不能把一个已经被 METAR 证明不可能的 NO 再次改成卖出。

## 5. 订单和持仓管理

所有入场意图为 5 股 NO、`BUY`、`GTC`。纸面状态必须区分 `PENDING_GTC`、`FILLED`、`CANCEL_REQUESTED`、`CANCELLED`、`EXIT_PENDING`、`PARTIALLY_FILLED`、`CLOSED`，并保存候选 ID、幂等键、token ID、市场规则 ID、bucket ID、限价、盘口快照哈希、数据源时间和资金占用。

真实执行阶段必须在每次撤单、卖出和重试前从交易所对账开放订单、成交、实际可卖数量和订单状态。没有可靠的交易所订单 ID、实际成交数量或可卖数量，系统必须失败关闭，绝不能按照本地意图猜测持仓。部分成交后只能对账后的剩余数量继续处理，绝不能重复卖出已成交数量。

错误 NO 的纸面退出意图使用卖出 NO 的 FAK。每次尝试都重新读取 best bid，先以当前可用 bid 为优先，并允许保护性最低价穿透多档 bid 深度；不能使用本地假设的市价成交。FAK 未成交余量自动取消，并在 0、5、20、60、120 秒阶段性重试。真实阶段任何不确定网络响应都必须先查询订单状态再决定，禁止盲目重发。

## 6. 资金限额

城市资金占用按“城市 × 当地日 × 单一方向”计算。保守口径为：已成交持仓成本，加上未成交 GTC 的最大名义金额，加上正在退出但尚未确认成交的数量成本。高温和低温各自拥有 40 USDC 上限，因为高温预测错误不代表同一天低温预测也错误。全局保守占用不得超过 1000 USDC。限额判断在创建任何新纸面订单前完成，并通过幂等键避免重复占用。

## 7. 运行频率和数据源

METAR/SPECI/COR 主扫描周期为 120 秒；CheckWX 失败或缺站时，按 tree5 的受限 AviationWeather.gov 灾备规则补齐。TAF 轮询周期为 900 秒，以 `issued`、原始报文哈希和解析后的 TX/TN 版本判断是否发生变化；AMD/COR 作为高优先级审计事件。市场规则按当地日期刷新，盘口由 Polymarket CLOB WebSocket 维护，快照过期或覆盖不完整时禁止新入场。

API key 只能通过环境变量提供，不得写入仓库、README、配置 JSON、日志或提交历史。请在本地使用 `CHECKWX_API_KEY` 环境变量；任何密钥均不应复制到代码或版本库中。

## 8. 实现边界与文件规划

当前新增的 `tree13_allno_strategy.py` 提供无网络、可回放的确定性核心，包括 all-NO 状态、TAF 版本、running extrema、四项入场门、6 小时 ask-TWAP 限价、资金上限、METAR 分类和 NO FAK 退出意图。它刻意不实现钱包、私钥、CLOB 签名、真实下单、真实撤单或真实持仓查询。

下一阶段接手者应将该核心接入现有 `metar_observer.py`、`market_adapter.py`、`websocket_market_data.py`、`local_order_book.py` 和审计存储，并补充 paper ledger。YES 入场模块、YES token 索引和卖 YES 退出路径不得重新引入 all-NO 主路径；若历史测试保留用于回归，必须明确标注为 legacy/reference，而不能被 tree13 runtime 调用。

## 9. 真实执行 PRD（后续阶段）

真实执行器需要提供以下接口：`reconcile_open_orders()`、`reconcile_fills()`、`reconcile_sellable_positions()`、`place_gtc_buy_no()`、`cancel_order()`、`place_fak_sell_no()` 和 `query_order_status()`。策略层只能提交结构化 intent，执行层负责权限、签名、幂等、超时和交易所状态确认。

每个 intent 必须含有确定性的 `idempotency_key`，至少由策略版本、城市、当地日、方向、bucket ID、事件版本和动作类型组成。首次提交前要检查本地审计与交易所状态；提交响应不确定时只允许查询后决定。撤单必须记录请求时间、交易所返回状态和最终确认时间。卖出必须记录卖出前可卖数量、订单 ID、每次 FAK 尝试、实际成交数量、剩余数量和最终关闭原因。

真实上线前至少要完成以下验收：纸面运行数周；覆盖不同城市、时区、摄氏/华氏单位、跨月跨年日期和 TAF 修订；证明所有订单都能由交易所状态重建；证明进程重启不会重复下单或超额卖出；证明 WebSocket 断线、CheckWX 限流、API 超时和部分成交均失败关闭；证明资金上限在并发城市和高低温同时运行时仍成立；证明策略扣除费用、滑点、未成交率和归零损失后仍有稳定的样本外正收益；最后才允许小额、人工确认的实盘灰度。

## 10. 观察和测试

安装依赖后运行：

```bash
python3 -m unittest discover -s tests -v
```

真实观测只允许在已配置有效 `CHECKWX_API_KEY`、不使用模拟数据、保持 `mode=observe` 或 `paper_only` 的前提下进行。建议先运行约 5 分钟，检查配置加载、当地日期、市场规则、WebSocket 快照、TAF/METAR 请求、候选筛选、资金占用、状态转换和审计日志。默认沙盒会休眠，不适合作为长期在线交易主机；5 分钟观测结果不能替代数周纸面验证。

## References

[1]: https://docs.polymarket.com/ "Polymarket Documentation"
[2]: https://docs.polymarket.com/trading/place-orders "Polymarket Place Orders"
[3]: https://docs.polymarket.com/trading/manage-orders "Polymarket Manage Orders"
[4]: https://www.checkwxapi.com/documentation/introduction "CheckWX API Introduction"
[5]: https://aviationweather.gov/data/api/ "Aviation Weather Center Data API"
