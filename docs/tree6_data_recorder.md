# tree6：只读市场数据记录器

`market_ws_recorder.py` 是 **paper-only** 数据采集器。它只订阅公开 Polymarket market WebSocket、发送 `PING` 保活、将服务器原始帧与接收时间写入本地数据湖，并用 `MarketStream` 重建订阅 token 的本地订单簿作为协议健康检查。它不导入 `config.json`，不读钱包、CLOB 凭证或 API key，不调用下单、撤单或账户接口。

## 运行方式

```bash
sudo pip3 install -r requirements.txt
python3 market_ws_recorder.py \
  --duration-seconds 300 \
  --data-lake-root /srv/weatherbot-data/data_lake \
  --token-ids '<由当时市场规则快照导出的 token id>'
```

不指定 `--token-ids` 时，脚本只会临时从公开 Gamma 市场列表中选取一个活动 token，**仅用于协议烟雾测试**；生产纸面记录必须使用当时温度市场规则解析得到的 token ID，并同时归档规则快照。该采集器不负责市场发现，也不把被订阅 token 视为可交易机会。

每次运行会创建一个新的 `data_lake/dt=YYYY-MM-DD/market_ws/part-<session>.jsonl` 和同名 `health-<session>.json`。原始 part 文件以独占方式创建，不能用相同 session 覆盖；每一行包含以下链路：

| 字段 | 用途 |
|---|---|
| `received_utc` | 程序接收到原始帧的 UTC 墙钟时间。 |
| `received_monotonic_ns` | 进程内单调时钟，供测量连续到达间隔与时钟跳变。 |
| `payload` | 未经业务改写的服务器帧或 transport 事件。 |
| `payload_sha256` | 规范化 payload 哈希。 |
| `previous_payload_sha256` | 上一条 payload 的哈希，形成顺序证据链。 |
| `event_type` | `book_array`、`price_change`、`book`、`pong` 等原始分类。 |
| `metadata.socket_epoch` | 每次断线重连递增；跨 epoch 的本地簿不能视为连续。 |

## 运行边界

单个进程不得跨 UTC 日期写入一个分区；若 `--duration-seconds` 会跨午夜，程序会拒绝启动。长期部署应由进程管理器在 UTC 午夜前安全停止，并在新 UTC 日重新启动。每次断线都会使本地簿失效；只有收到新的 `book` / 初始 book array 后，相关 token 才会再次是 `ready`。

该策略是为防止“只见 price_change 却没有完整 L2 基线”的乐观回放。价格增量在初始簿之前会被记录为 `increment_before_baseline`，但不得用于纸面成交估算。

## 验收门

运行至少 5 分钟后，health 文件的下列条件必须同时成立：

```json
{
  "status": "PASS",
  "errors": [],
  "extra": {
    "protocol_status": "PASS",
    "all_books_ready": true,
    "parse_errors": {}
  },
  "safety": {
    "paper_only": true,
    "orders_submitted": 0,
    "credentials_recorded": false
  }
}
```

任何 `parse_errors`、未重建 book、跨 epoch 使用旧 book、丢失原始 payload 或接收到未知关键事件时，数据只能标记为 `BLOCKED`；后续 paper execution 不得读取该段数据。原始数据与运行产物位于 `.gitignore` 中，必须由独立的加密备份/对象存储策略归档，而不是推送到 Git。
