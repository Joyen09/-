# 交接文件（HANDOFF）— 給下一個 Claude Code session

> 讀我就夠了。這份文件是上一個 session（2026-07-12 結束）留給你的完整狀態轉移。
> 使用者：初學者，用繁體中文，偏好「一步一步可複製貼上的 VM 指令」。
> 使用者的 VM：GCP 免費 e2-micro，帳號 `linjoyen`，用 GCP 網頁 SSH 操作。

---

## 1. 現在什麼在跑（真錢！小心！）

| 部署 | 位置 | 分支 | 模式 | 內容 |
|---|---|---|---|---|
| **實盤網格** | VM `~/bot` | `main` | **live（真錢 ~108 USDT）** | `docker compose up -d grid`，BTC_USDT 8 格網格，累計已實現 +1.4↑ |
| 紙上測試 | VM `~/bot-paper` | `claude/pionex-signal-bot-5ysba7` | paper | `docker-compose.paper.yml`（獨立容器名/8081 埠/獨立 data） |

**鐵則（violating these hurts real money）：**
- `~/bot` 的 `data/bot.db` **絕對不能刪**——存著實盤網格的持倉追蹤
- **不要**叫使用者在派網 App 手動賣網格買的 BTC——會打亂機器人記帳
- `.env` / `config.yaml` 被 gitignore，**永遠不進版控**；API key 只開「讀取+現貨交易」，無提現權限
- 使用者的 **Telegram 曾被盜**，已停用（`.env` 清空 + config enabled: false），通知已全面改 **Discord**——不要建議重開 Telegram

## 2. Discord 通知架構

- 實盤網格：`~/bot/.env` 的 `DISCORD_WEBHOOK_URL`（推播）＋ `DISCORD_BOT_TOKEN`/`DISCORD_CHANNEL_ID`/`DISCORD_USER_ID`（雙向：頻道打字→回網格狀態，唯讀）→ 頻道 `#pionex`
- 紙上：同一隻 bot、不同頻道 `#paper`（webhook URL 留空、走 bot 發訊 fallback）
- 設計：`notifier.py` 的 send() 有 Webhook 優先、無則 Bot 的 fallback；LINE 支援存在但未啟用

## 3. Git 狀態

- **repo**：`Joyen09/pionex-signal-bot`（GitHub 上已改名；本地 remote 可能仍叫 `-`）
- **main**：實盤網格跑的版本（Phase 0：網格/DCA/四策略/Discord）
- **分支 `claude/pionex-signal-bot-5ysba7`**：SMC 規格書 Phase 1+2+3 全部完成，**PR #1 開著待 merge**
  - merge 不影響既有行為（向下相容測試全綠，40+ 項測試）
  - merge 後實盤更新：`cd ~/bot && git pull && docker compose up -d --build grid`（網格行為不變）
- 沙盒 `git push` 偶爾 HTTP 503——重試幾次或改用 GitHub API（mcp github push_files）

## 4. SMC 專案的完整結論（重要：別重做已做過的事）

規格書：使用者上傳的 `smcspec.md`（Phase 1 風控 / Phase 2 偵測 / Phase 3 ict2022 / Phase 4 工具）。

**已完成並驗收：**
- Phase 1：以止損定倉位、多段 TP+保本、SL 收盤觸發（同根以 SL 計）、每日上限、消息封鎖、webhook 擴充。使用者親手 curl 測過 paper 全流程 ✅
- Phase 2：`pionexbot/smc/`（structure/liquidity/zones/sessions/mtf/plot），使用者拿講義截圖逐張人工驗收過（BOS/MSS、OB/Breaker、FVG/IFVG/BPR/CE 定義全部核對一致）✅
- Phase 3：`strategy/ict2022.py`（八步流水線純函數）＋ `backtest_ict.py`（R 倍數統計＋漏斗診斷）＋ `sources/ict_runner.py`（run-ict）✅

