# WeatherBot 订单执行系统审计报告（独立审计员 A · 只读）

> 审计对象：`/home/da/桌面/tree12-allno`（tree12-allno）
> 审计日期：2026-08-29
> 审计方式：只读，逐文件追踪调用链，未修改任何代码/测试/配置。
> 证据格式：`文件:函数:行号`

---

## 0. 当前真实配置（config.json）

| 键 | 值 | 含义 |
|---|---|---|
| `mode` (L2) | `"paper"` | 纸面模式 ✅ |
| `execution_engine` (L3) | `"legacy"` | 走 `simulate_paper_fak`，不是 tree2/tree3 |
| `execution_order_type` (L4) | `"FAK"` | ⚠️ 被 tree12 忽略（tree12 用 GTC） |
| `market_ws_enabled` (L12) | `false` | ⚠️ WebSocket 本地盘口**关闭** |
| `tree5_enabled` (L32) | `false` | tree5 关闭 |
| `tree12_enabled` (L33) | `true` | **tree12 是唯一活跃策略路径** |

**结论先行：当前实际下单路径是 tree12，而 tree12 的纸面成交是「把 best_ask 直接当全部成交」的假成交。活跃路径根本不用 FAK（用 GTC），也根本不用 WebSocket 本地盘口（用 REST 快照）。**

---

## 1. 五个重点问题的逐条回答

### Q1. Strategy 在哪产生信号？信号如何变成订单？

**有两条并行、互不共享的链路，都在 `metar_observer.py::scan_once`（L1049）里跑：**

**链路 A —— METAR 边沿信号（legacy/tree1，配置 `execution_engine="legacy"`）：**

```text
metar_observer.py::scan_once (1049)
  → edge_engine.py::evaluate_observation (198)
       └ 当日极值越过桶边界时产出 "candidate_no_signal" (284-294)
  → metar_observer.py::enrich_execution (871)
       └ paper_execution.py::simulate_paper_fak (97)   # legacy
       └ tree2_execution.py::simulate (111)            # 仅当 engine=tree2
```

这条链路只产生**只读成交估算**，不建仓、不落订单状态机、不改 tree12 持仓。

**链路 B —— tree12 早盘 NO 布局（当前活跃路径，`tree12_enabled=true`）：**

```text
metar_observer.py::scan_once (1049)
  → process_tree12_taf_entries (971)
       └ tree12_allno_strategy.py::record_tree12_taf_reports (119)   # TAF 极值 → state.tree12.taf_forecasts
  → process_tree12_cycle (1005)
       └ collect_tree12_book_token_ids (tree12:899)
       └ fetch_tree5_books (metar_observer:508)   # REST POST /books（不是 WS）
       └ tree12_allno_strategy.py::run_tree12_cycle (926)
            → plan_tree12_entries (559)           # ★ 信号在此产生
            → tree12_paper_fill (804)             # ★ 信号在此变成「成交」（假成交）
```

**tree12 的「信号」判定逻辑**（`plan_tree12_entries` L572-625）：对每个城市/当地日/方向，遍历所有 NO 桶，同时满足以下条件才买入 NO：

1. 距当地日 > 24h（`allow_new_entries` L200，L579 判定）
2. 该桶**不在**市场共识 top2 最低 ask 里（`consensus_top2_token_ids` L355，L610 判定）
3. 该桶**不在** TAF 预测桶里（`taf_forbidden_bucket_ids` L379，L607 判定）
4. best_ask ∈ [0.85, 0.95]（L613）

满足后写 `state.tree12.working_orders[key]`（L640-661，`order_type="GTC"`，`status="working_gtc_buy_no"`），并在 paper 模式下立即调用 `tree12_paper_fill`（L664）。

---

### Q2. FAK 是否真实现「成交多少算多少 → 剩余立即取消」？

**答案：活跃路径（tree12）根本没有实现 FAK，而且存在把 best_ask 当全部成交的假成交。**

证据分三层：

**(a) tree12 活跃路径：不是 FAK，是 GTC 假成交（🔴 严重）**

