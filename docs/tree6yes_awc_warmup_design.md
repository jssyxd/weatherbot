# tree6yes：2 分钟 CheckWX 与 AWC 暖机/灾备设计

## 目标与边界

本次变更将付费的 CheckWX 当前 METAR/SPECI 请求从原先 60 秒降频至 **120 秒**。CheckWX 继续是正常运行时的唯一首选数据源；AWC 不是预测、网格再分析或结算替代数据，也不参与温度合约的价格预测。

AWC 仅在两个确定性的已发布观测场景使用：首次启动或机场当地日切换时，读取当日的已发布 METAR/SPECI 重建日内极值；以及某个 CheckWX 当前请求成功但没有返回该 ICAO、或该请求异常时，读取该 ICAO 的近期已发布 METAR/SPECI 作为灾备。所有下游输入统一归一化为 ICAO、原始 METAR/SPECI 正文和报告时间，再按 IANA 当地日过滤。AWC 返回的 `temp` 等解码字段不会绕开现有的原始报文温度解析器。

> 数据源不可达、响应格式异常、204 无数据、暖机后没有当前当地日的有效 METAR/SPECI、订单簿不新鲜或市场规则不明确时，策略保持失败关闭。不会使用 TAF、预报、格点/再分析数据，也不会生成真实订单。

## AWC 请求形态与流量控制

AWC 的官方 Data API 支持 `GET /api/data/metar?ids=<ICAO>&format=json&hours=48`，数据库可访问最多前 30 天的数据；该 API 将多数端点单次结果限制为 400 条，并要求限制请求范围与频率。[1]

因此历史暖机固定**逐 ICAO**调用 AWC，使用 `hours=48` 覆盖全球机场在 UTC 与 IANA 当地日边界附近的当日已发布报文，之后再以本地日期精确筛选。逐站请求避免多站 48 小时查询超过单次返回上限而形成静默截断。实时灾备也逐 ICAO 请求，使用 `hours=2`；运行时仍受 `max_report_age_seconds` 约束，过时报文只审计、不驱动温度确认或换手。

| 流程 | 首选 | 何时使用 AWC | AWC 请求 | 不可用时 |
| --- | --- | --- | --- | --- |
| 正常扫描 | CheckWX `/v2/metar/.../short`，每 120 秒 | CheckWX 请求异常或响应未含目标 ICAO | 每个遗漏 ICAO 查询 2 小时 | 该站本轮无数据；记录来源/错误，不伪造事件 |
| 当前日暖机 | AWC 已发布 METAR/SPECI | 每次首次运行、当地日切换或暖机重试 | 每个待暖机 ICAO 查询 48 小时 | 写入 `failed_awc_fetch` 或 `failed_no_current_local_day_reports`；该站不产生策略信号 |

## 归一化与审计

AWC JSON 的 `icaoId`、`rawOb`、`reportTime` 和 `metarType` 映射到现有标准事件的 `icao`、`raw_text`、`observed` 与原始正文。每条归一化事件保留 `source`、`source_endpoint`，以区分 `CheckWX Aviation Weather API v2`、`AviationWeather.gov Data API (warmup)` 与 `AviationWeather.gov Data API (fallback)`。若 AWC 的原始正文缺失 `METAR` 或 `SPECI` 前缀，则仅在 `metarType` 可信为这两种已发布观测时补上对应前缀。

## 参考资料

[1] [AviationWeather.gov Data API](https://aviationweather.gov/data/api/)
