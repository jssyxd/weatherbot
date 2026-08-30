# weatherbot — tree12-allnopart

从 tree12-allno 派生。三项核心改动：

1. **开仓窗口**：当地日开始前 **>18 小时**（原 >24h）。
2. **只交易 10 个高流动性 + METAR ≤30min 刷新的机场**，其余城市完全不拉 METAR/TAF/订单簿、不参与策略。
3. 默认仍为 **paper**。live 需显式配置私钥且代码路径仍要求仓位对账。

## 白名单城市（10）

| city_id | ICAO | 备注 |
|---------|------|------|
| shanghai | ZSPD | 通常最高量 |
| beijing | ZBAA | |
| tokyo | RJTT | |
| seoul-incheon | RKSI | |
| paris | LFPB | |
| madrid | LEMD | |
| amsterdam | EHAM | |
| munich | EDDM | ~20min METAR |
| istanbul | LTFM | ~20min METAR |
| singapore | WSSS | |

伦敦 EGLC 因 TAF 有效期过短（常 <18h）未纳入。

## 快速开始（paper）

```bash
git clone --branch tree12-allnopart https://github.com/jssyxd/weatherbot.git
cd weatherbot
cp config.example.json config.json
# 把 contract_cities_path 改成 config/contract_cities_part.json
# 或直接把 contract_cities_part.json 覆盖为 contract_cities.json
cp .env.example .env
# 编辑 .env 填入 CHECKWX_API_KEY
export $(grep -v '^#' .env | xargs)
python3 metar_observer.py status
python3 metar_observer.py once
python3 metar_observer.py run
```

## 接入实盘（高风险）

1. 在受控机器上创建 `.env`，填入：
   - `CHECKWX_API_KEY`
   - `POLYMARKET_PRIVATE_KEY=0x...`（Polygon 钱包私钥）
2. `config.json` 中设置 `"mode": "live"`（当前仓库 live 执行器仍为阻断/观察边界，真正下单需额外实现并完成仓位对账）。
3. **切勿**把真实私钥写入任何被 Git 跟踪的文件、截图或日志。
4. 建议先用极小资金 + 纸面并行观察至少 1–2 个完整当地日再考虑放大。

原仓库明确设计为「无钱包、无签名、无真实提交」。tree12-allnopart 保持该默认安全边界。