- `tree12_allno_strategy.py:653`：`"order_type": "GTC"` —— tree12 建的是 GTC，不是 FAK。config 里的 `execution_order_type="FAK"`（config.json L4）被完全忽略。
- `tree12_allno_strategy.py::tree12_paper_fill`（L804-850）：接收 `fill_price=ask`（即 best_ask，由 L664 传入 `ask`），**没有任何盘口深度遍历、没有 partial fill、没有逐档均价**，直接把 `need = target - 持仓` 的全部股数按 best_ask 一次填满。
- `tree12_allno_strategy.py::paper_fill_working_order`（L853-896）：`filled = min(remaining, fill_shares)`（L865），单一 `fill_price` 做加权均价（L892）——因为只填一档，均价就是 best_ask。

这就是审计要素 §五 明确禁止的：

```text
if ask <= price:
    filled = requested_size
```

的假成交。

**(b) FAK 模拟器（legacy/tree2）实现了逐档撮合，但「剩余取消」语义错误（🟠 高）**

- `paper_execution.py::simulate_paper_fak`（L147-167）：**确实逐档 walk**、算均价（L202）、算手续费。但 `L169 if filled_shares < min_order_size → 返回 rejected`。即：盘口只有 3 股可成交时，返回的是「拒绝」，**不是** `PARTIALLY_FILLED + CANCEL_REMAINDER`。
- `tree2_execution.py::build_fixed_five_fak`（L84-97）：同样逐档 walk；`tree2_execution.py::simulate` L151 在 `executable_shares < min_order_size` 时返回 `paper_fill_rejected_below_min_order_size`，也不产生剩余取消状态。

**(c) 唯一正确命名「部分成交 + 剩余取消」的是 tree3，但它没接线（🟠 高）**

- `tree3_execution.py::simulate_local_fak`（L44-45）：有 `"status": "paper_fill_partial_fak", "decision_code": "PARTIAL_FILL_REMAINDER_CANCELLED"`，这是唯一正确表达 FAK 语义的地方。
- 但 `metar_observer.py::enrich_execution` L877-878 对 `engine=tree3` 直接 fail-closed 返回 `"tree3_local_book_required"`；且 `config.json` L12 `market_ws_enabled=false`，主循环**从未 import** `websocket_market_data.py` / `tree3_runtime.py`（grep 已确认）。所以 tree3 的正确 FAK 撮合在真实运行中**从不执行**。

---

### Q3. Paper 如何模拟成交？（是否用真实 L2 深度 / partial fill / 均价 / 剩余处理）

**活跃路径（tree12）的纸面成交 = REST 快照 + best_ask 全额假成交，没有任何 L2 深度。**

逐项对照审计要素 §三：

| 要求 | 实际情况 | 证据 |
|---|---|---|
| 读 WebSocket 本地 OrderBook？ | ❌ 否。用 REST `/books` 快照 | `metar_observer.py::fetch_tree5_books` L513 → `CLOBMarketData.fetch_books`（`clob_market_data.py:145`，POST `/books`） |
| 用 Best Bid/Best Ask？ | ✅ 只用 best_ask | `tree12_allno_strategy.py::best_ask_of` L287；`plan_tree12_entries` L598/L664 |
| 用真实盘口深度？ | ❌ 否。tree12 不看 depth | `tree12_paper_fill` L804-850 无 depth 参数 |
| 算实际可成交数量？ | ❌ 否。直接 `need=target-持仓` | `plan_tree12_entries` L604、L664 |
| 算成交均价？ | ⚠️ 只算「单一 best_ask 价」的伪均价 | `paper_fill_working_order` L892（单档，`filled` 全按 `fill_price`） |
| 支持 Partial Fill？ | ❌ 否 | `paper_fill_working_order` L865 `min(remaining, fill_shares)`，但 `fill_shares` 恒等于 `need` |
| 处理剩余数量？ | ⚠️ 仅 `remaining_shares` 减到 0，无「剩余取消」语义 | L868-871 |
| FAK 真实现？ | ❌ 否（见 Q2） | — |

