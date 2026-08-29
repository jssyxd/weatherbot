# 审计报告 B — WebSocket / OrderBook / 市场数据层

> 审计员：独立审计员 B（只读，未改任何代码/测试/配置）
> 日期：2026-08-29
> 范围：`websocket_market_data.py`、`local_order_book.py`、`clob_market_data.py`、`tree3_runtime.py`、`tree3_execution.py`、`market_adapter.py`、`paper_execution.py`、`tree2_execution.py`、`execution_policy.py`、`order_signing.py`、`tree12_allno_strategy.py` 中盘口/执行相关路径，以及 `metar_observer.py` 的接线点。
> 对照材料：`审计的要素-严格对照.txt`、`借鉴和借鉴说明.docx`
> 分级：🔴严重 / 🟠高 / 🟡一般 / 🟢正常

---

## 0. 一句话结论

**整个 WebSocket / 本地 OrderBook 层是"未接线的死代码"**：没有真实 WebSocket 客户端库、没有连接/心跳/重连实现、没有任何生产代码 import 它；主循环实际只用 REST `clob_market_data.fetch_books` 做盘口兜底。当前真正生效的 Paper 执行路径（`legacy` / `tree2` / `tree12`）中，只有 `legacy` 和 `tree2` 会按真实 ask 深度逐档撮合，`tree12` 的 GTC 纸面成交是"按 best_ask 单档价整单全成交"的虚假成交，且没有有效 book age 门。

---

## 1. WebSocket 连接建立 / 传输层

### 🔴 1.1 根本没有真实 WebSocket 连接实现

- `requirements.txt:1` 只有 `eth-account>=0.13,<0.14`，**没有任何 WebSocket 客户端库**（无 `websocket-client`、`websockets`、`aiohttp`、`asyncio`）。
- 全仓 grep `import websocket | WebSocketApp | run_forever | create_connection | asyncio | aiohttp` **零命中**。
- `websocket_market_data.py:1-6` 模块 docstring 明确写 "The transport is injected so replay tests do not require a network connection"，`MarketStream` 只接收 `handle_message()`，没有任何 socket 读取。
- `tree3_runtime.py:1-5` docstring 同样写 "Transport is intentionally injected."，`Tree3MarketRuntime.connect()`（`tree3_runtime.py:24-27`）只是把状态置 `connected` 并返回订阅消息，**不建立任何连接**。

结论：所谓"WebSocket 连接建立/Snapshot/Delta/断线重连"这一整套，在代码里只有一个可复现的状态机骨架，**没有一条真实的网络路径**。`MARKET_WS_URL`（`websocket_market_data.py:14`）从未被任何 socket 使用。

### 🔴 1.2 WS 层未接入主循环（死代码）

- `Tree3MarketRuntime` 只在 `tests/test_tree3_runtime.py:5` 被 import；`MarketStream` 只被 `tree3_runtime.py:12` 和 3 个测试文件 import。生产代码 `metar_observer.py` **没有任何 import**。
- 主循环对 `execution_engine == "tree3"` 直接返回 fail-closed 占位（`metar_observer.py:877-878`）：
  ```python
  result["execution"] = {"mode": "paper", "status": "tree3_local_book_required",
      "decision_code": "LOCAL_BOOK_PATH_NOT_ATTACHED", ...}
  ```
- 实际配置 `config.json:3` `execution_engine = "legacy"`、`config.json:12` `market_ws_enabled = false`。`market_ws_enabled` 只在 `metar_observer.py:286` 读入 config dict，**之后无任何消费点**——是一个死配置。

结论：审计要求 5"Paper 必须基于真实 WebSocket 本地盘口"**完全未满足**。实际 Paper 走 REST，而非 WS。

---

## 2. Snapshot / Delta / size=0 删除档位

### 🟢 2.1 全量 Snapshot + 增量 Delta 的基本语义正确

