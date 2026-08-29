# Changelog

## 2026-08-30 — 订单执行系统重构 + 市场规则并发修复

### 订单执行层重构（新增 `execution/` 包，统一 Paper/Live 模型）
- `execution/order_intent.py`：OrderIntent 统一订单模型（Paper/Live 共用，含 order_id/token_id/side/price/quantity/order_type/strategy/signal_reason/created_at）。
- `execution/order_state.py`：9 态订单状态机（CREATED→RISK_REJECTED｜SUBMITTED→ACKED→PARTIALLY_FILLED→FILLED/CANCELLED｜REJECTED/ERROR）。
- `execution/risk_gate.py`：独立风控（价格上下限/滑点/盘口龄/重复订单/余额）。
- `execution/paper_executor.py`：基于真实 L2 盘口的 FAK 模拟（full / partial+取消剩余 / zero）。
- `execution/live_executor.py`：Live fail-closed stub（LIVE=OFF，无私钥/签名/下单）。
- `execution/audit.py`：Fill→Position→PnL 统一审计 + risk_ledger 强制写入（闭合审计 B-M3）。
- 重接 `metar_observer.enrich_execution` → 新执行层；删除旧执行器（paper_execution/tree2_execution/tree3_execution/tree3_runtime/order_signing/execution_policy）合并为 1 套。

### 市场规则并发修复（审计 B3）
- `market_adapter.refresh_market_rules` 由串行改为并发（ThreadPoolExecutor 16 workers）。
- 配置 `market_metadata_timeout_seconds` 3→10、`market_refresh_deadline_seconds` 30→90。
- 效果：市场规则覆盖率 55/98 → 98/98（0 失败）。

### 测试
- 新增 `tests/test_execution_layer.py`：10 用例矩阵（full/partial/FAK取消剩余/zero/超价/stale/重复/断线重连/position-fill一致/paper-live共用）。

### 安全
- Live 保持 OFF（LIVE_EXECUTOR_DISABLED 双层 fail-closed），无私钥加载。
