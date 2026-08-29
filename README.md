# weatherbot — tree12-allno

**tree12-allno** 从 tree5 fork，默认 **paper / 观察模式**。

在目标城市 **IANA 当地日 00:00 之前超过 24 小时**，对符合过滤条件的温度桶 **NO** 布局目标 **5 股**，并用 TAF 修订、盘口共识、hybrid 限价与 METAR 实况持续管理未成交单与持仓。最高温与最低温都做；**49 城全开**。

## 入场过滤

1. 时间窗：仅 `now < 当地 00:00 − 24h` 可新开新桶；进入最后 24h 后不新开，已有挂单可 hybrid 改价。
2. TAF 规避：TX/TN 按预报时刻映射当地日；落入桶禁止买 NO；已持则快速 SELL NO。无覆盖不拦截。
3. 非共识前 2：同方向 NO 按 `best_ask` 升序排除最低与次低。
4. `best_ask > 0.85`。
5. 目标 5 股，允许部分成交后补足。

## 限价 hybrid

`fair = mid(6h WS VWAP, best_ask)`，`limit = min(fair, best_ask)` 对齐 tick，并保护 `>= 0.85+tick`。

## 出场

- METAR 温度落入所持 NO 桶 → FAK 阶梯 SELL NO（0/5/20/60/120s，保护性 bid 折价）
- TAF 修订打脸 → 同上
- 否则持有至结算

## 模块

- `tree12_allno_strategy.py` — 策略状态机
- `metar_observer.py` — 扫描后 `process_tree12_cycle` + 每秒 `tree12_maintenance_once`
- 复用 tree5 TAF 解析与 book 拉取

## 配置

```json
{
  "mode": "paper",
  "tree12_enabled": true,
  "tree5_enabled": true,
  "target_order_shares": "5",
  "scan_interval_seconds": 120
}
```

## 安全

默认不提交真实订单；无对账不猜仓。
