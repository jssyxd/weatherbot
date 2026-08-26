# CheckWX 文档调研记录

来源入口：<https://www.checkwxapi.com/documentation/introduction>

## 已确认的通用约束

| 项目 | 结论 |
|---|---|
| 服务地址 | `https://api.checkwx.com/` |
| 推荐认证 | HTTP 请求头 `X-API-Key: <密钥>` |
| 当前版本 | 文档说明当前版本为 v2，端点路径应含 `/v2/`；但文档的首个请求示例也展示了无版本形式，实施前需实测确认 v2 METAR 端点。 |
| 数据格式 | UTF-8 JSON 对象，顶层含 `results` 与 `data` 数组，时间戳为 UTC。 |
| 批量 ICAO | 一个请求最多 25 个、以逗号分隔的 ICAO 代码。 |
| 限额与缓存 | 每次 HTTP 调用均计为一次；官方建议 METAR/TAF 缓存至少 15 分钟，超限返回 HTTP 429，计数在 UTC 00:00 重置。 |
| 错误处理 | 200 成功；常见错误包括 400、401、403、404、422、429、500。 |
| 安全要求 | API 密钥不得写入公开代码、客户端 JavaScript 或公开仓库。 |

## 对 tree4 的初步实施含义

1. 仅在服务端/执行环境中读取 `CHECKWX_API_KEY`，提交 `.env.example` 而不提交真实密钥。
2. 将监测机场按最多 25 个 ICAO 分块，避免 N+1 请求。
3. 保留/落实不短于 15 分钟的缓存或轮询间隔，针对 429 与网络故障使用可观测日志和退避策略。
4. 查询路径和响应字段要以 CheckWX 实际 v2 文档页及一次受控实测为准，不能沿用 AWC 的响应结构。

## 全页遍历结果与 tree4 选型

| 文档页面 | 已确认内容 | tree4 决策 |
|---|---|---|
| Introduction | HTTPS REST、`X-API-Key`、JSON 顶层结构、限额与缓存建议。 | 使用服务端 Header 认证，封装超时、HTTP 状态、JSON 结构校验与 429 处理。 |
| Version 2 | `v2` 是当前版本；原始报文端点无需因 v1/v2 结构迁移而改变；短格式含 `icao`、`raw_text`、`observed`。 | 使用 `GET /v2/metar/{icao-list}/short`，因为策略核心仅需可审计的原始 METAR 与观测时间；不依赖易变的深层 decoded 字段。 |
| METAR | ICAO 批量端点为 `/v2/metar/{icao}`，默认返回 raw；`/short` 返回 ICAO、原文、观测 UTC；`/decoded` 提供温度、风、能见度、类别等字段。单次最多 25 ICAO。还提供历史、国家/州、近邻和半径端点（部分为高级功能）。 | 使用批量 `/short` 作为实时链路；温度仍须从原始 METAR（含 RMK 精确温度组）确定性解析，避免数据源字段口径变化。当前不将历史/近邻/半径/国家/州能力纳入策略关键路径。 |
| TAF | 与 METAR 同样支持 raw/short/decoded、批量上限 25、近邻/半径/历史等。 | 不使用。tree4 维持纯观测逻辑，避免重新引入预测输入。 |
| G-AIRMET | 可按 ICAO 或坐标查询有效风险区域，含短格式和 GeoJSON decoded 格式。 | 不使用。不是该策略的既定数据需求。 |
| Station | 可批量查询站点元数据、时区、日出日落和近邻/半径。 | 不作为实时链路依赖；继续使用项目已核验的合同机场配置和 IANA 时区，以避免额外请求和运行时口径变动。 |
| BOT | 面向无法解析 JSON 的单行文本端点，鉴权只支持查询参数。 | 禁用，不使用；该形式会暴露密钥于 URL、日志和代理记录，且无法提供 `observed` 字段。 |
| Code Samples | 展示 Header 和 query 参数两种认证；Fetch 示例用 Header。 | 采用 Header，不将密钥拼接到 URL。 |

## 关键请求契约

```text
GET https://api.checkwx.com/v2/metar/KJFK,KLAX/short
X-API-Key: ${CHECKWX_API_KEY}
Accept: application/json
```

成功返回的数据必须是对象，`results` 为非负整数，`data` 为数组。tree4 对 `/short` 的每个元素至少校验：`icao` 为预期 4 字符代码、`raw_text` 为非空字符串、`observed` 为可解析的 UTC 时间戳。若失败则对相关 ICAO 记录审计失败，绝不以不完整数据推进极值状态。

## 资料范围与来源

本次已完整读取公开 Documentation 导航的所有专题页：Introduction、Version 2、METAR、TAF、G-AIRMET、Station、BOT 与 Code Examples。实现仅采纳当前目标所需的 METAR v2 短格式端点；其余页面被审阅以确认未误接入预测、地理扩展或 URL 密钥模式。

[1]: https://www.checkwxapi.com/documentation/introduction
[2]: https://www.checkwxapi.com/documentation/version2
[3]: https://www.checkwxapi.com/documentation/metar
[4]: https://www.checkwxapi.com/documentation/taf
[5]: https://www.checkwxapi.com/documentation/airmet
[6]: https://www.checkwxapi.com/documentation/station
[7]: https://www.checkwxapi.com/documentation/chatbot
[8]: https://www.checkwxapi.com/documentation/code/samples