- `local_order_book.py:80-98` `apply_book`：全量替换 `_bids/_asks`，校验 `token_id` 一致，更新 `hash/timestamp/tick_size/min_order_size/neg_risk`，`version += 1`，`ready = True`。
- `local_order_book.py:100-125` `apply_price_change`：要求 `book_baseline_required`（`local_order_book.py:104-105`），即 delta 必须建立在全量 snapshot 之上，方向判定 `SELL/ASK → _asks`、`BUY/BID → _bids` 正确。

### 🟢 2.2 size=0 删除价格档实现正确（审计重点项）

- `local_order_book.py:115-118`：
  ```python
  if size == 0:
      levels.pop(price, None)
  else:
      levels[price] = size
  ```
  语义正确：size=0 表示撤掉该档，`dict.pop` 幂等。`_decimal`（`local_order_book.py:55-63`）只拒绝负值/非有限值，允许 0，因此 size=0 能正常流入该分支。✅ 这是本次审计重点中唯一"实现正确"的 WS 语义。

### 🟠 2.3 无序列号/乱序保护（stale book 回退风险）

- `apply_book`（`local_order_book.py:84-85`）**无条件覆盖** `_bids/_asks`，不比对 `hash`/`timestamp` 单调性，也不校验新 snapshot 的 `hash` 是否晚于当前。
- `MarketStream.handle_message`（`websocket_market_data.py:54-85`）对到达的 `book` 事件不做任何排序/去重/乱序重放。
- 后果：若迟到的全量 snapshot 在较新的 `price_change` 之后到达，本地盘口会被**回退到旧状态**。Polymarket market channel 的 `book.hash` 字段本可用于检测这种乱序/丢包，但代码只把它存下来（`local_order_book.py:87`），从不校验。

### 🟠 2.4 delta 的 hash 不做链式一致性校验

- `apply_price_change`（`local_order_book.py:119-122`）更新 `book_hash` 但**不校验** delta 携带的 hash 是否与当前 snapshot 的 hash 连续。若中间丢了一个 delta，本地盘口会静默偏离真实盘口且无告警。

---

## 3. 断线重连 / 心跳

### 🔴 3.1 无断线重连、无心跳、无退避

- `tree3_runtime.py:42-44` `disconnect()` 只把 `state = "reconnecting"`，**不触发任何重连动作**；`reconnect_count` 只在 `connect()`（`tree3_runtime.py:26`）里 +1，而 `connect()` 没有任何生产代码调用。
- 无 exponential backoff、无重连循环、无 watchdog、无 `last_event_at` 超时检测（`last_event_at` 只被记录，`tree3_runtime.py:35`，从未用于判定超时断线）。
- `MarketStream.handle_message`（`websocket_market_data.py:83`）把 `heartbeat/ping/pong` 直接 `return None`，**没有心跳发送逻辑**、没有 PING/PONG 保活、没有 heartbeat 超时判断。

结论：审计要求中的"断线重连"整块缺失。对照 docx 推荐（`discountifu/polymarket-websocket-client` 提供 automatic reconnect / exponential backoff / heartbeat），当前实现一个都没有。

---

## 4. 竞态 / stale book / 时钟一致性

### 🟠 4.1 两个 freshness 方法时钟源不一致

- `LocalBookSnapshot.is_fresh`（`local_order_book.py:15-19`）：`import time` + `time.time()`，**忽略注入 clock**。
- `LocalOrderBook.is_fresh`（`local_order_book.py:167-170`）：用 `self.clock()`。
- 后果：`simulate_local_fak`（`tree3_execution.py:30-31`）调 `local.is_fresh(max_age_seconds, now=now)` 走 snapshot 版（wall clock），而 `Tree3MarketRuntime.local_snapshot`（`tree3_runtime.py:46-50`）走 `stream.snapshot()` → book 版（注入 clock）。两处 stale 判定在回放/测试与实际运行间会不一致。因 tree3 未接线，当前无实害，但属未来接线必踩的坑。

