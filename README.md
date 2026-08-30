# weatherbot — tree12-allno

**tree12-allno** 从 tree5 fork，默认 **paper / 观察模式**。

在目标城市 **IANA 当地日 00:00 前 (18, 30] 小时** 窗口内，对符合过滤条件的温度桶 **NO** 布局目标 **5 股**，并用 TAF 修订、盘口共识、hybrid 限价与 METAR 实况持续管理未成交单与持仓。最高温与最低温都做；**49 城全开**。

## 入场过滤

1. 时间窗：仅当 `18h < hours_before_local_00:00 ≤ 30h` 可新开新桶；窗口外不新开，已有挂单可 hybrid 改价。
2. TAF 规避：TX/TN 按预报时刻映射当地日；落入桶禁止买 NO；已持则快速 SELL NO。无覆盖不拦截。
3. 非共识前 3：同方向 NO 按 `best_ask` 升序排除最低、次低与第三低。
4. `0.85 ≤ best_ask ≤ 0.95`（含端点）。
5. 目标 5 股，允许部分成交后补足。

## 限价 hybrid

`fair = mid(6h WS ask VWAP, best_ask)`，`limit = min(fair, best_ask)` 对齐 tick，并保护在 `[0.85, 0.95]` 区间内。

## 出场

- METAR 温度落入所持 NO 桶 → 快速 FAK 阶梯 SELL NO（0/3/8/15/30s，滑点 0.03→0.30，硬底 0.05）；**paper 立即结算并 release 资金**
- TAF 修订打脸 → 同上
- 否则持有至结算

## 模块

- `tree12_allno_strategy.py` — 策略状态机（自包含：本地实现 TAF TX/TN 解析、桶包含、UTC 工具，不 import tree5 代码）
- `metar_observer.py` — 扫描后 `process_tree12_taf_entries`（独立抓 TAF）+ `process_tree12_cycle` + 每秒 `tree12_maintenance_once`
- 复用 tree5 的 CLOB book 拉取（`fetch_tree5_books` 只是公共只读盘口客户端，不含 tree5 策略）

## 配置

```json
{
  "mode": "paper",
  "tree12_enabled": true,
  "tree5_enabled": true,
  "target_order_shares": "5",
  "scan_interval_seconds": 120,
  "tree12_taf_fetch_local_hour": 1,
  "tree12_taf_retry_seconds": 900
}
```

## 安全

默认不提交真实订单；无对账不猜仓。
