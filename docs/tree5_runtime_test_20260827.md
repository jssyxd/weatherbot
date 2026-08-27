# Tree5 隔离观察模式运行记录

**运行日期：**2026-08-27 UTC

**运行时长：**5 分钟（`timeout -s INT 305s`，由测试控制器正常中断）

**运行模式：**`observe`；`tree5_enabled=false`；无钱包、无 CLOB 凭证、无真实下单

**运行数据目录：**`/tmp/weatherbot-tree5-runtime`（未写入仓库）

## 测试目的与配置

本次运行验证以下三项变更：CheckWX 实时查询每 120 秒一次；本地日 warm-up 以 AviationWeather.gov 历史 METAR 作为确定性灾备；CheckWX 返回遗漏站点或请求失败时，仅对缺失站点再请求 AviationWeather.gov 最近两小时的 METAR。

| 配置项 | 值 | 说明 |
|---|---:|---|
| `scan_interval_seconds` | 120 | CheckWX 主实况扫描不快于两分钟一次。 |
| `warmup_source` | `aviationweather` | 因隔离环境未配置 CheckWX 密钥，测试专门验证免费历史灾备路径。 |
| `aviationweather_warmup_hours` | 48 | 用于重建每城当前 IANA 当地日的高低温基线。 |
| `aviationweather_realtime_fallback_hours` | 2 | 仅在主源缺报时查询的近期观测窗口。 |
| `market_metadata_timeout_seconds` | 3 | 单个 Gamma 元数据请求的硬上限。 |
| `market_refresh_deadline_seconds` | 5 | 为隔离测试设置的总上限；生产示例默认 30 秒。 |

## 结果

测试进程在五分钟内完成了启动 warm-up、两次完整扫描、市场元数据超时降级和受控停止，`stderr` 中没有 Python traceback。两次扫描均产生了正常的观察事件，且没有下单行为。

| 指标 | 观察值 | 结论 |
|---|---:|---|
| 完整扫描次数 | 2 | 使用 120 秒配置；每轮还受到有限制的市场元数据刷新所占时间影响，因此不会超过请求频率上限。 |
| 实时报告 | 第 1 轮 135 条；第 2 轮 147 条 | 报文被正常归一化与去重。 |
| 完成 warm-up 的城市 | 33 / 49 | 这些城市取得了足以建立当前本地日极值的 AviationWeather 观测。 |
| 未完成 warm-up 的城市 | 16 / 49 | 这些站点记录为 `aviationweather_network_error`；仍保持失败关闭。 |
| 未捕获异常 | 0 | 市场规则、历史源与实时源的网络错误均被转换为显式状态，而非终止主循环。 |
| 真实订单 | 0 | 观察模式且仓库没有真实执行器。 |

> **失败关闭结论：**16 个 AviationWeather 历史请求未成功时，对应城市持续处于 `untrusted_warmup`，不会产生候选信号。系统没有使用 Open-Meteo、预报或再分析数据填补基线。

## 本轮修复

初次五分钟运行暴露出 Gamma 市场规则启动刷新可能在上游 TLS/分块响应卡住，从而阻塞整个观察器。现已为市场规则增加 **3 秒单请求超时**和**30 秒全局刷新期限**；未完成的城市/方向明确记录 `market_discovery_deadline_exceeded`，候选链路照旧失败关闭。运行日志中此前将所有报文龄期硬标作“CheckWX”的问题也已修复，现在会记录实际来源，避免灾备报文被错误归因。

## 当前限制与部署建议

AviationWeather.gov 官方文档说明其数据 API 支持历史 METAR 查询、请求应控制范围与频率、常规端点受到速率限制；该站在本次环境中对一部分分批请求发生网络 TLS/连接错误。[1] 这是上游可达性限制，不是可通过重试或合成数据安全修复的应用错误。生产部署应保留 `warmup_source=auto`，优先使用有权限的 CheckWX `previous`，并在其失败时使用当前灾备；任一城市仍无已发布本地日观测时，继续不交易该城市。

## 复现命令

```bash
cd /home/ubuntu/weatherbot-tree5
python3 -m unittest discover -s tests -v
timeout -s INT 305s python3 metar_observer.py run --config /tmp/weatherbot-tree5-runtime-config.json
```

第二条命令所用配置是本次隔离测试文件，不应复制到生产：它刻意引用不存在的 CheckWX 环境变量，且把市场刷新总时限缩短为 5 秒以限制测试外部请求。

## References

[1]: https://aviationweather.gov/data/api/ "Aviation Weather Center Data API"
