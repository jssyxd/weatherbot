# WeatherBot 订单执行系统 — 审计报告与最小重构 PRD

> 阶段目标：把「订单执行系统」审计清楚，建立可靠的 Paper → Live 执行架构。
> 当前阶段：**只读审计已完成；重构须先经人工确认；LIVE 默认关闭。**

---

## A. 当前真实架构（文件级调用链）

```
天气数据链（本阶段不改）
  METAR/SPECI      metar_observer.py (fetch_checkwx_reports / fetch_realtime_weather_reports)
  TAF TX/TN        metar_observer.py (fetch_checkwx_taf_reports)
  IANA 当地日/极值  edge_engine.py (local_market_date / evaluate_observation)
        │
        ▼
策略产生信号（三处并存）
  tree1 死桶信号    edge_engine.evaluate_observation() -> "candidate_no_signal" (BUY NO)
  tree5 TAF 信号    tree5_strategy.plan_taf_entries() -> GTC BUY YES intent
  tree12 早布 NO    tree12_allno_strategy.plan_tree12_entries() -> GTC BUY NO intent
        │
        ▼
信号 -> 订单（没有统一 OrderIntent，各树各自造 dict）
  legacy:  metar_observer.enrich_execution() -> paper_execution.simulate_paper_fak()
  tree2:   metar_observer.enrich_execution() -> tree2_execution.simulate()
  tree12:  metar_observer.process_tree12_cycle() -> tree12_paper_fill()
        │
        ▼
风控（不独立、散落各处）
  execution_policy.decide_buy_no()  仅 tree2 使用
  paper_execution 内部价格门/城市日上限
  tree12_allno_strategy 内部 0.85-0.95 / 共识 / TAF 门
        │
        ▼
盘口（两条路，未打通）
  REST:   clob_market_data.CLOBMarketData.fetch_books()（主循环实际用的）
  WebSocket/本地盘口: websocket_market_data + local_order_book + tree3_runtime
                     —— 主循环 metar_observer.py 根本没有 import，属于未接线代码
        │
        ▼
执行（只有 Paper，无 Live executor）
  Paper: 4 套并存（见 B 问题 1）
  Live:  metar_observer.enrich_execution() 对 live 直接返回 blocked_no_live_executor
        │
        ▼
状态/仓位/审计
  tree12 有 positions/working_orders/exit_chases（dict，无统一状态机）
  audit_store.py SQLite 只记 signal_execution_decision + tree5/tree12 action 快照
  无 PnL、无统一 order_id 贯穿全程
```

**关键结论**：当前机器人有「信号 → 纸面成交」的完整纸面路径，但**没有统一的订单模型、独立风控、统一状态机、真实 WebSocket 盘口接线、PnL 结算**。Live 完全未实现（fail-closed）。

---

## B. 当前发现的问题（按严重度）

### 🔴 严重（会导致 Paper 结果不可信 / Live 无法安全切换）

1. **四套并存的 Paper 成交实现，口径不一致**
   - `paper_execution.simulate_paper_fak`（走真实 L2 深度、partial fill、费用、城市日上限）
   - `tree2_execution.simulate` + `build_fixed_five_fak`
   - `tree3_execution.simulate_local_fak`（本地盘口版，但主循环未接）
   - `tree12_allno_strategy.tree12_paper_fill`（只按 best_ask 单价成交，**不 walk depth、不支持跨档 partial fill**）
   - 证据：`grep "def simulate_paper_fak|def simulate|def build_fixed_five_fak|def tree12_paper_fill|def simulate_local_fak" *.py`

2. **没有统一 OrderIntent 模型**
   - 各树各自构造 order dict，字段不一致（`no_token_id`/`token_id`、`requested_shares`/`remaining_shares`/`shares`、`limit_price` 语义不一）。
   - 违反「Paper 与 Live 必须共用同一个 OrderIntent」。

3. **没有独立 RiskGate**
   - 唯一的风控函数 `execution_policy.decide_buy_no` 只被 tree2 使用；legacy `simulate_paper_fak` 和 tree12 各自内联风控，价格下限三套（0.40 / 0.05 / 0.85）互不相同。

4. **没有统一订单状态机**
   - 状态字符串散落：`planned_observe_only` / `working_gtc_buy_no` / `paper_fill_estimate` / `paper_filled` / `cancelled_*` / `blocked_*` / `invalidated_by_metar`…，无 `CREATED→RISK→SUBMIT→ACK→FILL/CANCEL` 语义，FAK 的「剩余取消」没有显式状态。