**回測最終結果（2026-07，六幣各 ~35 天 5M，zone_edge/killzone off/min_rr 1.2，零滑價）：**
BTC +2.79R×1｜ETH +1.69R×2｜SOL −1.79R×7｜BNB 0 筆（HTF 空頭正確空手）｜DOGE −1.32R×1｜XRP −0.01R×4
→ **合計 15 筆、+1.36R、E≈+0.09R/筆 ≈ 零**。事先判準（≥20 筆且 E 明顯>0）**未通過**。
**結論已告知使用者：證據不支持投錢。ict2022 不上實盤。**

**假設驗證結果（2026-07-13，六幣同段資料、`require_ote: true` + `sweep_sources: ["EQ"]` 硬條件重回測）：**
BTC 0 筆（原 +2.79R 的贏單被篩掉）｜ETH 0 筆（原 +1.69R×2 被篩掉）｜SOL +4.30R×3｜BNB 0 筆｜DOGE −1.43R×1｜XRP −3.01R×2
→ **合計 6 筆、−0.14R、E ≈ −0.02R/筆**。之前的贏單根本不來自 OTE+EQ 型態，假設被推翻；SOL 3/3 全勝是小樣本假象（n=3）。
**最終結論（已告知使用者）：ict2022 兩輪誠實驗證期望值皆 ≈ 零，此線收案，不上實盤。除非使用者主動要求，不要再提議繼續優化 ict2022。**

**v1.1 偵測層修正（2026-07-14，使用者上傳 `smcdetectorv1.1spec.md` 後重啟）：**
事後對 smc-plot 全量統計發現 Phase 2 偵測層有「族群統計退化」（BTC 15M 10 天
生出 435 個區域、BPR 佔 73%、中位高度 0.07%、同時活躍 31 條 → 選區形同隨機），
前兩輪 ≈0 期望值測的是這個退化版。已照 v1.1 規格完成：
- §1 BPR 五規則（相鄰 20 根、位移穿越、皆存活、寬度門檻、去重）
- §2 結構事件 `displacement` 欄位（ATR 實體/推進段 FVG，`either`）——狀態機照舊，
  下游（OB 生成、ict2022 等 MSS、密度統計）只認強事件
- §3 區域生命週期（翻轉一次→再穿移除、BPR 不翻轉、TTL 200、同類上限 8）＋
  `zones.detect_all()` 單一真相來源（plot 與策略共用）
- §4 `smc-stats` CLI＋`tests/test_smc_density.py`（校準區間具名常數；fixture 缺席
  會略過，產生方式見該檔檔頭）＋ §5 候選數診斷（`tags.candidates`、回測報表）
**抽查修正（2026-07-14，使用者上傳 `smcv11fixkit.md`——外部對 76 個標註逐筆物理驗證）：**
① BPR 收盤穿越「任一」遠端 → 直接 FILLED 不翻轉（殭屍 BPR@904 案例）；
② 影線走完規則在翻轉後的第二生命（IFVG/Breaker）同樣適用，觸及第二生命也記
TESTED（IFVG@959 案例）；Zone 新增 `flips` 欄位；
③ NY killzone 疑點澄清：實測 8 根＝2 小時正確，圖上 12 根的帶是 London；
④ 套件測試 `test_smc_v11_fixes.py`/`test_smc_density.py`（fixture `btc_15m_v11.json`，
與 `btc_15m_1000.json` 實為同段、僅末根差異）依適配規則只改呼叫行收入，
斷言未動。注意：904 殭屍在本 repo 因規則 6 根本不出生，該回歸測試呼叫行
放行 `component_min_pct` 以驗證生命週期契約本身。
副作用（正確的）：ict2022 e2e 劇本的 BPR 在拉升段被收穿上緣即死，策略改選
FVG——e2e 測試已照新真相更新（entry 99.2、R≈2.47、候選數 2）。

