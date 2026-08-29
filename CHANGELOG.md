# Changelog

本仓库采用“**先纸面、可回放、失败关闭，后续再独立审查执行**”的变更原则。任何涉及真实下单、撤单、账户查询、钱包或私钥的能力均不在以下版本范围内。

## tree12-allno — 独立 early-NO 布局（当前分支）

| 类别 | 改动 | 状态与验证要求 |
|---|---|---|
| 深度重构（PRD Step 5–10 完成） | 完成 cron agent 遗留的半成品：`execution/market.py`（BookView 归一化盘口）、`execution/order_state.py`（OrderStateMachine，审计 §8）、`execution/paper_executor.py` 的 `match_fak`/`match_gtc` 替换 `match_l2`、`execution/order_intent.py` 的 `Side/OrderType/OrderStatus` str-enum 与 `OrderIntent.new`、`execution/risk_gate.py` 的 `RiskGate` 类、`execution/position.py` 的 Fill 驱动 Position+apply_fill+unrealized_pnl+realized_pnl_for_exit；新增 `adapters/polymarket/orderbook.py`（from_any→BookView）与 `adapters/polymarket/live_executor.py`（薄封装 CLOB 下单器，默认 OFF）。tree2/paper_execution/tree12 各自旧撮合循环已删除，统一委托 `match_fak`/`match_gtc`。 | 177 tests OK；实测出现真实部分成交（2.01 股 + 2.99 股 resting）。 |
| 观察修复 | ① `RiskGate` BUY 滑点符号修正（`(ask-limit)/ask`，修复 ask 高于限价永不触发 SLIPPAGE_EXCEEDED）；② `audit_store` 的 `idx_audit_order` 索引移到 legacy 迁移之后，修复旧库 `no such column: order_id`。 | 177 tests OK；旧库自动迁移验证通过。 |
| 订单执行重构 | 新增 `execution/` 包：`order_intent.py`（OrderIntent + OrderStatus + Fill）、`risk_gate.py`（独立风控）、`paper_executor.py`（`match_l2` 按真实 L2 深度逐档撮合，partial fill + FAK 剩余取消显式 + 深度加权均价）。tree12 的 `tree12_paper_fill` 改为走 `match_l2`，不再“按 best_ask 单档整单假成交”。 | 新增 `tests/test_execution_paper.py`（full/partial/zero/FOK/avg-price + risk gate）；本地重跑验证：新代码 35 笔成交、已用 160.92/1000 USDC，旧假成交已被替换。 |
| 审计 | 3 名独立 `omp` 审计员（herdr 组织）+ 主审计报告：`docs/audit-a-order-execution.md`、`docs/audit-b-websocket-orderbook.md`、`docs/audit-c-risk-state-consistency.md`，PRD 见 `docs/PRD.md`。 | 一致结论：tree12 纸面成交原先不可信、WS 层未接线、无统一 OrderIntent/RiskGate/PnL。 |
| 策略 | 新增 `tree12_allno_strategy.py`：49 城、high/low 双向、目标 5 股 NO 布局；入场时间窗 `> 当地 00:00 − 24h`、TAF 规避、非共识前 2、`0.85 ≤ best_ask ≤ 0.95`（含端点）；hybrid 限价与 SELL NO FAK 出场阶梯。 | 默认 paper / observe-only；真实成交需外部持仓对账。 |
| 入场价格带 | `best_ask` 门槛由 `> 0.85` 改为 `[0.85, 0.95]` 闭区间；`hybrid_limit_price` 保护在同一区间内。 | 新增 `test_ask_range_inclusive_085_to_095`、`test_ask_above_095_blocked`；买入盈亏比不再被 >0.95 的薄利 NO 稀释。 |
| TAF 独立 | tree12 拥有自己的 `state.tree12.taf_fetches` / `taf_forecasts`，本地实现 `TAF_EXTREME_RE`、`parse_taf_extremes_for_local_day`、`due_tree12_taf_cities`、`record_tree12_taf_reports`；`tree12_allno_strategy.py` 不再 import `tree5_strategy`。 | 新增 `tests/test_tree12_allno_strategy.py` 中 `test_tree12_taf_is_self_contained`、`test_taf_parse_maps_local_day`。 |
| 调度修复 | `metar_observer.run_loop` 中 `tree12_maintenance_once` 不再嵌套在 `tree5_enabled` 分支内；tree5 与 tree12 各自独立调度维护。 | 配置 `tree12_enabled=true` 且 `tree5_enabled=false` 时，tree12 的 0/5/20/60/120s FAK 阶梯仍可运行。 |
| 盘口采样语义 | `ws_mid_samples` → `ws_ask_samples`、`ws_vwap_6h` → `ws_ask_vwap_6h`、`record_ws_sample` → `record_ws_ask_sample`；hybrid fair 改为 `mid(6h WS ask VWAP, best_ask)`。 | 消除了“名为 mid 实为 ask”的不一致。 |
| 纸面成交 | `paper_fill_working_order` 的 `avg_price` 由覆盖改为按成交股数加权平均。 | 多次部分成交时保留真实加权成本。 |
| 价格带 | `config.example.json` `max_execution_price` 0.99 → 0.98，与 `paper_execution.MAX_PRICE_INCLUSIVE` 一致；`load_config` 新增 `max_execution_price > 0.98` 即报错。 | fail-closed，拒绝无法构造的 CLOB 价格。 |
| 配置 | 新增 `tree12_taf_fetch_local_hour`（固定 1）、`tree12_taf_retry_seconds`、`tree12_exit_retry_seconds`、`tree12_exit_slippage`、`tree12_exit_min_price`、`tree12_action_dir`。 | `config.example.json` 已同步。 |
| 测试 | 修正 `test_execution_boundary` 过期价格带用例（0.40–0.98）；新增 tree12 独立 TAF 解析/记录用例。 | 本地 `python3 -m unittest discover -s tests` 全绿（112 tests OK）。 |
| WS 本地盘口接线 | 新增 `execution/book_source.py`：`LocalBookSource` 桥接既有 `MarketStream`/`LocalOrderBook` 状态机——REST 快照作为 prime 源灌入本地盘口，消费端读 freshness 门控的本地盘口，REST 兜底；`market_ws_enabled=true` 时 `metar_observer` 的 tree12 路径走 `fetch_tree5_books_local`（`process_tree12_cycle` 与 `tree12_maintenance_once`），REST 失败即 `disconnect` 使本地盘口失效（fail-closed）。 | 默认 `market_ws_enabled=false` 时行为不变；新增 `tests/test_book_source.py`（8 项：本地优先/过期兜底/断线失效/token 扩展/健康）。 |
| 最小 PnL | 新增 `execution/position.py`：`Position`（加权成本 + 已实现/未实现 PnL）+ `realized_pnl_for_exit` 纯函数；tree12 `paper_fill_working_order` 仓位附加 `cost_basis_usdc`/`realized_pnl_usdc`，退出 FAK 计划附加 `estimated_realized_pnl_usdc`。 | 新增 `tests/test_position.py`（7 项）。审计结论 M1“全程无 PnL”消除。 |
| 统一 order_id 审计 | `audit_store` 新增 `order_id` 列（旧库自动迁移）+ `append_order_event`（SIGNAL→INTENT→RISK→SUBMIT→ACK→FILL→CANCEL→POSITION→PNL 九阶段）+ `events_for_order`；tree12 从 `plan_tree12_entries` 起生成 `t12-<uuid12>` 贯穿 submit/fill/position/cancel/exit_chase/exit_fak，`metar_observer.append_tree12_actions` 落库。 | 新增 `tests/test_audit_lifecycle.py`（10 项：生命周期/迁移/贯穿）。 |
| 删除重复撮合 | `tree2_execution.build_fixed_five_fak` 与 `paper_execution.simulate_paper_fak` 的逐档 walk 统一委托 `match_l2`（单一深度撮合实现）；tree3 经 `build_fixed_five_fak` 间接统一。 | 输出结构/字段语义不变，`test_tree2_execution`/`test_execution_boundary` 原样通过。 |
| 成交测试矩阵 | 新增 `tests/test_execution_matrix.py`：按 PRD §G 补全 9 行矩阵（完全/部分/零成交、超价、盘口过期、重复订单防重、断线重连、仓位一致、Paper/Live 同模型）。 | 新增 12 项测试。 |
| 未实施 | 真实 FAK/GTC、撤单、卖出、NO/merge、账户/用户流对账、凭证加载。 | 保持未实现；如未来需要，必须在独立模块、独立审计和明确确认后再处理。 |

