# METAR/SPECI Dead-Bucket Observer（tree4）

**tree4** 在 tree3 的本地盘口、纸面 FAK 与严格失败关闭边界之上，已将航空实况唯一切换为 **CheckWX Aviation Weather API v2**。实时数据路径使用 `GET /v2/metar/{icao-list}/short`，在请求头中传递 `X-API-Key`；不再请求 AviationWeather.gov / AWC，也不解析其专有 JSON 字段。CheckWX 的短格式仅提供 ICAO、原始报文与 `observed` UTC 时刻，因此 tree4 从原始 METAR/SPECI 正文确定性解析温度，并在存在 RMK `T` 精确温度组时优先使用 0.1°C 精度。[1] [2]

> **策略边界没有改变。** METAR/SPECI 只是候选观测，不是结算源；本仓库没有钱包加载、私钥读取、真实订单提交或撤单能力。`live` 模式仍只写入阻断审计记录，绝不交易。

| 项目 | tree4 行为 |
|---|---|
| 当前实况请求 | `GET https://api.checkwx.com/v2/metar/{最多25个ICAO}/short`，使用 `X-API-Key` 请求头。 |
| 温度口径 | 优先 `RMK T[01][TTT][DDHH]` 的 0.1°C；否则确定性解析正文 `M?DD/M?DD` 整数摄氏度。 |
| 时间口径 | 用 CheckWX `observed` UTC 归属机场 IANA 当地自然日；`fetched_at` 仅用于 `observed` 至本地抓取的绝对新鲜度门。 |
| 批量与扫描 | 每次最多 25 个 ICAO；按 tree4 的运行要求默认每 60 秒全量扫描。CheckWX 文档仍建议缓存 METAR/TAF 至少 15 分钟，因此需通过运行日志持续监控 429 与实际首见收益。[1] |
| 暖机 | 启动/当地日切换时使用 `/v2/metar/{icao-list}/previous/{2..50}/short` 重新构建当天极值；若历史接口权限、响应或当前日数据不足，则保持失败关闭，不产生候选。 |
| 非目标数据 | 不使用 TAF、ECMWF、任何预测输入、Station、BOT、G-AIRMET 或 URL 查询参数密钥模式。 |

## 安装与安全配置

项目保持 Python 标准库的 CheckWX 请求实现；既有订单签名测试依赖仅在运行完整测试集时需要安装。先由示例配置创建本地配置，再将 API 密钥只注入启动进程环境。`config.json`、`.env` 和 `data/` 已被忽略，真实密钥绝不可写入仓库、配置 JSON、请求 URL、审计记录或截图。[1]

```bash
git clone --branch tree4 https://github.com/jssyxd/weatherbot.git
cd weatherbot
cp config.example.json config.json
export CHECKWX_API_KEY='在此设置你的密钥'
python3 metar_observer.py status
python3 metar_observer.py once
python3 metar_observer.py run
```

| 配置项 | 默认值 | 含义 |
|---|---:|---|
| `checkwx_api_key_env` | `CHECKWX_API_KEY` | 保存密钥的环境变量名，而非密钥本身。 |
| `scan_interval_seconds` | `60` | CheckWX METAR 全量轮询间隔；不得低于 60 秒。 |
| `rate_limit_backoff_seconds` | `60` | 收到 HTTP 429 后的最小退避时间；若服务端提供更长 `Retry-After`，优先采用更长值。 |
| `stations_per_request` | `25` | 当前 METAR 批量 ICAO 数；不得超过接口上限。 |
| `checkwx_previous_limit` | `50` | 每站暖机历史数量，支持范围为 2–50。 |
| `max_report_age_seconds` | `900` | `observed` 到本次抓取的最大可接受龄期。 |
| `warmup_stations_per_request` | `25` | 暖机历史请求分块大小。 |
| `warmup_retry_seconds` | `900` | 暖机失败重试节奏。 |

## 历史暖机权限与失败关闭

CheckWX 文档将历史 METAR 端点归类为高级端点；该端点需要相应的订阅权限。[2] 在这次 tree4 实施的受控实测中，当前密钥可以成功返回 `/v2/metar/KLAX/short`，但 `/v2/metar/KLAX/previous/50/short` 返回 HTTP 403，并提示需要 premium API plan。因此当前代码会继续采集和审计实时短格式 METAR，但**不会把未完成暖机的当天数据推进到候选信号状态机**。这是刻意的安全设计，避免在当天既有极值未知时错误判定温度桶已经失效。

若要完整启用 tree4 的候选逻辑，需让所用 CheckWX 订阅具有历史 METAR 权限；随后无需改动代码，只需以该环境变量重启观察器。任何 401、403、429、非 JSON 响应、`results` 与 `data` 数量不一致，或当前当地日没有可用历史报文，都会记录为退化状态并保持不动作。

## 时效性验证而非预设结论

切换 CheckWX 的目的是检验其数据可用时间是否早于旧免费来源；这并不是已被本仓库证明的结论。tree4 按运行要求每 60 秒扫描一次，并会在每个审计事件中保留 `report_time_utc`、`fetched_at_utc` 和 `checkwx_report_age_seconds`，以同一 ICAO、相同 `raw_metar` 对比首见时间。CheckWX 文档仍建议 METAR/TAF 至少缓存 15 分钟；因此 429、响应龄期与实际首见收益必须被持续审计，不能仅以扫描频率宣称更快。[1]

建议持续运行至少数周，特别覆盖整点 METAR、SPECI、机场当地午夜、夏令时切换和网络异常场景。只有在样本量、机场集合、轮询节奏及“首见”的定义一致时，才应判断 CheckWX 是否在目标机场上更早可用。

## 测试

```bash
sudo pip3 install -r requirements.txt
python3 -m unittest discover -s tests -v
```

当前测试覆盖 CheckWX 请求头鉴权、密钥不进入 URL、短格式 JSON 契约、25 ICAO 上限、60 秒扫描门、HTTP 429 的 `Retry-After` 退避、历史暖机失败关闭、IANA 当地日、正文和 RMK 温度解析、华氏精度保护、本地订单簿以及纸面 FAK 风控边界。它们不调用真实 API，也不会发出订单。

## tree3 保留边界

tree3 的本地 WebSocket 盘口、完整 ask 深度、固定 5 shares FAK 纸面估算、审计账本、价格保护与 `live` 阻断仍保持原状。tree4 只替换了航空实况供应商与与之绑定的配置、标准化、暖机、审计字段和测试；它不将 `midpoint`、成交价或 bid 误作可买入的 NO ask，也不新增任何真实执行路径。

## References

[1]: https://www.checkwxapi.com/documentation/introduction "CheckWX Introduction"
[2]: https://www.checkwxapi.com/documentation/metar "CheckWX METAR endpoints"
