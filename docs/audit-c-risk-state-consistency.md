# 审计报告 C — 风控 / 订单状态 / Paper-Live 一致性 / 审计账本

> 审计员：独立审计员 C（只读审计，未改任何代码/测试/配置）
> 仓库：`/home/da/桌面/tree12-allno`
> 日期：2026-08-29
> 范围：RiskGate 独立性 · 订单状态机 · 信号重复触发防重 · Fill→Position→PnL 一致性 · 订单可追踪性 · Live 安全性

---

## 0. 结论速览

| 审计焦点 | 判定 | 严重度 |
|---|---|---|
| RiskGate 是否独立 | 否，三套风控散落各处，价格门互不相同 | 🔴 |
| 信号重复触发防重 | 有状态标记防重，但无 PENDING/SUBMITTED/ACKED 状态机 | 🟡 |
| Fill→Position→PnL 一致 | tree12 按单档 best_ask 整单成交（不看深度）；全仓库无 PnL | 🔴 |
| CANCELLED 但仓位仍增加 | tree12 未复现（fail-closed），但退出路径永不减仓 | 🟢/🟠 |
| 订单可追踪性 | 无统一 `order_id`，SQLite 只记动作快照，`risk_ledger` 空表死代码 | 🔴 |
| Live 默认关闭/是否提交真实订单 | 默认 `paper`，fail-closed，无 live executor，不加载私钥 | 🟢（关闭态安全） |

---

## 1. RiskGate 是否独立 —— 🔴 否

**没有统一的 RiskGate。** 风控逻辑散落在三套互不调用的执行路径里，且价格门/滑点/盘口新鲜度检查彼此不一致。

### 1.1 三套并存的风控实现

| 路径 | 入口 | 价格门 | 滑点 | 盘口新鲜度 |
|---|---|---|---|---|
| tree1 死桶（legacy） | `paper_execution.simulate_paper_fak` | `MIN/MAX_PRICE_INCLUSIVE = 0.40 / 0.98` | 无 | 无（每次新 REST 拉取，不记 age） |
| tree2 | `execution_policy.decide_buy_no` + `tree2_execution.build_fixed_five_fak` | `MIN/MAX_EXECUTION_PRICE = 0.05 / 0.98` | 无 | `max_book_age_seconds=3.0` |
| tree3（未接线） | `tree3_execution.simulate_local_fak` | 复用 tree2 `0.05/0.98` | `max_slippage=0.10` | `is_fresh(3.0)` |
| tree12（当前启用） | `tree12_allno_strategy.plan_tree12_entries` | `TREE12_MIN/MAX_NO_ASK = 0.85 / 0.95` | 无（仅 hybrid limit） | 无（不检查 book age） |

证据：

- `execution_policy.py:26-93` `decide_buy_no()` —— 唯一名为「风控决策」的函数，但**只被 `tree2_execution.simulate` 调用**（`tree2_execution.py:127`）；tree1 和 tree12 均不经过它。
- `paper_execution.py:21-22` `MIN_PRICE_INCLUSIVE=0.40 / MAX_PRICE_INCLUSIVE=0.98`；`:127-133` 内联 best-ask 价格门。
- `tree2_execution.py:19-21` `MIN_EXECUTION_PRICE=0.05 / MAX_EXECUTION_PRICE=0.98 / DEFAULT_MAX_SLIPPAGE=0.10`。
- `tree12_allno_strategy.py:19-20` `TREE12_MIN_NO_ASK=0.85 / TREE12_MAX_NO_ASK=0.95`；`:613` 内联 ask 门。
- `config.json:6-7` `min_execution_price=0.40 / max_execution_price=0.98` —— 与 tree12 的 `0.85/0.95`、tree2 的 `0.05/0.98` **三套下限互不相同**。

### 1.2 六项风控逐项核对