### 🟠 4.2 REST 路径的 book age 门形同虚设

- `tree2_execution.simulate`（`tree2_execution.py:118-122`）先 `fetch_books()`（同步 REST）再 `executable_summary()`，而 `fetch_books` 里 `fetched_at_epoch = time.time()`（`clob_market_data.py:127`），于是 `executable_summary` → `get_cached` 拿到的快照**年龄恒为 ~0**。
- `execution_policy.decide_buy_no` 的 `age > max_book_age_seconds`（`execution_policy.py:59`）因此**在 tree2 永不触发**；`test_tree2_safety.py:62-70` 能触发 `STALE_OR_MISSING_BOOK` 只是因为测试用 `max_snapshot_age_seconds=0.01` 且不调用 `fetch_books` 刷新。
- 唯一真正有意义的 book age 门在死代码 `tree3`（`tree3_execution.py:30-31`）。

### 🔴 4.3 当前实际运行路径 legacy 完全不检查 book age/staleness

- `paper_execution.simulate_paper_fak`（`paper_execution.py:97-211`）是 `config.json` 的默认路径（`execution_engine=legacy`）。
- 它在 `paper_execution.py:112-116` 直接 `fetch_order_book` 后读 `book.get("asks")`，**无 `is_fresh`、无 `book_age` 检查、无 `book_hash` 校验、无 timestamp 有效性判断**。虽然每次 REST 新抓使其天然"新鲜"，但没有任何"快照是否可信/是否过期"的防御，一旦未来加缓存或批量复用就会静默吃 stale book。

---

## 5. 下单时是否检查 book age（审计点 4）

| 执行引擎 | 是否检查 book age | 证据 |
|---|---|---|
| `legacy`（**当前默认**） | ❌ 完全不查 | `paper_execution.py:112-116` 直接使用 asks |
| `tree2` | ⚠️ 名义查，实际恒过（刚同步抓取） | `tree2_execution.py:118-122` + `execution_policy.py:59` |
| `tree3` | ✅ 真正查（`local.is_fresh`）但**未接线** | `tree3_execution.py:30-31` |
| `tree12` GTC 纸面成交 | ❌ 不查 | `tree12_allno_strategy.py:804-834` |

结论：**当前没有任何一条实际运行的路径在下单前检查 book age**。tree3 有实现但从未被调用。

---

## 6. WS 重复订阅（审计点 5）

### 🟡 6.1 token 去重正确，但 connect 无守卫

- `MarketStream.__init__`（`websocket_market_data.py:23`）用 `tuple(dict.fromkeys(...))` 对 `token_ids` 去重 ✅。
- `mark_connected`（`websocket_market_data.py:36-40`）每次都重置 `subscribed = False` 并返回 `subscription_message()`；`Tree3MarketRuntime.connect`（`tree3_runtime.py:24-27`）**无"已连接"守卫**，重复调用会重复发订阅（对重连场景这是正确行为，但对误调无保护）。
- 因无真实 transport，此问题当前无实害，标注为接线后需加守卫的隐患。

---

## 7. 是否重复造轮子（对照 docx 成熟 SDK）

### 🔴 7.1 基础设施全量手写，未用任何官方 SDK

对照 docx 明确要求（复用 `Polymarket/py-sdk`、`py-clob-client`，禁止重写签名/WS/CLOB client），本项目手写了全部基础设施：

| 手写模块 | 对应成熟轮子 | 证据 |
|---|---|---|
| `local_order_book.py` | py-clob-client / py-sdk OrderBook | 全文件 |
| `clob_market_data.py` | py-sdk CLOB REST client | 全文件 |
| `order_signing.py` | py-clob-client EIP-712 签名 | `order_signing.py:151-201` |
| `websocket_market_data.py` | py-sdk / websocket-client | 全文件 |

