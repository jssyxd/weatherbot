# tree5：净期望值、TAF—市场一致性与持仓风险模型

> **纸面研究范围。** 本文定义的所有入场、退出与换手都是可回放的纸面候选。实现不得加载钱包、私钥或 CLOB 认证材料，不得提交/取消订单，也不得读取账户仓位。任何真实资金动作应在一个单独的、经持仓对账和人工确认的执行适配器中完成。

## 1. 入场假说与必要门

tree5 原有的 TAF 入场逻辑仅在当地 01:00 后抓取一次 TAF。此版本将其扩展为：每个 METAR 扫描轮次之前，保存程序**当前实际看见**的最新 TAF 版本及可见时间；仅当同一方向、同一城市、同一当地合约日满足下列三项一致性门时，才产生 `PAPER_ENTRY_CANDIDATE`。

| 门 | 严格定义 | 失败结果 |
|---|---|---|
| 最新 TAF 映射 | t0 前最新可见 TAF 的 `TX`（high）或 `TN`（low）值唯一映射到当前市场规则的桶 `B_taf`。 | `BLOCKED_NO_UNIQUE_TAF_BUCKET`。 |
| 市场共识映射 | 同一 high/low 市场中，各桶以 t0 前窗口内**时间加权可执行 YES bid 中位数**排名；`B_taf` 必须排名第一。 | `BLOCKED_TAF_NOT_MARKET_LEADER`。 |
| 共识领先度 | 第一桶必须同时满足 `p1 - p2 >= 0.05` 且 `p1 / p2 - 1 >= 0.20`；当 `p2=0`，只接受 `p1 >= 0.20`。`p1/p2` 取同口径可执行 bid 中位数。 | `BLOCKED_CONSENSUS_LEAD_INSUFFICIENT`。 |

“领先超过 20%”使用**相对领先 20%且绝对领先至少 5 个百分点**的双门。单用相对值会让 `0.03` 对 `0.025` 的薄盘口误过关；单用绝对值又会在高概率桶中忽略有意义的排序差。这些数值是预注册、可扫参的研究起点，不能在同一数据区间内反复修改后宣称为 alpha。

默认观察窗口是 120 分钟，但有效性不取决于挂钟时长：至少要有 45 个有效 L2 快照、覆盖窗口 75%、最后快照距 t0 不超过 4 分钟，并保留每个桶的 L2 原始证据。30/60/120/180 分钟应作为不同的**预注册实验臂**按日期滚动验证；不可因样本不足而缩短单次窗口。

## 2. 净期望值模型

“TAF 与市场共识一致”不是信息优势本身，反而说明市场已经公开吸收了相同方向。入场仍需通过净期望值门，且概率只能来自冻结的、历史可见的校准模型，而不能等同于 TAF 点预报。

对于信号 `s`、目标数量 `Q`、可见 L2 ask 走簿后的实际可执行平均价格 `VWAP_ask`、留出集下置信概率 `p_lower`、可见成交比例或校准填单概率 `q_fill`，定义：

```text
EV_net_lower = q_fill × Q × (p_lower - VWAP_ask)
               - q_fill × entry_fee
               - expected_exit_cost
               - latency_slippage_reserve
```

其中 `expected_exit_cost` 必须由可执行 bid 深度、费用和可能的部分成交构造，不得使用 midpoint；`latency_slippage_reserve` 是信号 t0 到 post-t0 L2 entry snapshot 的实际延迟分位数损失，不能设为零。若缺少经日期滚动留出集验证的 `p_lower`、完整 L2、费用、最小订单量或入场后快照，结果为 `BLOCKED_MISSING_EV_INPUT`，而不是默认通过。

| 输入 | 合法来源 | 不允许的替代 |
|---|---|---|
| `p_lower` | 历史已冻结的城市 × 季节 × high/low × lead-time × 已触及边界距离的分层校准，带留出集置信下界。 | 当天最终温度、日终 TAF、两三次局部命中率。 |
| `VWAP_ask` | t0 后限制延迟内的完整 YES ask 档位逐档走簿。 | midpoint、最后成交价、事后低价。 |
| `q_fill` | 同类型、同限价假设下前瞻纸面 FAK/GTC 记录。 | 将未成交 GTC 当作成交。 |
| 退出成本 | 同时刻或预注册未来时间点的可执行 bid、费用、部分成交模型。 | 假定总可按 bid 或面值退出。 |