**补充：WebSocket 本地盘口其实「写好了」，但没接线（🟢 实现正确 / 🔴 未使用）：**

- `local_order_book.py`：`apply_book`（L80）正确重建盘口、`apply_price_change`（L100）正确处理 `size==0` 删除价格档（L115-116）、`invalidate`（L151）断线失效、`is_fresh`（L167）记录年龄。实现本身是对的。
- `websocket_market_data.py::MarketStream`（L21）：book/price_change/tick_size 事件分发正确。
- 但：`config.json` L12 `market_ws_enabled=false`；`metar_observer.py` 全文**未 import** 这两个模块。`market_ws_enabled` 只在 `metar_observer.py:286` 被读进一个 dict，从未驱动任何 WS 连接。

**结论：当前 Paper 成交 = 拿一个 REST 快照，把 best_ask 当整单成交价。既不满足「基于真实 WS 本地盘口」，也不满足「逐档撮合 / partial fill / 剩余取消」。**

---

### Q4. 订单状态机是否完整？

**答案：不存在正式状态机。** 全项目是**散落的字符串状态**，没有 `CREATED/RISK_REJECTED/SUBMITTING/SUBMITTED/ACKED/PARTIALLY_FILLED/FILLED/CANCELLED/REJECTED/ERROR` 枚举，也没有 `OrderStatus` 类型。

实际出现的状态字符串（互不统一）：

- tree12 工作单：`working_gtc_buy_no`（L642）、`filled`（L871）、`blocked_insufficient_capital`（L836）、`cancelled_filter_break`（L696）、`cancelled_metar_exit`（L743）、`cancelled_taf_exit`（L786）
- tree12 退出梯：`active`/`awaiting_reconciliation`/`done`（`start_tree12_exit_chase` L441、`plan_tree12_due_exit_faks` L480/L489）
- 纸面 FAK：`paper_fill_estimate`/`paper_fill_rejected_*`/`paper_fill_unavailable`（`paper_execution.py` L195、L110 等）

问题：

1. **没有 ACKED**（因无真实下单，也就没有交易所回报）。
2. **没有 PARTIALLY_FILLED**（活跃路径只会在 `filled` 与 `working_gtc_buy_no` 之间跳）。
3. **没有 CANCELLED/REJECTED/ERROR 统一枚举**，全用中文状态串 + `blocked_*`/`rejected_*`/`cancelled_*` 前缀拼接。
4. 状态存于 `state.tree12.working_orders[key]["status"]`（一个 dict 字段），不是独立类型，任何模块都能改。

---

### Q5. Paper 与 Live 是否共用同一 OrderIntent 模型？

**答案：没有。而且当前根本不存在 Live 执行器，也就没有「共用」可言。**

1. **没有统一的 OrderIntent**。项目里有 **4 套互不相同的「订单」形状**：
   - `tree2_execution.py::FAKIntent`（L24，dataclass）
   - `tree12_allno_strategy.py` 的 `working_orders`（L640-661，ad-hoc dict，GTC）
   - `tree5_strategy.py` 的 `entries`（L321-324，ad-hoc dict，`external_order_id: None, confirmed_filled_shares: "0"`）
   - `order_signing.py::UnsignedOrder`（L66，EIP-712 签名用）
   没有 `OrderIntent` / `Order` / `Fill` / `Position` / `OrderStatus` 的统一模型（PRD.md 里写的是「目标架构」，尚未实现，`docs/PRD.md` 是未跟踪文件）。

2. **没有 Live 执行器**（这是「好事」，见 §5）：
   - `metar_observer.py::enrich_execution` L881-886：`mode=live` 直接返回 `"blocked_no_live_executor"`。
   - `execution_policy.py::decide_buy_no` L40-41：`mode=live` 返回 `"LIVE_EXECUTOR_DISABLED"`。
   - `order_signing.py`：只能离线构造 + EIP-712 签名（L151/L189），**没有 HTTP 客户端、没有提交订单的代码**。
   - `clob_market_data.py`：只有 GET `/books`、POST `/books`、GET `/fee-rate`，**没有 POST `/order` 或 DELETE `/order`**。
   - 全仓 grep 无 `submit_order` / `place_order` / `send_order` / 私钥加载。

   因此 Paper 与 Live「共用 OrderIntent」的前提（存在 Live）当前不成立。

