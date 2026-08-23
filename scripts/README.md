# scripts/

Hermes 运维脚本（与 tree1 观察器配套）：

- `polymarket_consensus_scan.py` — Gamma 事件共识扫描（每 30 分钟）。
  **突破即报，无稳定时长门槛**：同市场当地日内，高温 mode 向上移动或低温
  mode 向下移动即记为一个 breakout——一条新的 METAR/SPECI 观测突破桶边界
  后，被突破的桶立即成为事实死桶，可立即买 NO。脚本只做只读 GET、从不
  下单；stable_minutes 仅作信息展示，不作判定门槛。
  部署位置：`~/.hermes/scripts/`（cron 直接引用，此目录为版本化副本）。