只有 `EV_net_lower > 0`、纸面成交/覆盖样本数达到预注册下限、并且最近 out-of-sample 表现没有显著转坏时，信号才标记 `PAPER_ENTRY_READY`。这是一项研究门，不是实际下单指令。

## 3. 旧 YES 的三个独立决策

旧温度桶被证伪时，退出旧 YES、获取旧桶 NO/完整对、开新 YES 是三种不同的风险与现金流，不能用“立刻反手”合并。

| 决策 | 触发与输入 | tree5 本次纸面行为 |
|---|---|---|
| 退出旧 YES | `FACT_INVALIDATED`：high 的 running high 穿越持仓桶 `hi`；low 的 running low 低于 `lo`。使用经确认的已成交数量、旧 YES bid 多档与费用。 | 生成 `PAPER_EXIT_FACT_INVALIDATED`，优先级最高。 |
| 获取旧桶 NO / 完整对 | 比较 `BUY NO` 的 ask 走簿成本、`SELL YES` 的 bid 走簿净收入、费用和已确认数量。 | 只生成 `PAPER_ROUTE_COMPARISON_REQUIRED`；不假设 NO 一定更优。 |
| 进入新 YES | 把已知“至少达到某极值”转化为所有剩余桶的条件分布与 `EV_net_lower`，而非自动选择相邻桶。 | 只有独立 TAF/共识/EV 信号才生成 `PAPER_ENTRY_CANDIDATE`。 |

## 4. 无直接气象证据时的共识逆转

为响应风险控制需求，纸面状态机也跟踪已有持仓桶 `B_hold` 的市场共识。当另一桶 `B_alt` 的可执行 bid 中位数成为第一名，并同时满足下列条件，视为 `CONSENSUS_REVERSAL`：

```text
p_alt - p_hold >= 0.05
and p_alt / p_hold - 1 >= 0.30
and B_alt / B_hold 都具有完整窗口覆盖、最近 L2 与最低深度
```

这不是逻辑结算证据，而是风险状态变化。它产生 `PAPER_EXIT_CONSENSUS_REVERSAL`，并把所有尚未成交的相同桶纸面入场标为 `PAPER_CANCEL_CANDIDATE`；**不会**自动开 `B_alt`。如果同时满足 tree5 的“未达预期高/低温 + 反转趋势”时间闭合条件，则状态升级为 `TIME_CLOSURE_AND_CONSENSUS_REVERSAL`，但仍需要独立的新仓 EV 门。

## 5. 状态优先级与换手候选

```text
FACT_INVALIDATED
    > TIME_CLOSURE_AND_CONSENSUS_REVERSAL
    > CONSENSUS_REVERSAL
    > ACTIVE
```

状态上升只能停止入场、提出取消或退出候选；不能自动把同一笔数量换入“新共识桶”。新桶必须重做完整的 TAF 版本、共识、深度、`p_lower` 和 EV 检查，并得到独立 `signal_id`。这样可避免“旧桶错误”被误当作“下一桶必然正确”。

## 6. 数据与回测要求

每条候选至少要携带：原始 METAR/SPECI/COR、observed/first-seen/fetch-complete 双时钟、t0 前最新 TAF 原文和 hash、市场规则快照、120 分钟全桶 L2、post-t0 entry snapshot、费用、纸面订单状态、最终结算。Polymarket 的实时市场流可以提供实时 book/价格事件；历史 prices-history 最低文档化粒度为 1 分钟，但不提供任意时点历史 L2 或排队位置，所以价格曲线只能做事件研究，不能单独证明 FAK/GTC 成交。[1] [2]

## References

[1]: https://docs.polymarket.com/developers/CLOB/websocket/market-channel "Polymarket Market Channel"
[2]: https://docs.polymarket.com/developers/CLOB/prices-history "Polymarket Prices History"