---

## 2. 问题分级清单

### 🔴 严重

| # | 问题 | 证据 |
|---|---|---|
| S1 | **假成交**：tree12 纸面成交把 best_ask 当全部成交，无深度/无 partial fill | `tree12_allno_strategy.py::tree12_paper_fill` L804-850（调用点 `plan_tree12_entries` L664，`fill_price=ask`）；`paper_fill_working_order` L865/L892 |
| S2 | **无统一 OrderIntent + 无订单状态机**，4 套订单形状并存，Paper/Live 无法对齐 | 见 Q4/Q5 |
| S3 | **FAK 未真正实现于活跃路径**：tree12 用 GTC（L653），config 的 FAK 被忽略；FAK 模拟器把「不足最小单量」当拒绝而非 partial+cancel | `tree12_allno_strategy.py:653`；`paper_execution.py:169`；`tree2_execution.py:151` |
| S4 | **WebSocket 本地盘口完全未接线**，Paper 违反「必须基于真实 WS 本地盘口」 | `config.json:12 market_ws_enabled=false`；`metar_observer.py` 未 import `websocket_market_data`/`tree3_runtime`（grep 确认） |

### 🟠 高风险

| # | 问题 | 证据 |
|---|---|---|
| H1 | **5 套执行引擎并存、语义分歧**（legacy/tree2/tree3/tree12/tree5），同一 `scan_once` 里同时跑 legacy `simulate_paper_fak`（链路 A）与 tree12 `tree12_paper_fill`（链路 B），**共享同一 `reserve()` 现金账本但各自记账** | `metar_observer.py:1122`（链路 A）与 `:1143`（链路 B）；`paper_capital.py::reserve` L41 |
| H2 | **退出 FAK 只计划、从不撮合**：tree12 退出梯跑完 5 次 attempt 就 `awaiting_reconciliation`，从无 fill/持仓减少 | `tree12_allno_strategy.py::plan_tree12_due_exit_faks` L457-541（L523 `planned_observe_only`，L480/L539 `awaiting_reconciliation`） |
| H3 | **tree12 无盘口年龄检查、无滑点检查**（对比 `execution_policy.decide_buy_no` 有 `max_book_age_seconds=3.0` 和 `max_slippage`，但 tree12 从未调用它） | `tree12_allno_strategy.py::plan_tree12_entries` L559-666 无 age/slippage 门槛；`decide_buy_no` L26/L56/L59 |
| H4 | **费用模型不一致**：tree12 用固定 5% 手续费，legacy/tree2 用真实 fee-rate 端点 | `tree12_allno_strategy.py:27 TREE12_PAPER_FEE_RATE=0.05`（L800/L833 使用）vs `paper_execution.py::_base_fee_rate` L86（`base_fee/10000`，通常 <1%） |

### 🟡 一般

| # | 问题 | 证据 |
|---|---|---|
| M1 | 全程无 PnL 计算 | 全仓 grep 无 `pnl`/`profit`/`realized` 计算字段（`Position` 只有 `shares`/`avg_price`，`paper_fill_working_order` L872-895） |
| M2 | `execution_order_type="FAK"` 是死配置 | `config.json:4`；tree12 硬编码 GTC（`tree12_allno_strategy.py:653`） |
| M3 | `min_order_size` 与 FAK「成交多少算多少」语义混淆（最小单量应约束下单量，不约束部分成交量） | `paper_execution.py:169`；`tree2_execution.py:151` |
| M4 | legacy `simulate_paper_fak` 只估算不建仓，导致「现金被 reserve 但无持仓/PnL」 | `paper_execution.py:185 reserve`；无对应 position 写入 |

### 🟢 正常

