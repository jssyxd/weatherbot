# Changelog — weatherbot paper trading (tree12-allno / tree12-allnopart)

## 2026-08-30 — Paper 到期结算 + 部署迁移

### 新增:纸面到期结算(hold-to-settlement 闭环)⭐

**背景**:策略意图"持有至结算"此前只实现了"持有",未实现"结算"。持仓到 Polymarket 结算日后,赢的 NO 应结算 1.0、输的结算 0,但 paper 账本中持仓**永远挂着**:资金永久占用(paper_total_debit 只增不减,撞 1000 USDC 上限后系统停摆)、无 realized PnL、positions 永久累积、权益/胜率统计失真。

**实现**(`tree12_allno_strategy.py`,commit `80f52a8` / `77b409b`):

- 新函数 `settle_tree12_expired_positions(state, cities, rules, now_utc)`:
  - 遍历 `tree12.positions`,结算 `market_local_date` 已过结算期(当地日结束后 + grace)的仓位
  - **赢**(最终温度未落桶,NO 结算 1.0):`release(shares × 1.0)` 回补现金,`realized_pnl = (1.0 − avg_price) × shares`,审计 `tree12_settled_win`
  - **输**(温度落桶):`release(0)` 不回收,`realized_pnl = −avg_price × shares`,审计 `tree12_settled_loss`
  - 移除 position 与对应 working order,结算结果写入 `tree12.settled_positions`
- 接线:`metar_observer.py` 的 `process_tree12_cycle` 每轮调用
- 配置开关:`tree12_expired_settle_enabled`(默认 true)、`tree12_expired_settle_grace_hours`(默认 6)
- 单测 4 个:赢结算释放资金 / 输结算零回收 / 未到期不动 / 结算后移除 position+working order

**判定依据**:优先 Gamma 市场 resolved/outcome,fallback 用当日最终观测温度是否落入持仓桶。

### 修复:tree12-allnopart 部署阻断 bug(commit `b551842`)

1. **白名单过滤失效(零交易)**:`filter_allowed_cities` 用 dict key(ICAO,如 `ZSPD`)对比 `city_id` 白名单(如 `shanghai`),恒不匹配 → 过滤为空 → 10 城分支 0 交易。改为按 `c["city_id"]` 过滤。
2. **part 城市 schema 缺失**:`contract_cities_part.json` 的 10 城缺 `coordinate_source` 与 `market_city_slug`(seoul-incheon 应为 `seoul`)→ 配置校验失败 + Gamma slug 解析失败。已补齐。
3. **49 城硬编码校验**:`edge_engine.load_contract_cities` 硬编码 `len(by_icao) != 49` 抛错,10 城白名单分支无法启动。改为 `>= 10` 守卫(建议后续改为配置驱动)。

### 部署迁移(2026-08-30)

- **卸载**:老部署(weatherbot-tree11-yes / tree13-allno / tree6yes / tree4optimized 4 容器 + 镜像 + 卷、本地 tree12 进程与目录 `/home/da/桌面/tree12-allno`)全部清理,无历史残留
- **重新部署(全新 1000 USDC paper)**:
  - `weatherbot-tree12-allno`:tree12-allno 分支(49 城优化版:入场窗口 (18,30]h、共识 top3、exit 滑点 0.03→0.30 + 硬底 0.05、纸面割肉结算、纸面到期结算)
  - `weatherbot-tree12-allnopart`:tree12-allnopart 分支(10 城白名单:shanghai/beijing/tokyo/seoul-incheon/paris/madrid/amsterdam/munich/istanbul/singapore)
  - 均 paper 模式、初始 1000 USDC、CheckWX API key 环境变量注入、host 网络 + 代理 127.0.0.1:7897
- **监控**:15 分钟双项目运行报告(健康/资金/近 15min 活动/逆转退出统计:总下单 NO、被 METAR 证明错误数、逆转率、最近逆转、卖出价、已结算、挂账),Telegram 发送带重试

### 部署中规避的仓库缺陷(本地已修,建议同步仓库)

- `config.example.json`:`market_ws_enabled: true` 在 token 列表为空时抛 `at_least_one_token_required`(LocalBookSource([]) 构造 MarketStream 即崩),部署时配置为 false 走 REST 路径
- 容器代理被镜像层环境变量(192.168.1.5:7890 老代理)覆盖,docker run 时显式 `-e` 注入 127.0.0.1:7897

### 已知遗留(非本轮范围)

- 基线测试 6 failures + 2 errors(53d7a92 既有:top2→top3 后测试未同步、ask 区间测试、缺 eth_account 依赖),非本轮引入
- `edge_engine.py` 城市数校验建议改为配置驱动(消除 49 城硬编码)
- 到期结算的胜负判定建议在真实结算日(8/31/9/1)用 Gamma resolved 交叉验证