**密度驗收（2026-07-14，BTC 15M×1000 真實快照 = `tests/fixtures/btc_15m_1000.json`）：
全過並已凍結參數。** 兩輪校準：① `displacement_mode: fvg`＋獨立門檻
`displacement_fvg_min_pct: 0.0015`（either 模式 19 個 MSS 全判強、0 弱 → 閘門虛設）；
② BPR 規則 6 `component_min_pct`（預設同位移門檻）：組成 BPR 的兩個 FVG 都要位移級
（gap/dedup 推不動細條族群，這才是對的旋鈕）。結果：強 MSS 19→4、MSS/BOS 0.66→0.44、
BPR 18→9（中位高度 0.096%→0.206%）。**參數已凍結：此後任何改動＝重跑密度驗收，
理由只能來自密度/視覺證據，不得來自回測績效。**
**凍結確認（2026-07-14）：使用者在 VM 以同一快照重算，密度表與沙盒完全一致
（BPR 9、強 MSS 4、MSS/BOS 0.44），抽查修正後驗收完成——參數正式凍結，
使用者已放行六幣回測。**
**回測成交模型修正（2026-07-14，使用者上傳 `backtestfillfix.md`）：**
偵測層**不動**（凍結維持）。修的是回測引擎的止損成交模型——原本 TP 掛限價
（零超越）、但 SL 用收盤觸發又用收盤價成交（吃整根 K 的超越），零滑價下卻跑出
−8.97R，讓 −1R 上限失效、R 記帳失真。改為**觸價止損**（low 觸及即以止損價成交、
對稱於 TP）。保守規則全保留（SL 先於 TP、進場根觸損 −1R、保本觸價成小正）。
- 出場邏輯抽成純函數 `apply_bar_exit`（`Position`），`tests/test_backtest_fill.py`
  7 項契約測試（觸價 −1R、深影線仍 −1R、同根 SL+TP 以 SL 計、進場根、保本回撤小正、
  全 TP）。fixture 驗證：同一筆止損 slip_R 0.10→0.00、−1.19R→−1.09R（僅剩手續費）。
- 報表新增：止損滑移統計（應趨近 0）＋成交模型假設抬頭。`IctTrade` 加
  `sl_ref/exit_px/trigger_ohlc/sl_slip_r`、`report.verify_sl_fills()` 可逐筆稽核。
- e2e 測試更新：觸價模型下劇本改以「保本被回踩觸價」小正出場（比舊收盤模型
  「撐過保本續抱到更高 TP」更貼實盤）——這是修正的正確副作用，非回歸。
- **§3 實盤前提（誠實附註）**：觸價回測只有實盤能對等執行才誠實。Pionex 現貨
  **原生 stop 單支援＝未驗證**（`place_order` 有 order_type 參數但實際只用 MARKET；
  需查 Pionex API 文件，列為 open item）。現行 `ict_runner` 以 30s 輪詢＋
  `check_plan` 市價出場（比規格的 1M 輪詢細，但仍是市價、快市/gap 會比回測差）。
  報表抬頭已標「SL=觸價成交於止損價（gap 高估，需實盤 stop 單支撐）」。
- **§6 誠實預告**：此修正讓數字第一次**可信**，不是救成正的。手算：10 敗壓回 ~−1R、
  2 勝不變 → E 從 −1.95R 修到約 −0.5R/筆。仍負，但這是**第一次可信的否定**，
  比靠虛構 R 撐出來的數字有價值。

**ict2022 正式收案（2026-07-15，使用者決定中止六幣全跑——VM 太慢且 SSH 斷線）：**
未達成形式上的 ≥20 筆，但既有證據全部同向：修好儀器後派網 3 筆＋幣安 BTC 2.5 年
13 筆（三 regime：多頭 −1.65R／盤整 −1.57R／熊市 −3.12R）全體為負；成交模型修正
後手算 E 仍約 −0.5R/筆；2024 大多頭只做多 0 勝 4 敗；訊號密度單幣年僅 4–6 筆。
**結論：證據不支持 ict2022 有可投錢的優勢，收案、不上實盤、不再調參。**
除非使用者主動重啟（屆時先用修正後引擎補滿六幣 ≥20 筆），不要再提議優化它。
所有工具（SMC 偵測層 v1.1、觸價回測引擎、幣安資料源）保留——它們是可信的儀器，
可服務其他策略。使用者的關注已轉向：**如何用同樣的量化嚴謹精進實盤網格**。

