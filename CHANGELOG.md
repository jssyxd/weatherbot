# Changelog

本仓库采用“**先纸面、可回放、失败关闭，后续再独立审查执行**”的变更原则。任何涉及真实下单、撤单、账户查询、钱包或私钥的能力均不在以下版本范围内。

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
