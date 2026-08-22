# METAR/SPECI Observer

这是一个**只读的机场气象观测扫描器**。它每分钟从 [AviationWeather.gov Data API](https://aviationweather.gov/data/api/) 获取已经发布的 METAR 与 SPECI，筛选新出现的报文，并以可审计的 JSONL 事件日志保存。

本项目从 `technosheen/weatherbot` 的 MIT 许可代码库派生，但已移除所有预测、模型集成、市场请求、下单、钱包、结算、回测、凯利公式和收益计算代码。它**不会**调用任何数值天气预报、市场或交易接口。

> METAR/SPECI 是已发布的观测报告，不是预测。本工具仅记录报告内容和各时间戳；它不会发出交易指令，也不会替任何用户进行买卖。

## 功能

每个扫描周期会以合规的批量请求读取配置中的 ICAO 站点，并仅处理 `METAR` 与 `SPECI` 类型。新报文会按 `ICAO + 报告类型 + 报告时间 + 原始报文` 去重，随后打印到标准输出，并追加写入按 UTC 日期分割的 JSONL 文件。

| 保留功能 | 行为 |
|---|---|
| 每分钟扫描 | 默认间隔为 60 秒；扫描周期自动对齐到下一分钟边界。 |
| 已发布观测 | 只读取 AWC 已接收的 METAR/SPECI，不进行数值天气预报。 |
| 多机场批量查询 | 默认包含原项目覆盖的 35 个机场；配置可增删 ICAO。 |
| SPECI 捕获 | 例行 METAR 与非例行 SPECI 都保留，便于分析不规则天气变化。 |
| 审计时间线 | 保存报文时间、AWC `receiptTime`、本机 UTC 抓取时间以及原始报文。 |
| 去重与重启恢复 | 过去 72 小时的事件 ID 保存在状态文件，重启不会重复输出旧事件。 |
| 只读存储 | 输出为 JSONL 和状态 JSON；不需要 API 密钥、钱包或私钥。 |

## 不包含的内容

| 已移除的能力 | 状态 |
|---|---|
| 数值天气预报模型与模型集成 | 已删除。 |
| 集成预测、偏差校正、温度桶概率、EV、凯利仓位 | 已删除。 |
| 市场查询、订单提交、取消、卖出 | 已删除。 |
| 钱包、私钥、链上赎回、余额与仓位管理 | 已删除。 |
| 预测回测、校准、胜率学习与收益报表 | 已删除。 |

## 安装与首次运行

本项目只使用 Python 标准库，Python 3.10+ 即可运行，不需要安装第三方包。

```bash
git clone https://github.com/jssyxd/weatherbot.git
cd weatherbot
cp config.example.json config.json
python3 metar_observer.py once
```

首次 `once` 执行会输出并写入过去一小时内可见的报文，作为本地事件基线。以后运行只会输出首次看到的新报文；去重状态保存在 `data/state.json`。

```bash
# 连续运行；每分钟扫描一次
python3 metar_observer.py run

# 查看最近一次扫描与去重状态
python3 metar_observer.py status

# 使用其他配置文件
python3 metar_observer.py once --config /path/to/config.json
```

## 配置

`config.example.json` 的默认站点与原项目的 35 个机场一致。可以仅保留你需要的 ICAO，例如只扫描 ZSPD 与 RCTP：

```json
{
  "scan_interval_seconds": 60,
  "history_hours": 1,
  "stations_per_request": 35,
  "state_path": "data/state.json",
  "event_dir": "data/observations",
  "stations": [
    {"icao": "ZSPD", "name": "Shanghai Pudong"},
    {"icao": "RCTP", "name": "Taiwan Taoyuan"}
  ]
}
```

| 字段 | 含义 | 约束 |
|---|---|---|
| `scan_interval_seconds` | 两次扫描之间的秒数 | 不得低于 60；AWC 完整缓存按分钟更新。 |
| `history_hours` | 每次查询回看的小时数 | 1–24；去重防止回看窗口内重复写入。 |
| `stations_per_request` | 单次 AWC 请求合并的站点数 | 1–100；默认 35，降低请求数。 |
| `state_path` | 去重与扫描状态文件 | 相对路径以项目目录为基准。 |
| `event_dir` | JSONL 事件目录 | 按 UTC 日期生成文件。 |
| `stations` | `icao` 与可选 `name` 的数组 | 仅应填写有效的四位 ICAO 代码。 |

## 事件格式

每条 JSONL 记录都可以独立解析，包含如下关键字段：

```json
{
  "airport_icao": "ZSPD",
  "report_type": "METAR",
  "report_time_utc": "2026-08-22T12:00:00Z",
  "receipt_time_utc": "2026-08-22T12:05:16.281Z",
  "awc_receipt_delay_seconds": 316.281,
  "fetched_at_utc": "2026-08-22T12:06:00Z",
  "temperature_c": 28,
  "raw_metar": "METAR ZSPD 221200Z 12007MPS 9999 BKN016 28/26 Q1005 NOSIG"
}
```

三个时间字段不能互换：`report_time_utc` 是报文时刻，`receipt_time_utc` 是 AWC 记录的接收时间，`fetched_at_utc` 是扫描器自己的抓取时间。`awc_receipt_delay_seconds` 仅在两个源时间戳一致且非负时计算；否则为 `null`，并以 `awc_receipt_delay_status=source_time_inconsistent` 标记。它们都不等同于机场传感器的原始采样时刻。

## 持续运行方式

每分钟扫描需要一个在后台持续运行的环境。以下两种方式都可行；请选择符合你的成本、可维护性和在线要求的方案。

| 方式 | 运行效果 | 成本与限制 | 适合场景 |
|---|---|---|---|
| 本机后台进程 | 在你的电脑持续运行 `python3 metar_observer.py run` | 无额外托管费用，但电脑、网络和进程必须持续在线 | 先验证时延、仅个人使用、可接受本机中断。 |
| 托管的后台任务/常驻服务 | 部署到可持续运行的托管环境 | 取决于所选服务；需要配置日志、重启与持久化存储 | 需要 24/7 扫描且不依赖个人电脑。 |

在 Linux 主机上可先使用进程管理器或服务管理器启动；部署前请确保 `data/` 目录位于持久化磁盘，并监控进程重启与 API 错误日志。不要使用一次启动一个完整 AI 会话的分钟级定时任务来运行此类确定性轮询。

## 数据源与限流

上游使用 [AviationWeather.gov Data API](https://aviationweather.gov/data/api/)。其文档说明 METAR 覆盖全球、全量缓存每分钟更新、API 限制为每分钟最多 100 次请求，并建议按产品更新频率控制请求。默认配置将 35 个 ICAO 站合并为一个请求并以 60 秒间隔运行，远低于该限制。

该 API 是**观测再发布层**。不同机场的上游观测、例行分钟相位和 SPECI 触发逻辑不同；AWC `receiptTime` 也不等于 Wunderground、其他聚合网站或任何市场的页面显示时间。

## 许可证与来源

本仓库保留源仓库的 [MIT License](LICENSE)。初始派生来源为 [technosheen/weatherbot](https://github.com/technosheen/weatherbot)，随后进行了非预测、非交易的观测扫描器重构。