`requirements.txt:1` 仅 `eth-account`（签名底层原语），未引入任何官方 SDK。docx 第 385-401 行特别警告的"旧 py-clob-client 已归档、不要自己写签名/WS"，本项目完全反向操作。

### 🟠 7.2 三套并行 FAK 模拟器，风控口径互相冲突

- `paper_execution.simulate_paper_fak`（价格下限 `0.40`，`paper_execution.py:21`）
- `tree2_execution.simulate`（价格下限 `0.05`，`tree2_execution.py:19`）
- `tree3_execution.simulate_local_fak`（复用 tree2 的 `build_fixed_five_fak`）
- 外加 `tree12_allno_strategy.tree12_paper_fill`（NO ask 区间 `0.85-0.95`，`tree12_allno_strategy.py:19-20`）
- `execution_policy.decide_buy_no` 只被 tree2 使用；legacy/tree12 各自内联风控。价格下限三套（0.40 / 0.05 / 0.85）互不相同，违反"Paper 与 Live 共用一套 RiskGate"。

### 🟠 7.3 两套盘口快照类型并存

- `local_order_book.LocalBookSnapshot`（WS 层）vs `clob_market_data.BookSnapshot`（REST 层），需要 `tree3_execution.py:15-22` 的 `_book_from_local` 手动转换。字段语义（`fetched_at_epoch` vs `received_at_epoch`、`timestamp` vs `exchange_timestamp`）不统一。

---

## 8. REST 盘口兜底正确性（审计点 3）

### 🟢 8.1 分批 + 单抓 fallback 设计合理

- `clob_market_data.fetch_books`（`clob_market_data.py:145-182`）：按 `BOOKS_CHUNK_SIZE=100` 分批 POST `/books`，对每批缺失的 token 逐个 GET `/book?token_id=` 兜底，fail-closed。注释（149-153）解释了批量过大易超时的问题，实现与注释一致。

### 🟡 8.2 错误被静默吞掉，调用方拿不到失败明细

- 整批失败仅 `pass`（`clob_market_data.py:169-170`），单抓失败 `continue`（`clob_market_data.py:179-180`），最终 `fetch_books` 可能返回**部分结果且无错误说明**。
- 上层 `fetch_tree5_books`（`metar_observer.py:508-515`）也只把整体异常转成字符串，无法区分"哪个 token 失败、为何失败"。Paper 侧拿不到"盘口缺失是网络抖动还是该 token 无盘口"。

### 🟡 8.3 tree12 的 "ws" 命名与实际数据源不符

- `record_ws_ask_sample` / `ws_ask_vwap_6h` / `tree["ws_ask_samples"]`（`tree12_allno_strategy.py:208-227`）名字含 "ws"（WebSocket），但其数据来自 `books_by_token`，而 `books_by_token` 由 `metar_observer.py:513` 的 `CLOBMarketData.fetch_books()`（**REST**）产生。
- `CHANGELOG.md:13` 声称"盘口采样语义 ws_mid→ws_ask"，但数据源始终是 REST 快照，并非 WebSocket。`hybrid_limit_price`（`tree12_allno_strategy.py:246-258`）据此计算的 "6h WS ask VWAP" 实为 "6h REST best_ask VWAP"。命名误导，会干扰后续对"WS 盘口是否已接"的判断。

---

## 9. Paper 成交真实性（审计点 3 / 5）

### 🔴 9.1 tree12 GTC 纸面成交是"按 best_ask 整单全成交"的虚假成交

- `tree12_paper_fill`（`tree12_allno_strategy.py:804-850`）对 `shares`（=`need`=整目标 5 股）在**单一 `fill_price`（=best_ask）一次性全成交**：
  ```python
  principal = shares * fill_price          # tree12_allno_strategy.py:832
  estimated_fee = shares * TREE12_PAPER_FEE_RATE * fill_price * (1 - fill_price)
  ```
