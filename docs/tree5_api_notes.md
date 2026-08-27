# Tree5 API 约束核验笔记

核验日期：2026-08-27（GMT+8）

## CheckWX TAF

- 所有请求使用 `https://api.checkwx.com/v2`，建议经 `X-API-Key` HTTP 请求头认证。
- TAF 支持 `/v2/taf/{icao}/short`：返回 `icao`、`issued` UTC 和 `raw_text`。
- TAF 支持 `/v2/taf/{icao}/decoded`：返回有效期 `period.from` / `period.to` 及分段的 `forecast`；Tree5 应优先使用该端点，并只保留覆盖城市 IANA 当地当日的有效段。
- 多 ICAO 请求上限为 25。供应商文档建议 METAR、TAF 至少缓存 15 分钟；因此 Tree5 仅在每个城市当地 01:00 后进行一次 TAF 取数，并实行幂等日键与失败关闭。

## Polymarket CLOB

- `GTC` 和 `FAK` 是互斥的订单生命周期，而非可合并的单个订单类型。GTC 会留在盘口直至成交或撤销；FAK 立即与可用流动性成交并取消剩余数量。
- FAK 更适合已有仓位的快速退出；若 FAK 部分成交或不成交，Tree5 必须先查询/对账实际成交余量，才可进行下一次限价单，避免重复卖出。
- 卖单的 `makerAmount` 是股份数量，`takerAmount` 是 `price × size`；价格须满足当前 `tick_size`，数量须不少于 `min_order_size`。
- 挂单/成交必须通过经过认证的订单查询接口对账；撤单动作需要订单 ID。Tree5 记录本地意图但不应把本地记录当作交易所真实仓位。

## 设计含义

- 实现第一阶段仅新增可重放、默认 `observe` 的状态机和订单意图。真实钱包加载、CLOB 认证、签名提交、撤单与仓位查询为显式可插拔执行适配器；没有该适配器时，`live` 必须失败关闭。
- 因下单与撤单会直接影响用户资金，未来启用真实执行前须在本次对话中确认精确动作及风险边界。

## Sources

1. CheckWX Introduction: https://www.checkwxapi.com/documentation/introduction
2. CheckWX TAF: https://www.checkwxapi.com/documentation/taf
3. Polymarket Place Orders: https://docs.polymarket.com/trading/place-orders
4. Polymarket Manage Orders: https://docs.polymarket.com/trading/manage-orders