| # | 点 | 证据 |
|---|---|---|
| G1 | Live 被安全阻断，无钱包/私钥/下单/签名在任何主循环路径 | `metar_observer.py:881-886`；`execution_policy.py:40-41`；`order_signing.py`（纯离线）；`clob_market_data.py`（无 /order 端点） |
| G2 | `order_signing.py` 刻意离线、不加载凭据 | `order_signing.py:1-6` |
| G3 | WS 本地盘口 + LocalOrderBook 实现本身正确（book 重建/size=0 删档/断线失效/新鲜度） | `local_order_book.py:80/100/115-116/151/167`；`websocket_market_data.py:54-85` |
| G4 | `audit_store.py` SQLite append-only 审计台账可用且被调用 | `audit_store.py:34-48`；`metar_observer.py:1154-1167` |
| G5 | 风控方向 fail-closed（缺失盘口/过期盘口/超价一律拒绝，不猜） | `execution_policy.py:42-68`；`tree12_allno_strategy.py:613-619` |
| G6 | 有重复下单保护（tree12 用 `working` 状态跳过/改价，legacy 用 `handled_candidate_buckets`） | `tree12_allno_strategy.py:626-639`；`edge_engine.py:277-283` |

---

## 3. Paper 跑 3 天：哪些可信、哪些不可信？

**基本可信：**

- 天气数据链（METAR/SPECI/TAF 解析、IANA 当地日、每日极值、warm-up）——审计要素要求不改这条链，且本次未发现明显缺陷。
- 市场规则（Gamma bucket/token 结构）刷新。
- tree12 的**入场筛选**（24h 提前、非 top2 共识、非 TAF 预测、ask∈[0.85,0.95]）。
- 审计日志（SQLite + JSONL 双写）会完整记录每一次决策。

**不可信（直接污染结论）：**

- **成交价格**：全部按 best_ask 记，实际挂单会因深度/滑点显著偏离。
- **成交数量**：永远「满 5 股」，从不 partial，等于假设盘口无限深度。
- **持仓与现金**：假成交让持仓「看起来」建好了，但真实市场可能 1 股都买不到。
- **退出/PnL**：退出 FAK 从不成交，没有任何平仓，因此任何「策略盈利/亏损」结论都无从谈起。
- **手续费**：固定 5%，是真实费率（通常 <1%）的数倍到数十倍，虚高成本。

**一句话：3 天跑下来，只有「哪些城市哪些桶会被 tree12 选中」和「天气链是否稳定」可信；成交数量、成交价格、持仓、盈亏全部不可信。**

---

## 4. Live 安全判定

## `NOT SAFE`（当前不可上线，但原因不是「会乱下单」）

准确说，当前 Live **根本无法触发**（无钱包、无私钥、无签名、无下单 HTTP、无取消 HTTP，`mode=live` 会被 `enrich_execution` L881 和 `decide_buy_no` L41 双重阻断）。

但若未来接入 Live，**以当前状态接入是 NOT SAFE**，原因：

1. 没有统一 `OrderIntent` 供 Paper/Live 共用（Q5），无法「先 Paper 验真、再切 Live」。
2. 没有 `RiskGate` 独立层：tree12 的风控散落在策略里（`plan_tree12_entries`），且**无滑点、无盘口年龄、无最大仓位检查**（对比 `decide_buy_no` 才有，但 tree12 不用它）。
3. 订单状态机缺失，无法可靠判断 `ACKED/PARTIALLY_FILLED`，也无法对账（reconcile）。
4. `order_signing.py` 是离线正确，但**没有对应的提交/查询/取消 HTTP 层**，接 Live 需要按官方当前 CLOB API 文档新增（审计要素 §二十一明确禁止凭旧博客猜接口）。

**结论：在补齐 OrderIntent + RiskGate + 订单状态机 + 真实 WS 盘口 + 官方 CLOB 下单层之前，Live = NOT SAFE。**

---

## 5. 最小修改方案（审计后建议，未执行）

> 遵守审计要素 §十六~§十八：不重写、不改天气策略、不新建 tree 分支、不为过测试改业务。