| 检查项 | tree1 (legacy) | tree2 | tree12 | 结论 |
|---|---|---|---|---|
| 价格 min/max | 0.40/0.98 | 0.05/0.98 | 0.85/0.95 | 🔴 三套不一致 |
| 数量 >0 且 ≤max | 固定 5 股 | 固定 5 股 | 固定 5 股 | 🟢 数量恒定 |
| 盘口存在/完整 | 检查 asks 列表 | `executable_summary` | 仅 best_ask 非空 | 🟡 程度不一 |
| 盘口过期 | 无 | 3s | **无** | 🟠 tree12 无 age 检查 |
| 滑点 | 无 | 无 | 无（入场） | 🟠 入场无滑点门 |
| 重复订单 | `handled_candidate_buckets` | 无（由 tree1 层防重） | `working_orders` map | 🟡 状态标记防重 |
| 最大仓位 | 无（只有资金上限） | 无 | 每 bucket 5 股 | 🟡 无全局仓位上限 |
| 余额检查 | `reserve()` + city-day cap | `reserve()` | `remaining_capital_usdc` + `reserve()` | 🟢 有资金检查 |

关键证据：

- **tree12 入场无盘口新鲜度检查**：`plan_tree12_entries` 在 `tree12_allno_strategy.py:598` 直接 `ask = best_ask_of(books_by_token.get(token))`，全程不检查 `book_age`/`book_timestamp`。books 来自 `fetch_tree5_books`（`metar_observer.py:508-515`），它调用 `CLOBMarketData.fetch_books`（`clob_market_data.py:145-182`）每次都做**全新 REST 拉取**，返回的 `BookSnapshot` 有 `timestamp` 字段但 tree12 从不读取，订单里也不记录 `book_age`。
- **tree12 入场无滑点检查**：`plan_tree12_entries` 的限价 `hybrid_limit_price`（`tree12_allno_strategy.py:246-258`）是 6h-WS-VWAP 锚定，不是滑点门。滑点只出现在**退出**追价梯（`tree12_allno_strategy.py:505-510`）。
- `execution_paused`（`metar_observer.py:603` 状态默认值）**全仓库无任何读取点**，是死开关。

---

## 2. 信号重复触发防重 —— 🟡 状态标记，非状态机

### 2.1 实际防重机制（可用，但语义弱）

- **tree1 死桶**：`edge_engine.evaluate_observation` 用 `handled_candidate_buckets` 按 `market_rule_id|bucket_id` 永久标记（`edge_engine.py:277-283`）。一个 bucket 只触发一次 `candidate_no_signal`。
- **tree12 入场**：`working_orders` 按 `position_key` 存单，`need = target - pos_shares`（`tree12_allno_strategy.py:604`）；已有 `working_gtc_buy_no` 则只 requote 或跳过（`:626-639`）。
- **tree5 入场**：`tree["entries"]` 按 `entry_key` 去重（`tree5_strategy.py:296-298`）。
- **退出追价**：`start_tree12_exit_chase` 检查 `existing.status in {"active","awaiting_reconciliation"}` 防重（`tree12_allno_strategy.py:434-436`）；tree5 同（`tree5_strategy.py:375-377`）。

### 2.2 缺失（与需求第十节对照）

需求明确「必须有 PENDING / SUBMITTED / ACKED / PARTIALLY_FILLED 状态的相同/冲突订单检查」，当前**完全不存在**这类状态机。实际使用的状态字符串是：

```
working_gtc_buy_no / filled / cancelled_filter_break / cancelled_metar_exit /
cancelled_taf_exit / blocked_insufficient_capital / resting_above_limit /
planned_observe_only / paper_filled / planned_gtc_entry / invalidated_by_metar …
```

（`tree12_allno_strategy.py`、`tree5_strategy.py`、`tree2_execution.py` 中散落，无 `CREATED→RISK→SUBMIT→ACK→FILL/CANCEL` 语义。）