**網格精進：路 1 儀表＋路 2 壓測（2026-07-15，使用者選定「1→2 連著做」）：**
- **記帳缺口（重要發現）**：`grid_runner._close_grid` 的倒貨賣出原本**不記
  realized_pnl**——「歷次累計已實現」只算收割贏、藏掉重開虧，帳面系統性高估。
  已修：grid:close 現在記入「賣得 − 格線成本」。歷史舊紀錄無法補記，
  但 `grid-report` 用純現金流重建、不受影響。
- **`grid-report`**（路 1，在 `~/bot` 跑）：從 trades 表重建真實損益 =
  賣出總收入 − 買入總支出 ＋ 存貨市值（filled_quote 含手續費滑價，藏不住）。
  依 grid:close 切週期、逐週期損益、收割頻率、年化、對比買入持有與抱 USDT。
  離線時以最後成交價代替現價（會標註）。
- **`grid-stress`**（路 2，在 `~/bot-paper` 跑）：讀使用者現行 config 的網格參數，
  用幣安三段市況（2024/2025/2026）回放 `DynamicGridBacktester`（與 live 同邏輯），
  對照三個基準變體；`--sweep` 掃 atr_mult(4/6/8)×adx_max(20/30/off) 穩健性矩陣。
  **判讀紀律**：sweep 是看排名跨市況穩不穩，嚴禁挑某段最優參數回頭改實盤（曲線擬合）。
- `pionexbot/report.py`＋`tests/test_grid_report.py`（含 close 記帳修正的單測）。

**幣安長歷史整合（2026-07-14，使用者選「現在就平行建好」）：**
- `pionexbot/binance_client.py`：只做公開行情（免簽章、不下單），介面對齊
  `PionexClient`（get_klines/get_klines_history 回舊→新 dict、欄位相容 ohlc），
  另加 `get_klines_range` 向前分頁抓日期區間。週期 60M→1h、月才是大寫 1M；
  交易對 BTC_USDT→BTCUSDT。帶指數退避重試（4xx 不重試）。
- `main.py binance-backtest`：預設跑三段（2024 多頭/2025 盤整/2026 熊市）各自
  檢定＋跨段合計＋自動判準標示；`--start/--end` 給自訂單段；`--symbol` 覆寫。
  **只換資料源——嚴禁碰偵測層凍結參數**（回測引擎/SMC 一行未改）。
- `tests/test_binance_client.py`：正規化/格式相容/分頁不漏不重/重試，假 session
  離線全綠。沙盒連不到幣安（proxy 擋），**真實抓取要在 VM 驗**。
- **地區限制已解（2026-07-14）**：VM 首打 `api.binance.com` 回 HTTP 451（GCP IP
  被幣安地區封鎖）。已改預設端點為 `data-api.binance.vision`（幣安官方公開行情
  專用、免金鑰無地區限制、資料同主站），並自動輪替備援 host（vision→api-gcp→
  api.binance.com→api1），451/403 自動換下一個。可用 `--base` 或 config
  `backtest.binance_base_url` 覆寫。**注意不要混用 binance.us**（不同資料集會汙染回測）。

## 5. 這段開發抓到的坑（下次別再踩）

1. **派網 K 線單次回傳是新→舊**——`pionex_client.get_klines` 已統一 `_sort_asc` 排序，別移除
2. **派網歷史有保存上限**（5M 約 1 萬根 ≈35 天），翻頁過界回 `MARKET_INVALID_TIME`——`get_klines_history` 已優雅停手
3. 派網週期代號用 `60M` 不是 `1H`（client 有別名轉換）；市價買最低 `minAmount=10 USDT`；BTC `basePrecision=6`，賣出數量要無條件捨去
4. **plotly `add_shape` 是 O(n²)**——shapes/annotations 必須收集後一次 `update_layout`
5. **SQLite 連線不能跨執行緒**——背景執行緒要開自己的 Store
6. Docker：compose 的 entrypoint 已是 `python main.py`，執行子指令用 `docker compose run --rm <service> <子指令>`（不要再帶 python main.py）；`container_name` 寫死，平行部署用 `docker-compose.paper.yml`
7. 市價單回應只有 orderId，成交明細要輪詢 `get_order`；只認 `filledSize/filledAmount>0` 欄位
8. 幣安週期代號：`60M`→`1h`、`4H`→`4h`（小寫）；**月線才是大寫 `1M`**（別和「分鐘」的 5M 搞混）；單次上限 1000 根（派網是 500）