- 调用点 `tree12_allno_strategy.py:664`：`tree12_paper_fill(state, key, need, ask, now_utc)`，其中 `ask` 是 `best_ask_of(...)`（`tree12_allno_strategy.py:598`）。
- **不检查 ask 深度是否 ≥ need、不跨档撮合、不支持 partial fill**。这正是审计要求五明确禁止的 `if ask <= price: filled = requested_size` 形态。若 best_ask 档只有 0.5 股，代码仍会"成交"5 股，虚增仓位。

### 🟢 9.2 legacy 是唯一真正逐档 walk 的路径

- `simulate_paper_fak`（`paper_execution.py:147-168`）按 ask 档位升序遍历、`quantity = min(size, target - filled)` 跨档撮合、算 partial、均价、逐档费用、`min_order_size` 门槛、城市日上限。✅ 这部分可信。
- `tree2_execution.build_fixed_five_fak`（`tree2_execution.py:84-108`）同样逐档 walk 并算 partial。✅

### 🟡 9.3 FAK"剩余取消"是隐式的，无显式 CANCEL_REMAINDER

- `simulate_paper_fak`/`build_fixed_five_fak` 会算 partial fill，但返回里**没有显式 `CANCEL_REMAINDER` 记录**，审计无法区分"成交多少、取消多少"。tree3 的 `simulate_local_fak` 用 `PARTIAL_FILL_REMAINDER_CANCELLED`（`tree3_execution.py:45`）语义较好，但 tree3 未接线。

### 🟢 9.4 费用公式与官方文档一致（已核实）

- 官方公式 `fee = C × feeRate × p × (1-p)`（docs.polymarket.com/trading/fees），代码 `fee_rate * price * (1-price)`（`paper_execution.py:153`、`tree2_execution.py:91`）**与官方一致**。✅（早期怀疑错误，经核实排除。）

### 🟡 9.5 tree12 费用率硬编码，与 legacy/tree2 不一致

- legacy/tree2 从 `/fee-rate/{token}` 取真实 `base_fee`（`paper_execution.py:86-90`、`tree2_execution.py:46-50`）。
- tree12 硬编码 `TREE12_PAPER_FEE_RATE = Decimal("0.05")`（`tree12_allno_strategy.py:27`），而 Politics 类实际 taker fee 为 0.04。纸面 PnL 会有偏差。

---

## 10. 风控对照（审计点 6 中与盘口/执行相关项）

| 风控项 | legacy | tree2 | tree3 | tree12 | 证据 |
|---|---|---|---|---|---|
| 最大/最小价格 | ✅ 0.40–0.98 | ✅ 0.05–0.98 | ✅ 0.05–0.98 | ✅ 0.85–0.95 | 见 7.2 |
| 最大滑点 | ❌ 无 | ❌ 无（未用 `max_slippage`） | ✅ `best_ask+slippage` | ✅ 退出折价阶梯 | `tree3_execution.py:37`；`tree2_execution.py` 无 slippage 消费 |
| **OrderBook 最大年龄** | ❌ 无 | ⚠️ 恒过 | ✅ 3s（未接线） | ❌ 无 | 见第 5 节 |
| 重复订单保护 | ❌ 无 | ❌ 无 | ❌ 无 | ⚠️ working_orders 去重 | `tree12_allno_strategy.py:626` |
| 余额检查 | ✅ reserve | ✅ reserve | ❌ 无 reserve | ✅ reserve | `paper_capital.py:41-56` |

关键缺口：**book age 门（`local_book_max_age_seconds`，`config.json:11`）配置存在，但没有任何实际运行的执行引擎真正消费它**；`max_slippage` 配置只在 tree3（未接线）和 tree12 退出路径生效。

---

## 11. 结论与风险清单汇总