| 文件 | 问题 | 修改内容 | 风险 |
|---|---|---|---|
| 新建 `execution/order_intent.py` | 无统一模型 | `OrderIntent` dataclass + `OrderStatus` 枚举（字段按审计要素 §七） | 低，纯新增 |
| 新建 `execution/risk_gate.py` | 风控散落 | 独立 `RiskGate`：价格/数量/滑点/盘口年龄/重复单/最大仓位，Paper/Live 共用 | 低，纯新增 |
| 新建 `execution/paper_executor.py` | 假成交 | 真实 L2 逐档撮合：partial fill + 均价 + FAK 剩余显式 `CANCEL_REMAINDER`（复用 `tree2_execution.build_fixed_five_fak` 的正确 walk + `tree3_execution` 的正确 FAK 语义） | 中，替换 `tree12_paper_fill` 假成交 |
| 新建 `execution/order_state.py` | 状态机缺失 | CREATED→RISK→SUBMIT→ACK→PARTIAL/FILLED/CANCELLED/REJECTED/ERROR | 低 |
| `metar_observer.py` | WS 未接线 | 接 `tree3_runtime.MarketStream` 为 Paper 盘口来源（REST 兜底），把 `market_ws_enabled=true` 真正生效 | 中，需处理断线重连/stale book |
| `tree12_allno_strategy.py` | 假成交 + GTC 硬编码 | 入场改为产生 `OrderIntent`（FAK），交给 `RiskGate` + `paper_executor`，删除 `tree12_paper_fill` 假成交 | 中，核心改动 |
| `tree2/tree3/tree5_execution` | 重复实现 | 统一由 `paper_executor` 撮合，删除各自撮合 | 低 |
| `audit_store.py` | 无 order_id 生命周期 | 记录统一 `order_id` 的 SIGNAL→INTENT→RISK→SUBMIT→ACK→FILL→CANCEL→POSITION→PNL | 低 |

---

## 6. 建议修改顺序

```text
Step 1  OrderIntent + OrderStatus（统一模型）
Step 2  RiskGate（独立风控，Paper/Live 共用）
Step 3  PaperExecutor（真实 L2 深度 FAK 撮合 + 剩余取消显式）
Step 4  OrderStateMachine（状态机）
Step 5  接线 WS 本地盘口（market_ws_enabled 真正生效）
Step 6  tree12 改为产出 OrderIntent（删假成交）
Step 7  Position + 最小 PnL
Step 8  AuditStore 生命周期扩展
Step 9  Paper 长跑验证（对应审计要素 §十八 的 10 个测试 + §十九 的成交矩阵）
```

---

## 附：关键调用链速查（文件:函数）

```
信号产生（tree12 活跃）:
  tree12_allno_strategy.py::plan_tree12_entries (559)
     └ list_no_buckets (321) / consensus_top2_token_ids (355) / taf_forbidden_bucket_ids (379)

信号→订单:
  tree12_allno_strategy.py::plan_tree12_entries (559) → working_orders (640-661)

订单→（假）成交:
  tree12_allno_strategy.py::tree12_paper_fill (804) → paper_fill_working_order (853) → tree12.positions (872-895)

盘口来源:
  metar_observer.py::fetch_tree5_books (508) → clob_market_data.py::CLOBMarketData.fetch_books (145)  [REST /books]
  （未接线）websocket_market_data.py::MarketStream (21) + local_order_book.py::LocalOrderBook (38)

FAK 撮合（均未用于活跃路径）:
  paper_execution.py::simulate_paper_fak (97)      # REST /book, 逐档 walk, 缺 partial+cancel
  tree2_execution.py::build_fixed_five_fak (63)    # 逐档 walk, 缺 partial+cancel
  tree3_execution.py::simulate_local_fak (25)      # WS 本地盘口, 有 PARTIAL_FILL_REMAINDER_CANCELLED (45)

Live 签名（离线，无提交）:
  order_signing.py::build_unsigned_buy_order (151) / sign_order (189)
```