- **没有 cooldown** —— 这点符合要求「不要用 cooldown 掩盖问题」。但防重靠「状态字典 + `need<=0`」，对 Paper 成立；**对 Live 不成立**，因为 `paper_mode=False` 时 `tree12_paper_fill` 不执行（`tree12_allno_strategy.py:663`），working order 永远停在 `working_gtc_buy_no`，`remaining_shares` 永不更新，一旦将来接 Live 无法区分 ACK/部分成交。
- tree1 的 `handled_candidate_buckets` 是**永久标记**：若某次 `simulate_paper_fak` 被拒（资金不足/无深度），bucket 已被标记，**不会重试**，属于「漏单」而非「重复单」的反向风险（`edge_engine.py:283`）。

---

## 3. Fill → Position → PnL 一致性 —— 🔴

### 3.1 tree12 纸面成交不看深度（核心问题）

`plan_tree12_entries` 在创建 working order 的**同一 tick** 立即调用 `tree12_paper_fill(state, key, need, ask, now_utc)`（`tree12_allno_strategy.py:663-664`），`fill_price=ask`。

`tree12_paper_fill`（`tree12_allno_strategy.py:804-850`）只检查 `fill_price <= limit_price`，然后：

```python
principal = shares * fill_price          # 804 行起，整单按单一 best_ask 计价
result = paper_fill_working_order(state, key, shares, fill_price, now_utc)
```

`paper_fill_working_order`（`:853-896`）直接 `filled = min(remaining, fill_shares)` 把整 5 股记入 `pos["shares"]`，**不 walk 盘口深度、不跨档、不产生 PARTIAL_FILL**。

这正命中需求第五节明确禁止的模式：

> 「是否错误地把“当前 ask”直接当成全部成交？」

证据（实盘运行数据，`data/audit.sqlite3`）：

```
tree12_submit_entry  204 条（全部 working_gtc_buy_no）
tree12_paper_fill    204 条（全部 paper_filled，filled=5）
tree12 positions     204 个
paper_total_debit_usdc = 996.91252（1000 初始，已近耗尽）
```

每条 fill 均 `filled=5` 且 `avg_price = principal/5` 等于单档 ask —— 例如 `singapore|2026-08-31|high|3967492` `principal=4.995 avg=0.999`。**从未出现 partial fill**，说明深度从未被检查。（注：0.96–0.999 的历史 fill 来自 commit `0fcb03e`「NO entry band 0.85-0.95」之前的旧代码；当前磁盘代码已有 0.95 上限门，但「单档整单成交、不看深度」的缺陷在 `tree12_paper_fill` 主体中仍然存在。）

### 3.2 「CANCELLED 但仓位仍增加」—— tree12 未复现（🟢）

`paper_fill_working_order` 仅由 `tree12_paper_fill` 调用，而 `tree12_paper_fill` 第一行守卫：

```python
if not isinstance(order, dict) or order.get("status") != "working_gtc_buy_no":
    return {"status": "no_working_order", ...}   # tree12_allno_strategy.py:819-821
```

已取消订单（`cancelled_*`）不会进入 fill，`reserve()` 失败也不写仓位（`:835-843`）。**该具体子问题 fail-closed，未发现仓位虚增。** 但注意：这是「当前未复现」，不是「被状态机保证」。

### 3.3 退出路径永不减仓、无 PnL（🔴）

- 退出追价 `plan_tree12_due_exit_faks` 只产出 `planned_observe_only` / `skipped_no_bid` 动作（`tree12_allno_strategy.py:492-533`），**从不减少 `pos["shares"]`、从不结算 PnL**。实证：`tree12_exit_fak` 313 条 `planned_observe_only` + 9 条 `skipped_no_bid`，`tree12_exit` 56 条 `chase_started`，但 `positions` 始终 204 未减少。
- **全仓库无 PnL 实现**：`grep -rn "pnl|realized|unrealized" *.py` 返回空。Position 有、Fill 有（tree12），但 PnL 链完全缺失。需求第十五节「只有实际 Fill 才改变 Position」在 tree12 满足，但「Fill→Position→PnL」整条链不成立。
- **tree1 不产生仓位**：`simulate_paper_fak`（`paper_execution.py:97-211`）只 `reserve()` 资金并返回估算，**从不写任何 position**。tree1 与 tree12 的「成交→仓位」语义不一致。
- **tree5 仓位来源不明**：`confirmed_positions` 只被 `tree5_risk_state` / `start_exit_chase` 读取（`tree5_strategy.py:363-368`），本仓库无任何写入 `confirmed_positions` 的代码，`confirmed_filled_shares` 恒为 `"0"`（`tree5_strategy.py:324`）。

