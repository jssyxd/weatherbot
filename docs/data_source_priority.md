# tree4 数据源优先级与失败关闭规则

## 结论

**CheckWX v2 是 tree4 的实时主数据源。** AWC（AviationWeather.gov Data API）仅承担两个确定性辅助职责：首次部署或机场当地日切换时的已发布历史 METAR/SPECI 暖机，以及 CheckWX 当前批次未返回某个 ICAO 时的单站缺口回退。**Open-Meteo、其他再分析、网格化数据、TAF 或预测输入均不参与暖机、候选或结算逻辑。**

| 场景 | 首选 | 回退条件 | AWC 请求 | 产出规则 |
|---|---|---|---|---|
| 常规实时扫描 | CheckWX `/v2/metar/{icao-list}/short` | 不适用 | 不请求 | CheckWX 返回的报告为正常主路径。 |
| CheckWX 缺报机场 | CheckWX | 指定 ICAO 不在 CheckWX 成功返回集中，或其所在 CheckWX 批次发生非 429 错误 | `GET /api/data/metar?ids={missing-icao-list}&format=json&hours=2`，仅取每 ICAO 最新已发布报告 | AWC 成功报告并入本轮观测，并在审计中标注 `source_kind=awc_fallback`。 |
| 首次/当地日暖机 | AWC（默认） | `warmup_source=awc` | `GET /api/data/metar?ids={due-icao-list}&format=json&hours=30` | 仅以当前 IANA 当地日的已发布 METAR/SPECI 重建极值；不产生候选。 |
| 可选商业历史暖机 | CheckWX `previous` | 仅显式配置 `warmup_source=auto/checkwx` 且订阅有权限 | `/v2/metar/{icao-list}/previous/{2..50}/short` | `auto` 在 CheckWX 历史失败或缺报时回退 AWC；`checkwx` 严格失败关闭。 |
| AWC 无数据、错误或超时 | 无 | AWC 回退后仍缺 ICAO，或暖机缺当前当地日记录 | 不使用任何近似数据替代 | 健康状态为 `degraded`；相应机场不产出候选。 |
| CheckWX HTTP 429 | 无 | 主源限流导致当轮 ICAO 未取得 | 仅将当轮未取得的 ICAO 交由 AWC 灾备；下一轮仍先尝试 CheckWX | AWC 成功数据可继续作为回退观测，健康状态记录主源退化；若 AWC 同样失败则对缺口失败关闭。 |

## 接口与约束

AWC 官方 Data API 支持 `/api/data/metar`，以 `ids` 约束机场、`format=json` 返回结构化报文、`hours` 指定回看窗口。官方文档说明其数据库可访问最近 30 天，但请求应限制范围和频率，单查询最多 400 条。实测显示 10 个机场、30 小时的暖机查询可能正好达到 400 条，因此当前实现默认每次仅请求 **1 个 ICAO**；若任何查询返回达到 400 条即失败关闭，绝不以可能被截断的历史重建极值。[1]

AWC JSON 中的 `icaoId`、`rawOb`、`metarType` 与 `obsTime`/`reportTime` 被转换为 tree4 的规范字段 `icao`、`raw_text` 与 `observed`。策略温度仍以 `raw_text` 进行确定性解析，优先 RMK 精确温度组。这一映射避免把供应商数值字段直接视为唯一结算口径。

## 可观测性

每条观测记录带有 `source`、`source_kind`、`source_endpoint`、`report_time_utc`、`fetched_at_utc` 与 `report_age_seconds`。CheckWX 主源另保留 `checkwx_report_age_seconds`；AWC 灾备记录另保留 `awc_report_age_seconds`。`health.json` 与 `status` 输出中的 `data_source_summary` 会记录 CheckWX 返回数量、触发 AWC 回退的 ICAO、回退返回数量、两个来源的错误和回退后仍缺失的 ICAO。

## References

[1]: https://aviationweather.gov/data/api/ "AviationWeather.gov Data API"