## 6. 常用指令速查（在對應目錄下）

```bash
# 測試（無網路需求）
python tests/test_signing.py && python tests/test_strategies.py \
  && python tests/test_phase1.py && python tests/test_smc.py && python tests/test_ict2022.py \
  && python tests/test_smc_stats.py && python tests/test_smc_density.py \
  && python tests/test_smc_v11_fixes.py && python tests/test_binance_client.py

# 實盤網格（~/bot，main）
docker compose logs -f grid
docker compose run --rm grid status          # 交易記錄
docker compose run --rm grid grid-report     # 績效儀表（真實現金流損益）
# Discord #pionex 打任意字 → 網格狀態；打「報表/損益/績效/賺/結算/pnl」→ 績效儀表（含帳戶對照）

# 紙上（~/bot-paper，分支）
docker compose -f docker-compose.paper.yml run --rm webhook ict-backtest --limit 20000 [--symbol ETH_USDT]
docker compose -f docker-compose.paper.yml run --rm webhook smc-plot --interval 15M --limit 1000 --out data/smc.html
docker compose -f docker-compose.paper.yml run --rm webhook run-ict   # ict2022 paper 執行
docker compose -f docker-compose.paper.yml run --rm webhook smc-stats --interval 15M --limit 1000   # 密度驗收
# 幣安長歷史（分段檢定，只換資料源、不碰凍結參數）
docker compose -f docker-compose.paper.yml run --rm webhook binance-backtest --symbol BTC_USDT
docker compose -f docker-compose.paper.yml run --rm webhook binance-backtest --symbol BTC_USDT --start 2025-01-01 --end 2025-04-01
# 網格長歷史壓測（三段市況；nohup 防 SSH 斷線殺工作）
nohup docker compose -f docker-compose.paper.yml run --rm webhook grid-stress --sweep > stress.log 2>&1 &
tail -f stress.log
```

## 7. 下一步的候選（使用者說「看要不要繼續實測」）

按優先序建議：
1. **merge PR #1**（安全，收工具進 main）
2. ~~ict2022 硬條件重回測~~ 已做完，假設被推翻——但 v1.1 發現當時偵測層退化，
   前兩輪測量作廢、待偵測層驗收後乾淨重測一次（見第 4 節 v1.1 段與其紀律）
3. **v1.1 進行中**：程式與測試已完成，下一步是 VM 跑 `smc-stats`/`smc-plot`
   密度驗收（順便 `--out tests/fixtures/btc_15m_1000.json` 產 fixture）→
   參數凍結 → 重跑六幣 `ict-backtest`
4. 使用者 VM 的 `~/bot-paper` config.yaml 可能還開著上一輪的兩個硬條件——重測前
   把 `require_ote`/`sweep_sources` 改回關閉
3. 使用者還想做的其他機器人：台股、每日便宜機票搜尋、OCR 圖片辨識（都想用同一隻 Discord bot 架構）
4. 網格照顧：跌破 60369 會自動平倉重開（設計行為）；BTC 現處 2026 熊市，使用者知道 4 年週期論

## 7c. 帳戶對照

`grid-report` 末尾會查交易所餘額算「帳戶總值」；在 config.yaml 的
`trading.initial_capital` 填入存進派網的本金，就會直接算出實際賺賠——
這是不靠任何記帳推算的最終真相。

## 8. 溝通風格提醒

- 使用者是初學者：指令給完整可貼、解釋用生活比喻、一次不要丟太多步驟
- **對「能不能賺錢」永遠誠實**——這是整個專案的核心約定。使用者已被教育過：n=1 不是證據、倖存者偏差、事先判準。請延續這個標準，不要為了討好而樂觀
- 使用者會貼截圖回報，擅長跟著漏斗/報表判讀——善用既有的診斷工具而不是猜