5. **WebSocket / 本地盘口链路未接入主循环**
   - `websocket_market_data.py`、`local_order_book.py`、`tree3_runtime.py` 全部没有被 `metar_observer.py` import（`grep` 确认）。主循环只走 REST `clob_market_data.fetch_books`，Paper 不满足「基于真实 WS 本地盘口」的要求。

### 🟠 高风险

6. **tree12 纸面成交不按深度撮合**
   - `tree12_paper_fill` 用 `best_ask` 单档价把整单 5 股记成交，不检查 ask 深度是否够 5 股、不跨档、不产生 PARTIAL_FILL。
   - 会导致 Paper 仓位虚增（真实盘口可能只有 1 股在 0.90，其余在 0.95）。

7. **FAK 剩余数量取消是隐式的**
   - `simulate_paper_fak`/`build_fixed_five_fak` 会算 partial，但返回里没有显式 `CANCEL_REMAINDER` 记录；审计无法区分「已成交多少、取消了多少」。

8. **无 PnL 与结算**
   - 全仓库无 `pnl/realized/unrealized` 实现。Position 有了，但无法从日志定位「这笔赚了还是亏了」。

9. **审计账本无统一 order_id 贯穿**
   - SQLite 记的是 signal/action 快照，没有 SIGNAL→INTENT→RISK→SUBMIT→FILL→CANCEL→POSITION 的完整生命周期。

### 🟡 一般

10. **价格/常量三套下限**：`MIN_PRICE_INCLUSIVE=0.40`（legacy）、`MIN_EXECUTION_PRICE=0.05`（tree2）、`TREE12_MIN_NO_ASK=0.85`（tree12）。
11. **重复实现**：CLOB REST 读取同时存在于 `clob_market_data.py` 与 `paper_execution.py`。
12. **tree12 paper_fill 的 `avg_price` 已加权**（上一轮已修），但 legacy 仍只报单档均价，跨档均价语义不统一。
13. **盘口采样**：`record_ws_ask_sample` 名义是 WS 采样，实际是每次扫描记录 REST `best_ask`（主循环无 WS）。

### 🟢 正常（可保留）

14. 天气数据链（CheckWX/AviationWeather/Gamma 解析、IANA 当地日、死桶判定）逻辑清晰、失败关闭正确。
15. `order_signing.py` 只做离线 EIP-712 构造/签名，运行时未接，作为未来 Live 的构件是合适的。
16. `audit_store.py` 的 SQLite append-only 记账本身可用。

---

## C. 当前 Paper 下单是否可信？

**结论：只有「legacy tree1 死桶」这条 Paper 路径基本可信；tree12 的纸面成交不可信。**

- ✅ 可信：`paper_execution.simulate_paper_fak` 会按真实 ask 深度逐档 walk、计算 partial fill、费用、均价、城市日上限（尽管 FAK 剩余取消未显式记录）。
- ❌ 不可信：`tree12_paper_fill` 只按 `best_ask` 单档价记整单成交，不检查深度，会虚增仓位。
- ❌ 不可信：整个 Paper 未走 WebSocket 本地盘口（用 REST 快照），无法复现 Live 的实时盘口行为。
- ⚠️ 无 PnL，无法从日志判断盈利。

如果现在让它 Paper 跑 3 天：legacy 死桶的「成交均价/数量」大致可信；tree12 的「成交数量」可信度低；任何 PnL 结论都不可得。

---

## D. 当前 Live 是否安全？

**NOT SAFE**（不是因为会乱下单，而是因为没有 Live 执行路径）。

- `enrich_execution()` 对 live 模式直接返回 `blocked_no_live_executor`（fail-closed，这是好的）。
- 但：无真实订单提交、无订单状态查询、无撤单、无账户/余额/持仓对账；`order_signing.py` 未接入运行时。
- 因此当前状态是「Live 完全未实现」，任何「已能切 Live」的判断都不成立。**继续 LIVE=OFF。**

---

## E. 最小重构方案（PRD）

原则：复用现有正确代码、删除重复实现、不改天气策略/数据链、不过度工程化、不建新分支、不一次性重写。

### 目标架构（胶水化）

```
Weather Signal（现有，不动）
      ↓
OrderIntent（新建，统一模型）
      ↓
RiskGate（新建，独立，Paper/Live 共用）
      ↓
ExecutionEngine
   ├── PaperExecutor（新建，基于真实 L2 深度 walk，FAK 剩余取消显式化）
   └── LiveExecutor（新建，薄封装官方 py-sdk，当前默认禁用）
      ↓
Fill → Position → PnL（新建 Position/PnL 最小实现）
      ↓
Audit（扩展 audit_store，统一 order_id 生命周期）
```

### 新建文件

