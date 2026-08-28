# METAR/SPECI Dead-Bucket Observer（tree4）

**tree4** 在 tree3 的本地盘口、纸面 FAK 与严格失败关闭边界之上，以 **CheckWX Aviation Weather API v2** 作为实时主数据源。正常扫描使用 `GET /v2/metar/{icao-list}/short` 并在请求头传递 `X-API-Key`。**仅**当首次/当地日切换需要当天历史暖机，或 CheckWX 未返回某个 ICAO 时，才调用官方 **AviationWeather.gov / AWC Data API** 的已发布 METAR/SPECI 作为确定性辅助源；不使用 Open-Meteo、TAF、再分析或预测数据。CheckWX 与 AWC 均从原始 METAR/SPECI 正文确定性解析温度，并在存在 RMK `T` 精确温度组时优先使用 0.1°C 精度。[1] [2] [3]

> **策略边界没有改变。** METAR/SPECI 只是候选观测，不是结算源；本仓库没有钱包加载、私钥读取、真实订单提交或撤单能力。`live` 模式仍只写入阻断审计记录，绝不交易。

| 项目 | tree4 行为 |
|---|---|
| 当前实况主请求 | `GET https://api.checkwx.com/v2/metar/{最多25个ICAO}/short`，使用 `X-API-Key` 请求头。 |
| 灾备回退 | 仅对 CheckWX 未返回的 ICAO 请求 `GET https://aviationweather.gov/api/data/metar?ids={icao-list}&format=json&hours=2`；不覆盖 CheckWX 已返回的站点。 |
| 暖机历史 | 默认用 AWC `hours=30` 重建每个机场当前 IANA 当地日极值；`warmup_source=checkwx/auto` 仅适用于具有 CheckWX `previous` 权限的部署。 |
| 温度口径 | 优先 `RMK T[01][TTT][DDHH]` 的 0.1°C；否则确定性解析正文 `M?DD/M?DD` 整数摄氏度。 |
| 时间口径 | 用各已发布报告的 `observed` UTC 归属机场 IANA 当地自然日；`fetched_at` 仅用于 `observed` 至本地抓取的绝对新鲜度门。 |
| 批量与扫描 | CheckWX 每次最多 25 个 ICAO；按 tree4 的运行要求默认每 120 秒全量扫描。CheckWX 文档仍建议缓存 METAR/TAF 至少 15 分钟，因此需通过运行日志持续监控 429 与实际首见收益。[1] |
| 暖机 | 启动/当地日切换时默认以 AWC `hours=30` 重新构建当天极值；若 AWC 不可达、响应无效或当前当地日数据不足，则保持失败关闭，不产生候选。 |
| 纸面执行默认 | `mode=paper` 且 `execution_engine=legacy`：候选信号走 `simulate_paper_fak` 纸面成交估算，**不**要求本地 WebSocket 盘口。 |
| 非目标数据 | 不使用 TAF、ECMWF、任何预测输入、Station、BOT、G-AIRMET 或 URL 查询参数密钥模式。 |

## Changelog

### grok20260828

- **执行引擎默认改为可纸面成交**：`config.example.json` 中 `execution_engine` 由 `tree3` 改为 `legacy`，`mode` 由 `observe` 改为 `paper`，`market_ws_enabled` 改为 `false`。
- **原因**：`tree3` 在主循环未附着本地 WebSocket 盘口时固定返回 `LOCAL_BOOK_PATH_NOT_ATTACHED`，候选无法产生纸面成交估算；`legacy` 使用既有 `simulate_paper_fak`，在暖机完成且 `market_rules` 新鲜时即可写出纸面执行结果。
- **可选**：若已挂好本地盘口并需要 tree3 深度/盘口路径，可在本地 `config.json` 将 `execution_engine` 改回 `tree3` 并设置 `market_ws_enabled=true`。
- **未改动**：CheckWX/AWC 数据路径、暖机失败关闭、`market_rules_stale` 拦截、`live` 无真实下单边界均保持原样。

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
| `mode` | `paper` | 运行模式：`observe` / `paper` / `live`（`live` 仅审计阻断，不下单）。 |
| `execution_engine` | `legacy` | 纸面执行引擎：`legacy`（默认，FAK 纸面估算）/ `tree2` / `tree3`（需本地 WebSocket 盘口，否则 `LOCAL_BOOK_PATH_NOT_ATTACHED`）。 |
| `checkwx_api_key_env` | `CHECKWX_API_KEY` | 保存密钥的环境变量名，而非密钥本身。 |
| `scan_interval_seconds` | `120` | CheckWX METAR 全量轮询间隔；不得低于 60 秒。 |
| `rate_limit_backoff_seconds` | `120` | 收到 CheckWX HTTP 429 后的最小退避时间；若服务端提供更长 `Retry-After`，优先采用更长值。 |
| `stations_per_request` | `25` | CheckWX 当前 METAR 批量 ICAO 数；不得超过接口上限。 |
| `warmup_source` | `awc` | 暖机源：默认 AWC；可选 `auto`（CheckWX 历史后 AWC）或 `checkwx`（仅有 `previous` 权限时）。 |
| `awc_warmup_history_hours` | `30` | AWC 暖机回看窗口，覆盖当前 IANA 当地日；范围 1–48 小时。 |
| `awc_fallback_history_hours` | `2` | CheckWX 缺报时 AWC 回退窗口；每个站点只取最新已发布报告。 |
| `awc_stations_per_request` | `10` | AWC 回退与暖机分块大小。 |
| `checkwx_previous_limit` | `50` | 仅在 `warmup_source=auto/checkwx` 下使用；每站历史数量范围 2–50。 |
| `max_report_age_seconds` | `900` | `observed` 到本次抓取的最大可接受龄期。 |
| `warmup_stations_per_request` | `25` | 暖机历史请求分块大小。 |
| `warmup_retry_seconds` | `900` | 暖机失败重试节奏。 |

