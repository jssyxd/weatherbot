# Changelog — weatherbot tree12 (2026-09-01)

## 本轮重大发现与修复(双 pi 讨论 + Polymarket_data 验证驱动)

### 🔴 重大发现 1:割肉触发器"过度敏感"——逆转率虚高 3-4 倍
- **问题**:`plan_tree12_exits_from_metar` 用**任意瞬时 METAR 观测落入持仓桶**即触发割肉;但 Polymarket 结算以**当日运行极值**判定(`_extreme_based_win`)。瞬时穿桶(如中午 27.5°C 进 [27,28) 后升至 32°C)不证明 NO 已输——旧逻辑大量误割本会赢的仓。
- **数据铁证**(Polymarket_data 实测 2662 个已结算死桶市场):NO 真实输率仅 **7.3%**、市场隐含 ~10%,而实盘逆转率 **28.9%**——触发器过敏感,非选桶能力差。
- **修复**:触发割肉改为"当日运行极值落桶才割";极值缺失 fail-closed 不触发。

### 🔴 重大发现 2(补全):实时极值从不更新——修复 1 的必要前提
- **问题**:`daily_extrema` 仅在 warmup(当地日开始时)用历史重建一次,**实时新 METAR 从不更新**——中午创新高不会反映,极值割肉判定会漏割。
- **修复**:新增 `update_daily_extrema_from_observation`(high=max / low=min 滚动更新),接入 scan_once 实时观测循环。
- **最终割肉语义**:温度上升经过持仓桶(瞬时穿桶)→ 不割;当日运行极值(实时 max/min)进入持仓桶 → 立即割。

### 🔴 重大发现 3:TAF 过滤器在入场时"从未工作过"(时序 bug)
- **问题**:入场窗口是目标日 D 前 18-30h(当地 D-1),但 D 的 TAF 要到 D 当日 01:00 当地才拉取(`due_tree12_taf_cities` 只处理当日)→ 入场时 `taf_forbidden_bucket_ids(D)` 恒为空。
- **修复**:新增 `tree12_taf_prefetch_local_hour=18`——当地 18:00 后预拉**明日 D+1** 的 TAF(复用 `parse_taf_extremes_for_local_day` 跨日 TX/TN 解析),使入场过滤器在窗口期真正生效;保留 D 当日 01:00 修订拉取。

### 🟡 其他发现(待实施)
- ask 区间 [0.85,0.95] 落在胜率最低区:历史低量桶 NO 胜率 99.2%(Q1)/96.8%(Q2),但其 NO 价 >0.95 被上限排除(评估上限 0.95→0.97-0.98)
- 机场-城区 basis 风险:paris→LFPB、sao-paulo→SBGR、seoul→RKSI 远机场与大折损城市重合(城市分层 A/B/C)
- limit=ask 零折价 → 被动挂单折价 0.01-0.03(参考 lihanyu81/polymarket_lp_tool)
- METAR 48 分钟老化 → AWC 实时主源化

### 开源项目借鉴(X 帖子:Recogard 10 repos)
- **SII-WANGZJ/Polymarket_data**:107GB 真实交易数据验证通过——2662 个青岛/上海已结算死桶市场(2026-05~07),NO 胜率 81.8%/输率 7.3%;成交量为分位胜率 99.2%/96.8%/85.9%/82.4%。为离线回测提供数据基础。
- 其余(backtesting 模拟器 / LP 限价单工具 / PolyWeather 同赛道)列为后续借鉴项。

### 测试
- 全套件 **194 tests 全绿**(新增:极值触发割肉 3 例、TAF 预拉 2 例、实时极值滚动更新 4 例;同步 8 个既有测试到 top3/lead 窗口语义;补装 eth_account 依赖)
- 修复 1 行为变化:瞬时穿桶不再触发割肉(旧测试 `test_metar_exit_chase_and_fak` 已更新为极值落桶场景)

### 部署
- 双容器(weatherbot-tree12-allno / allnopart)重建为修复版,全新 1000 USDC 纸面账本观测中
- 15 分钟报告 + 30 分钟 watchdog 持续监控

### 🔧 补丁 A + B(极值穿越不割 / TAF 锚定 / 开市保护)——2026-09-01 追加

承接"割肉用当日运行极值"修复,进一步消除两类残留误割:

- **补丁 A(极值精确化)**:割肉不再用 `bucket_contains(极值)`(极值在桶内即割)。
  - high:运行极值 **≥ 桶上界 hi** → 极值已越过桶,最终最高温必不在桶内 → **NO 必赢,不割**;
    极值 **< 桶下界 lo** → 未到桶,不割;
    极值在桶内(不确定态)→ 仅当 **TAF 锚定**(运行极值距 TAF 预测极值 ≤1°C,温度已封顶)或
    **已过当日峰值时段**(当地 ≥18 点,极值定型)才割,否则视为"拂过"继续持有。
  - low 对称(锚定容差 1°C / 低谷时段当地 ≥9 点)。
  - 新增 `_tree12_taf_anchor_value`(从 `taf_forecasts` 取 TAF 预测极值,缺失 fail-closed)。
- **补丁 B(开市保护)**:当日观测条数 `obs_count < 3` 或当地时间 < 07:00 不割——
  修复"开市即割"(jeddah 9/2 00:03 首条观测 34°C 即割,当日极值实际 38-40°C)。
  `update_daily_extrema_from_observation` 新增 `obs_count` 滚动计数。
- 测试:适配 `test_extreme_hit_triggers_exit` / `test_metar_exit_chase_and_fak`(fixture 补
  obs_count + 固定峰值时段 now);177/179 OK(2 error = 环境缺 eth_account,既有)。