| # | 级别 | 问题 | 位置 |
|---|---|---|---|
| 1 | 🔴 | 无真实 WebSocket 客户端/连接实现，WS 层纯状态机 | `requirements.txt:1`、`websocket_market_data.py:1-6`、`tree3_runtime.py:1-5` |
| 2 | 🔴 | WS 层未接入主循环（死代码），tree3 返回 fail-closed | `metar_observer.py:877-878`、`config.json:3,12` |
| 3 | 🔴 | 无断线重连/心跳/退避 | `tree3_runtime.py:42-44`、`websocket_market_data.py:83` |
| 4 | 🔴 | 默认 legacy 路径不查 book age/hash/staleness | `paper_execution.py:112-116` |
| 5 | 🔴 | tree12 GTC 纸面成交按 best_ask 整单全成交、不 walk depth | `tree12_allno_strategy.py:832-834,664` |
| 6 | 🔴 | 基础设施全量手写，重复造轮子（应复用 py-sdk） | `requirements.txt:1`、`local_order_book.py`、`order_signing.py` |
| 7 | 🟠 | 三套 FAK 模拟器 + 三套价格下限，风控口径冲突 | `paper_execution.py:21`、`tree2_execution.py:19`、`tree12_allno_strategy.py:19` |
| 8 | 🟠 | snapshot 无乱序/序列保护，迟到 snapshot 回退状态 | `local_order_book.py:84-85` |
| 9 | 🟠 | delta hash 无链式一致性校验 | `local_order_book.py:119-122` |
| 10 | 🟠 | 两个 freshness 方法时钟源不一致 | `local_order_book.py:15-19` vs `167-170` |
| 11 | 🟠 | REST 路径 book age 门形同虚设（刚抓取恒新鲜） | `tree2_execution.py:118-122`、`execution_policy.py:59` |
| 12 | 🟡 | `fetch_books` 静默吞错，无失败明细 | `clob_market_data.py:169-180` |
| 13 | 🟡 | tree12 "ws" 命名实为 REST 数据，误导 | `tree12_allno_strategy.py:208-227`、`metar_observer.py:513` |
| 14 | 🟡 | FAK 剩余取消隐式，无显式 CANCEL_REMAINDER | `paper_execution.py:194-209` |
| 15 | 🟡 | tree12 费用率硬编码 0.05，与 legacy/tree2 不一致 | `tree12_allno_strategy.py:27` |
| 16 | 🟡 | connect 无重复调用守卫（接线后需加） | `tree3_runtime.py:24-27` |
| 17 | 🟢 | size=0 删除价格档正确 | `local_order_book.py:115-118` |
| 18 | 🟢 | token 去重（dict.fromkeys）正确 | `websocket_market_data.py:23` |
| 19 | 🟢 | legacy/tree2 逐档 walk、partial、均价正确 | `paper_execution.py:147-168`、`tree2_execution.py:84-108` |
| 20 | 🟢 | 费用公式与官方一致 | `paper_execution.py:153` |
| 21 | 🟢 | REST 分批 + 单抓 fallback 设计合理 | `clob_market_data.py:145-182` |

---

## 12. 与 docx 借鉴建议的对照结论

- **WebSocket**：docx 建议"不要自己写 WS 协议/心跳/重连"，本项目手写了一个未接线的状态机，既没复用官方 SDK，也没实现任何真实功能。**应删除 `websocket_market_data.py` + `tree3_runtime.py`，改用官方 py-sdk 的 market channel（或 `discountifu/polymarket-websocket-client` 思路）。**
- **OrderBook**：docx 明确"不要自己写 Local OrderBook"。`local_order_book.py` 属重复造轮子；`clob_market_data.py` 的 REST 盘口兜底可保留但应薄封装到 py-sdk。
- **OrderIntent / RiskGate / PaperExecutor**：当前没有统一 OrderIntent、没有独立 RiskGate，docx 建议的这三层是**允许自己写**的部分，恰恰是当前缺失、而基础设施被过度手写的部分——方向反了。

---

*本报告为只读审计，未修改任何代码/测试/配置。所有行号基于审计当日仓库快照。*