## Unreleased — tree5 净期望值与共识风险研究

| 类别 | 改动 | 状态与验证要求 |
|---|---|---|
| 策略规则 | 新增 `docs/tree5_ev_model.md`，将“最新 TAF 与市场最高共识一致且领先第二桶”形式化为 t0 可见版本、全桶可执行 bid 排名、20% 相对领先与 5 个百分点绝对领先的纸面门。 | 文档化完成；阈值必须按日期滚动的前瞻样本验证。 |
| 净期望 | 新增 `tree5_ev_policy.example.json`，要求使用 `p_lower`、L2 ask VWAP、费用、预期退出成本与时延储备来计算 EV 下界；缺任一输入即阻断。 | 仅预注册配置；后续单元/回放测试不得用日后信息填充。 |
| 净期望评估器 | 新增 `tree5_ev_model.py` 与 7 项测试：按 t0 前全桶 L2 验证 TAF 桶第一名、20% 相对领先与 5 个百分点绝对领先；再用 post-t0 ask 走簿、留出样本概率下界、填单率和成本计算 EV 下界。 | 已完成 5 分钟/49 轮、每轮 94 项测试的隔离验证；不访问网络或交易接口。 |
| 纸面风险 | 新增 `tree5_risk_state.py` 与 7 项测试：将 `FACT_INVALIDATED`、`CONSENSUS_REVERSAL` 和 `TIME_CLOSURE_AND_CONSENSUS_REVERSAL` 按优先级分开建模；旧仓退出、NO/完整对和新 YES 入场为独立决策。 | 状态机只生成 `PAPER_STOP_NEW_ENTRIES`、`PAPER_CANCEL_CANDIDATE`、`PAPER_EXIT_CANDIDATE`、`PAPER_ROUTE_COMPARISON_REQUIRED` 和独立新仓候选，绝不创建真实订单。 |
| 单轮协调 | 新增 `tree5_paper_cycle.py` 与 3 项测试，将每个最新 TAF 周期的共识、EV、post-t0 L2 与已有持仓风险输入组合为一份冻结纸面结果。 | 不满足 TAF 共识或 EV 门时直接跳过；旧持仓证伪仍与新入场分开记录。已完成 5 分钟/49 轮、每轮 104 项测试的隔离验证。 |
| 执行边界 | 新增 `scripts/soak_test.sh`，连续运行本地测试以验证回归稳定性。 | 已完成基线 5 分钟/49 轮，87 项既有测试均通过。 |
| 未实施 | 真实 FAK/GTC、撤单、卖出、NO/merge、账户/用户流对账、凭证加载。 | 保持未实现；如未来需要，必须在独立模块、独立审计和明确确认后再处理。 |

## tree5 历史基础

`tree5` 基于 tree4 的 CheckWX METAR/SPECI 与有界 AviationWeather 回退，已经包含默认 observe 模式、TAF TX/TN 解析、合同桶映射、实况证伪的计划退出及 append-only 审计。详见 `README.md`。