---

## 4. 订单可追踪性 —— 🔴

### 4.1 无统一 `order_id`

`grep` 全仓库：无 `order_id` 生成逻辑；唯一相关字段是 tree5 的 `external_order_id`，且**恒为 `None`**（`tree5_strategy.py:324`）。需求第十四节要求的字段（`requested_price/filled_size/remaining_size/reject_reason/cancel_reason/book_timestamp/book_age`）**没有统一的订单模型承载**，散落在各 dict 中且命名不一（`no_token_id` vs `token_id`、`remaining_shares` vs `requested_shares` vs `shares`）。

### 4.2 审计账本现状

`audit_store.py`（SQLite，append-only）记录内容（实测 `data/audit.sqlite3` 48,388 行）：

```
tree12_entry            41569
tree12_entry_window      5125
signal_execution_decision  575
tree12_taf_fetch          328
tree12_exit_fak           322
tree12_submit_entry       204
tree12_paper_fill         204
tree12_exit                56
...
risk_ledger                 0   ← 空表，死代码
```

问题：

- **`risk_ledger` 表是死代码**：`set_ledger`/`get_ledger`（`audit_store.py:50-59`）只被 `tests/test_tree2_safety.py:81-82` 调用，生产代码从不写它。真正的资金账本在内存 `state` dict（`paper_total_debit_usdc` / `paper_city_day_total_debit`），**审计 DB 里没有资金/仓位账本**。
- 审计关联键是 `correlation_id`（`event_id` / `key` / `token_id`），**不是贯穿 SIGNAL→INTENT→RISK→SUBMIT→FILL→CANCEL→POSITION→PNL 的统一 order_id**（`metar_observer.py:530,550,1161`）。
- `signal_execution_decision` 大部分是 `no_signal`（`market_rules_stale`/`not_new_daily_low`），无成交信息。
- FAK 的「剩余取消」没有显式记录：tree2/tree3 的 partial 只体现在返回 dict 里，不落到审计表（`tree3_execution.py:44-45` 的 `PARTIAL_FILL_REMAINDER_CANCELLED` 只是 decision_code，tree12 根本不走这条路径）。

---

## 5. Live 是否默认关闭 / 是否提交真实订单 —— 🟢（关闭态安全）

### 5.1 默认关闭

- `config.json:2` `"mode": "paper"`。
- `config.example.json:2` 也是 `"paper"`；`:12` `market_ws_enabled: true`（示例开着 WS，但主循环不接线）。

### 5.2 fail-closed 三重保险

1. `metar_observer.enrich_execution`（`:881-886`）：`mode=live` 直接返回 `blocked_no_live_executor`，不产生任何订单。
2. `execution_policy.decide_buy_no`（`execution_policy.py:40-41`）：`mode=live` 返回 `LIVE_EXECUTOR_DISABLED`。
3. 无 live executor：`requirements.txt` 仅 `eth-account`；无 `py-clob-client`、无订单提交 SDK、无 POST/DELETE 到 CLOB。`order_signing.py` 只做离线 EIP-712 构造/签名，`sign_order`（`:189-201`）的私钥仅来自测试固定 key（`test_order_signing.py:20-21`），**生产代码零私钥加载**。

### 5.3 结论（需求第二十节）

**Live = OFF 是安全的**：即使把 `config.json` 的 `mode` 改成 `live`，也只会得到 `blocked_no_live_executor` 的审计记录，不会下任何真实订单。但这不是「可切换 Live」，而是「Live 尚未实现」—— 没有账户/余额对账、订单状态查询、撤单、真实成交确认。