## 历史暖机权限与失败关闭

CheckWX 文档将历史 METAR 端点归类为高级端点；该端点需要相应的订阅权限。[2] 在这次 tree4 实施的受控实测中，当前密钥可以成功返回 `/v2/metar/KLAX/short`，但 `/v2/metar/KLAX/previous/50/short` 返回 HTTP 403，并提示需要 premium API plan。tree4 因而默认不再请求该付费历史端点，而改以 AWC 已发布历史进行暖机；**任何暖机仍未完成的城市都不会被推进到候选信号状态机**，避免在当天既有极值未知时错误判定温度桶已经失效。

tree4 默认无需 CheckWX `previous` 权限即可尝试暖机，因为暖机使用 AWC 已发布的历史 METAR/SPECI。任何 AWC/CheckWX 网络或响应异常、CheckWX HTTP 429、当前当地日没有足够可用历史报文，或 AWC 回退后仍有缺报机场，都会记录为退化状态并保持失败关闭。若部署具备 CheckWX `previous` 权限，可显式将 `warmup_source` 改为 `auto` 或 `checkwx`。

## 时效性验证而非预设结论

切换 CheckWX 的目的是检验其数据可用时间是否早于旧免费来源；这并不是已被本仓库证明的结论。tree4 按运行要求每 120 秒扫描一次，并在审计事件中保留 `source`、`source_kind`、`report_time_utc`、`fetched_at_utc` 和通用 `report_age_seconds`；CheckWX 主源另保留 `checkwx_report_age_seconds`，AWC 回退则保留 `awc_report_age_seconds`。因此可以在同一 ICAO、相同 `raw_metar` 下对比各源首见。CheckWX 文档仍建议 METAR/TAF 至少缓存 15 分钟；429、响应龄期与实际首见收益必须被持续审计，不能仅以扫描频率宣称更快。[1]

建议持续运行至少数周，特别覆盖整点 METAR、SPECI、机场当地午夜、夏令时切换和网络异常场景。只有在样本量、机场集合、轮询节奏及“首见”的定义一致时，才应判断 CheckWX 是否在目标机场上更早可用。

## 测试

```bash
sudo pip3 install -r requirements.txt
python3 -m unittest discover -s tests -v
```

当前测试覆盖 CheckWX 请求头鉴权、密钥不进入 URL、AWC 官方 JSON 映射、AWC 最新报文选择、CheckWX 缺报时仅对缺口 ICAO 的 AWC 回退、25 ICAO 上限、120 秒默认扫描、HTTP 429 的 `Retry-After` 退避、AWC 暖机失败关闭、IANA 当地日、正文和 RMK 温度解析、华氏精度保护、本地订单簿以及纸面 FAK 风控边界。它们不调用真实 API，也不会发出订单。

## tree3 保留边界

`execution_engine=tree3` 时仍要求本地 WebSocket 盘口；主循环未附着真实行情时 fail-closed（`LOCAL_BOOK_PATH_NOT_ATTACHED`）。tree3 的完整 ask 深度、固定 5 shares FAK 纸面估算、审计账本、价格保护与 `live` 阻断能力仍保留在代码中，可在本地配置中显式启用。tree4 默认使用 `legacy` 纸面路径，只替换了航空实况供应商与与之绑定的配置、标准化、暖机、审计字段和测试；它不将 `midpoint`、成交价或 bid 误作可买入的 NO ask，也不新增任何真实执行路径。

## References

[1]: https://www.checkwxapi.com/documentation/introduction "CheckWX Introduction"
[2]: https://www.checkwxapi.com/documentation/metar "CheckWX METAR Documentation"
[3]: https://aviationweather.gov/data/api/ "AviationWeather.gov Data API"