| 文件 | 职责 | 说明 |
|---|---|---|
| `execution/order_intent.py` | `OrderIntent` 数据类 + `OrderStatus` 枚举 | 字段：order_id/token_id/side/price/quantity/order_type/strategy/signal_reason/created_at |
| `execution/risk_gate.py` | 独立 `RiskGate` | 价格/数量/滑点/盘口年龄/重复订单/最大仓位；Paper 与 Live 共用 |
| `execution/paper_executor.py` | 真实 L2 撮合 | 逐档 walk、partial fill、均价、FAK 剩余取消显式记录 |
| `execution/order_state.py` | 订单状态机 | CREATED→RISK→SUBMIT→ACK→PARTIAL/FILLED/CANCELLED/REJECTED/ERROR |
| `execution/position.py` | `Position` + 最小 PnL | Fill 驱动仓位与已实现 PnL |
| `adapters/polymarket/orderbook.py` | 薄封装官方 py-sdk 的 OrderBook | 只暴露 best_bid/ask/depth/age/vwap |
| `adapters/polymarket/live_executor.py` | 薄封装 py-sdk 下单/撤单/查单 | 默认禁用，需显式 `live_enabled` |

### 修改/删除

| 动作 | 文件 | 说明 |
|---|---|---|
| 保留 | `edge_engine.py`、`metar_observer.py` 天气链 | 不碰 |
| 迁移 | `paper_execution.simulate_paper_fak` 的深度撮合 | 迁入 `paper_executor.py` 复用 |
| 删除重复 | `tree2_execution.build_fixed_five_fak` / `tree3_execution.simulate_local_fak` / `tree12_paper_fill` 的各自撮合 | 统一由 `paper_executor` 撮合 |
| 接线 | `metar_observer.run_loop` | 接 WS 本地盘口（`tree3_runtime.MarketStream`）作为 Paper 盘口来源，REST 兜底 |
| 扩展 | `audit_store.py` | 记录统一 order_id 生命周期 |

### 明确不改

- 天气策略（tree1 死桶 / tree5 TAF / tree12 早布 NO 的入场过滤逻辑本身）
- CheckWX / AviationWeather / Gamma 数据链
- 不引入 Redis/Kafka/微服务/L3

---

## F. 修改顺序

```text
Step 1  execution/order_intent.py + order_state.py（统一模型/状态机）
Step 2  execution/risk_gate.py（独立风控）
Step 3  execution/paper_executor.py（真实 L2 深度 FAK 撮合 + 剩余取消显式）
Step 4  adapters/polymarket/orderbook.py（薄封装盘口）
Step 5  接线 metar_observer（WS 盘口 + 统一执行路径）
Step 6  execution/position.py + 最小 PnL
Step 7  audit_store 生命周期扩展
Step 8  删除重复执行代码（tree2/tree3/tree12 各自撮合）
Step 9  测试矩阵（见下）+ 全量回归
Step 10 Paper 本地长时间运行观察
```

---

## G. 测试矩阵（必须验证实际数量与状态）

| 情况 | 盘口 | 订单 | 预期 |
|---|---|---|---|
| 完全成交 | ask 深度足够 | BUY 5 FAK | FILLED, filled=5 |
| 部分成交 | ask 只够 3 | BUY 5 FAK | PARTIALLY_FILLED + CANCEL_REMAINDER, filled=3 |
| 零成交 | ask > limit | BUY 5 FAK | CANCELLED, filled=0 |
| 超价 | ask 超保护价 | BUY 5 | RISK_REJECT |
| 盘口过期 | book age 超限 | BUY 5 | RISK_REJECT |
| 重复订单 | 已有相同 open 订单 | BUY 5 | REJECT（防重） |
| 断线重连 | WS 断开 | 重新 Snapshot | 新 book，旧 book 失效 |
| 仓位一致 | 部分成交 3 | Position +3 | Fill 与 Position 一致 |
| Paper/Live 同模型 | 同一 OrderIntent | 过同一 RiskGate | 共用 |

---

## 参考借鉴（来自《借鉴和借鉴说明.docx》）

- 官方 `Polymarket/py-sdk`：REST + WebSocket + 下单的首选基础设施；不要自己重写签名/认证/WS 协议/心跳。
- `pascal-labs/polymarket-sdk`：参考其「Signal→Order→Fill→Position」链路与 L2 盘口、thread-safe 价格访问。
- `Caiooooo/polymarket-l2-collector`：参考 Snapshot + WS + timestamp + book levels 的盘口结构。
- 已归档的 `py-clob-client` 禁止作为新依赖。

**最终架构 = 80% 复用官方 SDK，20% 自写业务胶水（OrderIntent/RiskGate/PaperExecutor）。**