### 5.4 附注（数据卫生，非交易风险）

- `.env` 文件存在真实 CheckWX API key（`CHECKWX_API_KEY=bbe7…`），但已被 `.gitignore` 排除（`git ls-files .env` 为空），且这是**只读天气 API**，不是钱包私钥。🟡 仍建议轮换并确认未误提交到任何历史提交。

---

## 6. 对照需求第二十二节的最终回答

### C. 当前 Paper 下单到底是否可信？

- **tree1（legacy 死桶）路径相对可信**：`simulate_paper_fak` 会逐档 walk 深度、算 partial、费用、均价、城市日上限（`paper_execution.py:147-192`），尽管 FAK 剩余取消未显式记录、且它不产生仓位。
- **tree12（当前启用）路径不可信**：`tree12_paper_fill` 按单档 best_ask 整单 5 股成交，不看深度，会虚增仓位（204 个仓位全部 `filled=5`，从未 partial）。
- **任何 PnL 结论都不可得**（无 PnL 实现）。
- 若现在让它 Paper 跑 3 天：tree1 的「成交均价/数量」大致可信；tree12 的「成交数量」不可信；PnL 完全不可得。

### D. 当前 Live 是否安全？

**SAFE（作为「无法下单」的关闭态）** —— 三重 fail-closed，无 live executor，不加载私钥，不提交真实订单。

> 严格按需求「只允许回答 SAFE / NOT SAFE」：从「会不会乱下单」角度是 **SAFE**；从「能否安全切换小额 Live」角度是 **NOT SAFE（Live 未实现，无下单/查单/撤单/对账路径）**。两条都成立，区别在于问题含义。建议运维口径：**继续保持 LIVE=OFF**。

---

## 7. 证据索引（文件:函数:行号）

| # | 结论 | 证据 |
|---|---|---|
| 1 | 三套风控、价格门不一致 | `execution_policy.py:26-93`；`paper_execution.py:21-22,127-133`；`tree2_execution.py:19-21`；`tree12_allno_strategy.py:19-20,613`；`config.json:6-7` |
| 2 | RiskGate 只被 tree2 调用 | `tree2_execution.py:127`；tree1/tree12 均未 import `decide_buy_no` |
| 3 | tree12 无盘口 age 检查 | `tree12_allno_strategy.py:598`（直接读 best_ask）；`metar_observer.py:508-515` |
| 4 | tree12 无入场滑点门 | `tree12_allno_strategy.py:246-258,616-619`；滑点只在退出 `:505-510` |
| 5 | `execution_paused` 死开关 | `metar_observer.py:603`（只 setdefault，无读取） |
| 6 | 防重是状态标记非状态机 | `edge_engine.py:277-283`；`tree12_allno_strategy.py:604,626-639`；`tree5_strategy.py:296-298` |
| 7 | tree12 单档整单成交 | `tree12_allno_strategy.py:663-664,804-850,853-896` |
| 8 | 实证 204 仓无 partial | `data/audit.sqlite3`：`tree12_paper_fill` 204 条全部 `filled=5` |
| 9 | CANCELLED 不增仓（fail-closed） | `tree12_allno_strategy.py:819-821,835-843` |
| 10 | 退出永不减仓、无 PnL | `tree12_allno_strategy.py:492-533`；`grep pnl` 空 |
| 11 | tree1 不产生仓位 | `paper_execution.py:97-211` |
| 12 | 无统一 order_id | `grep order_id` 无生成逻辑；`tree5_strategy.py:324` `external_order_id=None` |
| 13 | `risk_ledger` 死代码 | `audit_store.py:50-59`；仅 `test_tree2_safety.py:81-82` 调用；实测表 0 行 |
| 14 | Live fail-closed | `metar_observer.py:881-886`；`execution_policy.py:40-41`；`requirements.txt` 仅 `eth-account` |
| 15 | 私钥仅测试 | `order_signing.py:189-201`；`test_order_signing.py:20-21` |
