#!/usr/bin/env python3
"""派網訊號機器人 CLI。

用法：
    python main.py test            # 測試連線與 API 簽章（建議第一步）
    python main.py balance         # 查詢帳戶餘額（需 API 金鑰）
    python main.py price           # 查目前市價
    python main.py run-strategy    # 啟動內建策略機器人（輪詢 K 線）
    python main.py run-grid        # 啟動自動網格機器人（風險過高自動關閉重開）
    python main.py run-webhook     # 啟動 Webhook 伺服器（接 TradingView 等）
    python main.py status          # 顯示目前持倆與最近交易
    python main.py buy  --quote 20 # 手動下一筆市價買單
    python main.py sell --base 0.001  # 手動市價賣出
    python main.py backtest        # 用歷史 K 線回測策略
    python main.py backtest-sweep  # 掃描停損/停利組合，推薦最佳參數
    python main.py optimize        # walk-forward 最佳化（嚴謹驗證是否真有優勢）
    python main.py grid-backtest   # 網格交易回測（--lower --upper --grids）

回測參數：
    --strategy ma_cross   指定策略（預設讀 config）
    --interval 5M         K 線週期
    --limit 1000          回測用的 K 線根數
    --cash 1000           起始資金

共用參數：
    --config config.yaml   指定設定檔
"""
from __future__ import annotations

import argparse
import sys

from pionexbot.bot import Bot
from pionexbot.config import load_config
from pionexbot.models import Action, Signal
from pionexbot.pionex_client import PionexError


def cmd_test(bot: Bot) -> int:
    cfg = bot.cfg
    print(f"模式：{cfg.mode}　交易對：{cfg.symbol}　Base URL：{cfg.base_url}")
    problems = cfg.validate()
    if problems:
        print("⚠ 設定問題：")
        for p in problems:
            print("  -", p)

    # 1) 公開行情（不需金鑰）
    print("\n[1] 測試公開行情 ...")
    try:
        price = bot.client.get_ticker_price(cfg.symbol)
        print(f"  ✅ {cfg.symbol} 市價 = {price}")
    except PionexError as exc:
        print(f"  ❌ 行情失敗：{exc}")
        return 1

    # 2) 私有端點（驗證簽章）
    print("\n[2] 測試私有端點 / API 簽章 ...")
    if not cfg.secrets.has_api_keys:
        print("  ⏭ 未設定 API 金鑰，略過。（紙上交易仍可運作）")
        return 0
    try:
        balances = bot.client.get_balances()
        nonzero = {k: v for k, v in balances.items() if v > 0}
        print(f"  ✅ 簽章正確，餘額：{nonzero or '（皆為 0）'}")
    except PionexError as exc:
        print(f"  ❌ 私有端點失敗（簽章或權限問題）：{exc}")
        return 1
    print("\n全部通過 ✅")
    return 0


def cmd_balance(bot: Bot) -> int:
    try:
        balances = bot.client.get_balances()
    except PionexError as exc:
        print(f"❌ {exc}")
        return 1
    for coin, free in sorted(balances.items()):
        if free > 0:
            print(f"  {coin:>8}: {free}")
    return 0


def cmd_price(bot: Bot) -> int:
    try:
        print(f"{bot.cfg.symbol} = {bot.client.get_ticker_price(bot.cfg.symbol)}")
    except PionexError as exc:
        print(f"❌ {exc}")
        return 1
    return 0


def cmd_status(bot: Bot) -> int:
    pos = bot.store.load_position()
    print(f"持倆：{pos.base} @ 均價 {pos.avg_cost:.2f}")
    print(f"當日已實現損益（{pos.day or 'N/A'}）：{pos.realized_pnl_today:.2f}")
    print("\n最近交易：")
    rows = bot.store.recent_trades(10)
    if not rows:
        print("  （無）")
    for r in rows:
        tag = "模擬" if r["simulated"] else "實盤"
        print(f"  [{tag}] {r['side']:>4} {r['base']:.8f} @ {r['price']:.2f} "
              f"= {r['quote']:.2f}　pnl={r['realized_pnl']:.2f}　{r['source']}")
    return 0


def cmd_run_strategy(bot: Bot) -> int:
    from pionexbot.sources.strategy_runner import StrategyRunner
    runner = StrategyRunner(bot.cfg, bot.client, bot.executor, bot.notifier)
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        bot.notifier.send("🛑 策略機器人已手動停止。", "info", important=True)
    except Exception as exc:  # noqa: BLE001 - 意外崩潰也要通知
        bot.notifier.send(f"💥 策略機器人異常結束：{exc}", "error")
        raise
    return 0


def cmd_run_grid(bot: Bot) -> int:
    from pionexbot.sources.grid_runner import GridRunner
    runner = GridRunner(bot.cfg, bot.client, bot.broker, bot.store, bot.notifier)
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        bot.notifier.send("🛑 網格機器人已手動停止。", "info", important=True)
    except Exception as exc:  # noqa: BLE001
        bot.notifier.send(f"💥 網格機器人異常結束：{exc}", "error")
        raise
    return 0


def cmd_run_dca(bot: Bot) -> int:
    from pionexbot.sources.dca_runner import DcaRunner
    runner = DcaRunner(bot.cfg, bot.client, bot.broker, bot.store, bot.notifier)
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        bot.notifier.send("🛑 DCA 機器人已手動停止。", "info", important=True)
    except Exception as exc:  # noqa: BLE001
        bot.notifier.send(f"💥 DCA 機器人異常結束：{exc}", "error")
        raise
    return 0


def cmd_dca_backtest(bot: Bot, args) -> int:
    from pionexbot.dca import compare_dca

    cfg = bot.cfg
    symbol = (args.symbol or cfg.symbol).upper()
    interval = args.interval or "1D"
    limit = args.limit or 1000
    # every：每隔幾根 K 線買一次（1D 線時 every=7 ≈ 每週）
    every = args.grids or 7   # 借用 --grids 當 every，避免再加參數

    print(f"抓取 {symbol} {interval} 共 {limit} 根，比較 DCA（每 {every} 根買一次）...")
    try:
        klines = bot.client.get_klines_history(symbol, interval, total=limit)
    except PionexError as exc:
        print(f"❌ 抓 K 線失敗：{exc}")
        return 1
    if len(klines) < 50:
        print(f"❌ K 線太少（{len(klines)}）")
        return 1

    results = compare_dca(klines, base=10.0, every=every)
    print(f"\n{symbol} {interval}　{len(klines)} 根　每 {every} 根買 10 USDT")
    print("\n方式        投入    現值    報酬     均價    買入次數")
    print("─" * 56)
    for r in results:
        print(f"{r.label:<10}{r.invested:>6.0f}  {r.final_value:>6.0f}  "
              f"{r.total_return*100:+6.1f}%  {r.avg_cost:>8.0f}  {r.buys:>4}")
    print("\n判讀：『逆低加碼』的均價通常較低；在波動/下跌段報酬常優於陽春定額。")
    return 0


def _signal_status_text(bot: Bot, store) -> str:
    """訊號機器人（策略/webhook）目前狀態：持倉、出場計畫、累計損益。"""
    mode = "實盤" if bot.cfg.is_live else "紙上"
    pos = store.load_position()
    try:
        price = bot.broker.get_price(bot.cfg.symbol)
    except Exception:  # noqa: BLE001
        price = 0.0
    lines = [f"🧪 訊號機器人狀態（{mode}｜{bot.cfg.symbol}）",
             f"現價 {price:.2f}",
             f"持倉 {pos.base:.8f} @ 均價 {pos.avg_cost:.2f}"]
    if pos.base > 0 and price:
        lines.append(f"未實現 {(price - pos.avg_cost) * pos.base:+.2f}")
    plan = store.load_plan()
    if plan:
        lines.append(f"出場計畫：SL {float(plan['stop_loss']):.2f}"
                     f"（進場 {float(plan['entry']):.2f}）")
        for t in plan.get("tps", []):
            mark = "✅" if t["filled"] else "⏳"
            lines.append(f"  {mark} TP {t['price']} × {t['fraction']}")
    count, pnl = store.stats_since(0)
    lines.append(f"累計成交 {count} 筆，已實現 {pnl:+.2f}")
    return "\n".join(lines)


def _plan_monitor_loop(bot: Bot, poll_seconds: int = 20) -> None:
    """Webhook 模式背景監控：出場計畫（TP 觸價 / SL 收盤觸發）＋ Discord 查詢。

    SQLite 連線不能跨執行緒共用，本執行緒開自己的 Store / Executor
    （同一個 data/bot.db 檔案，SQLite 以檔案鎖處理並行）。"""
    import time as _time

    from pionexbot.executor import Executor
    from pionexbot.store import Store

    store = Store("data/bot.db")
    execu = Executor(bot.broker, bot.risk, store, bot.notifier,
                     risk_cfg=bot.cfg.risk)
    interval = bot.cfg.strategy.get("interval", "5M")
    last_candle = None
    discord_after = None
    while True:
        try:
            plan = store.load_plan()
            if plan:
                price = bot.broker.get_price(bot.cfg.symbol)
                candle_close = None
                kl = bot.client.get_klines(bot.cfg.symbol, interval, limit=3)
                if len(kl) >= 2:
                    k = kl[-2]  # 最後一根已收盤
                    t = k.get("time") if isinstance(k, dict) else k[0]
                    if t != last_candle:
                        last_candle = t
                        c = k.get("close") if isinstance(k, dict) else k[4]
                        candle_close = float(c)
                execu.check_plan(price, candle_close=candle_close)

            # Discord 打字查詢（唯讀，與 grid_runner 同一套過濾規則）
            if bot.notifier.discord_bot_enabled:
                msgs = bot.notifier.get_discord_messages(after=discord_after)
                if msgs:
                    newest = max(int(m["id"]) for m in msgs
                                 if str(m.get("id", "")).isdigit())
                    first_poll = discord_after is None
                    discord_after = str(newest)
                    if not first_poll:
                        for m in msgs:
                            if m.get("webhook_id") or (m.get("author") or {}).get("bot"):
                                continue
                            author = str((m.get("author") or {}).get("id", ""))
                            uid = bot.notifier.discord_user_id
                            if uid and author != str(uid):
                                continue
                            bot.notifier.send(_signal_status_text(bot, store),
                                              important=True)
                            break
        except Exception as exc:  # noqa: BLE001 - 監控錯誤不該讓伺服器倒
            bot.notifier.send(f"⚠️ 出場計畫監控錯誤：{exc}", "warning")
        _time.sleep(poll_seconds)


def cmd_run_webhook(bot: Bot) -> int:
    import threading

    import uvicorn
    from pionexbot.sources.webhook import build_app
    app = build_app(bot.cfg, bot.executor, bot.notifier)
    wh = bot.cfg.webhook
    mode = "實盤" if bot.cfg.is_live else "紙上"
    # 背景監控 SL/TP（帶止損的訊號進場後，出場由這條執行緒盯）
    threading.Thread(target=_plan_monitor_loop, args=(bot,), daemon=True).start()
    print(f"🤖 Webhook 伺服器啟動（{mode}模式）"
          f" http://{wh.get('host','0.0.0.0')}:{wh.get('port',8080)}{wh.get('path','/webhook')}")
    uvicorn.run(app, host=wh.get("host", "0.0.0.0"), port=int(wh.get("port", 8080)))
    return 0


def cmd_backtest(bot: Bot, args) -> int:
    from pionexbot.backtest import Backtester
    from pionexbot.strategy import build_strategy

    cfg = bot.cfg
    scfg = cfg.strategy
    name = args.strategy or scfg.get("name", "ma_cross")
    interval = args.interval or scfg.get("interval", "5M")
    limit = args.limit or 1000

    print(f"抓取 {cfg.symbol} {interval} 共 {limit} 根 K 線 ...")
    try:
        klines = bot.client.get_klines_history(cfg.symbol, interval, total=limit)
    except PionexError as exc:
        print(f"❌ 抓 K 線失敗：{exc}")
        return 1
    if len(klines) < 30:
        print(f"❌ K 線資料太少（{len(klines)} 根），無法回測")
        return 1

    strategy = build_strategy(name, scfg.get("params", {}))
    bt = Backtester(
        strategy,
        start_cash=args.cash or 1000.0,
        quote_per_trade=float(cfg.trading.get("quote_per_trade", 100)),
        max_position_base=float(cfg.risk.get("max_position_base", 0)) or None,
    )
    result = bt.run(klines, cfg.symbol)
    print(result.summary())
    return 0


def cmd_backtest_sweep(bot: Bot, args) -> int:
    from pionexbot.backtest import sweep_stop_params

    cfg = bot.cfg
    scfg = cfg.strategy
    name = args.strategy or scfg.get("name", "ma_cross")
    interval = args.interval or scfg.get("interval", "5M")
    limit = args.limit or 1000

    print(f"抓取 {cfg.symbol} {interval} 共 {limit} 根 K 線，掃描停損/停利組合 ...")
    try:
        klines = bot.client.get_klines_history(cfg.symbol, interval, total=limit)
    except PionexError as exc:
        print(f"❌ 抓 K 線失敗：{exc}")
        return 1
    if len(klines) < 50:
        print(f"❌ K 線太少（{len(klines)}），無法回測")
        return 1

    rows = sweep_stop_params(
        name, scfg.get("params", {}), klines, cfg.symbol,
        start_cash=args.cash or 1000.0,
        quote_per_trade=float(cfg.trading.get("quote_per_trade", 100)),
    )

    by_return = sorted(rows, key=lambda r: r.result.total_return, reverse=True)
    print(f"\n策略={name}  K線={len(klines)}  "
          f"買入持有報酬={by_return[0].result.buy_hold_return * 100:+.2f}%")
    print("\n停損   停利   報酬      回撤    交易  勝率")
    print("─" * 48)
    for r in by_return:
        sl = f"{r.stop_loss*100:.0f}%" if r.stop_loss else "—"
        tp = f"{r.take_profit*100:.0f}%" if r.take_profit else "—"
        res = r.result
        print(f"{sl:>4}  {tp:>4}  {res.total_return*100:+7.2f}%  "
              f"{res.max_drawdown*100:5.1f}%  {len(res.closed_trades):4}  "
              f"{res.win_rate*100:4.0f}%")

    best_return = by_return[0]
    best_risk = max(rows, key=lambda r: r.score)
    print("\n🏆 報酬最高：停損 {} / 停利 {} → 報酬 {:+.2f}%、回撤 {:.1f}%".format(
        f"{best_return.stop_loss*100:.0f}%" if best_return.stop_loss else "關閉",
        f"{best_return.take_profit*100:.0f}%" if best_return.take_profit else "關閉",
        best_return.result.total_return * 100, best_return.result.max_drawdown * 100))
    print("⚖️  風險調整最佳（報酬/回撤）：停損 {} / 停利 {} → 報酬 {:+.2f}%、回撤 {:.1f}%".format(
        f"{best_risk.stop_loss*100:.0f}%" if best_risk.stop_loss else "關閉",
        f"{best_risk.take_profit*100:.0f}%" if best_risk.take_profit else "關閉",
        best_risk.result.total_return * 100, best_risk.result.max_drawdown * 100))
    print("\n建議把 config.yaml 的 risk 改成（風險調整最佳那組）：")
    print(f"  stop_loss_pct: {best_risk.stop_loss}")
    print(f"  take_profit_pct: {best_risk.take_profit}")
    return 0


def cmd_optimize(bot: Bot, args) -> int:
    from pionexbot.backtest import walk_forward

    cfg = bot.cfg
    scfg = cfg.strategy
    name = args.strategy or scfg.get("name", "ma_cross")
    interval = args.interval or scfg.get("interval", "4H")
    limit = args.limit or 5000

    print(f"抓取 {cfg.symbol} {interval} 共 {limit} 根 K 線，對 {name} 做 walk-forward 最佳化 ...")
    try:
        klines = bot.client.get_klines_history(cfg.symbol, interval, total=limit)
    except PionexError as exc:
        print(f"❌ 抓 K 線失敗：{exc}")
        return 1
    if len(klines) < 300:
        print(f"❌ K 線太少（{len(klines)}），無法做 walk-forward")
        return 1

    cash = args.cash or 1000.0
    folds = walk_forward(name, klines, cfg.symbol, folds=4,
                         start_cash=cash, quote_per_trade=cash)
    if not folds:
        print("❌ 資料不足以切分，請加大 --limit")
        return 1

    print(f"\n策略={name}　K線={len(klines)}　（每段都『前段找參數→後段沒看過的資料驗收』）")
    print("\n段  最佳參數                              內測報酬   實測報酬   實測交易")
    print("─" * 78)
    compounded = 1.0
    test_returns = []
    for f in folds:
        compounded *= (1 + f.test_return)
        test_returns.append(f.test_return)
        ps = ",".join(f"{k}={v}" for k, v in f.params.items())
        print(f"{f.fold:>2}  {ps:<36}  {f.train_return*100:+7.2f}%  "
              f"{f.test_return*100:+7.2f}%  {f.test_trades:>6}")

    oos = (compounded - 1) * 100
    wins = sum(1 for r in test_returns if r > 0)
    print("─" * 78)
    print(f"\n📉 實測（out-of-sample）累積報酬：{oos:+.2f}%　"
          f"（{wins}/{len(test_returns)} 段為正）")
    print("⚠️  此為含手續費、但『不含滑價』的理想值；實盤每筆再扣約 0.1~0.3% 滑價。")

    if oos > 5 and wins >= len(test_returns) * 0.6:
        print("\n✅ 在沒看過的資料上多數為正且累積正報酬 → 較可能有真實優勢（仍需扣滑價評估）。")
    else:
        print("\n🔴 在沒看過的資料上未能穩定獲利 → 先前的高報酬多為過度擬合，不建議投真錢。")
    return 0


def cmd_grid_backtest(bot: Bot, args) -> int:
    from pionexbot.grid import GridBacktester, _ohlc

    cfg = bot.cfg
    symbol = (args.symbol or cfg.symbol).upper()
    interval = args.interval or "4H"
    limit = args.limit or 2000

    print(f"抓取 {symbol} {interval} 共 {limit} 根 K 線 ...")
    try:
        klines = bot.client.get_klines_history(symbol, interval, total=limit)
    except PionexError as exc:
        print(f"❌ 抓 K 線失敗：{exc}")
        return 1
    if len(klines) < 50:
        print(f"❌ K 線太少（{len(klines)}）")
        return 1

    first_close = _ohlc(klines[0])[2]
    lower = args.lower or first_close * 0.8
    upper = args.upper or first_close * 1.2
    grids = args.grids or 20
    quote = args.quote or 20.0

    print(f"網格區間 {lower:.2f} ~ {upper:.2f}，{grids} 格，每格 {quote} USDT")
    bt = GridBacktester(lower, upper, grids, quote_per_grid=quote)
    result = bt.run(klines, symbol)
    print(result.summary())
    if result.unrealized < -result.start_cash * 0.1:
        print("\n⚠️ 未實現虧損偏大：價格可能已跌破網格下緣（住套房），這是網格在跌勢的典型風險。")
    return 0


def cmd_grid_compare(bot: Bot, args) -> int:
    from pionexbot.grid import compare_grid_variants

    cfg = bot.cfg
    symbol = (args.symbol or cfg.symbol).upper()
    interval = args.interval or "4H"
    limit = args.limit or 3000
    grids = args.grids or 20
    cash = args.cash or 1000.0

    print(f"抓取 {symbol} {interval} 共 {limit} 根 K 線，比較三種網格 ...")
    try:
        klines = bot.client.get_klines_history(symbol, interval, total=limit)
    except PionexError as exc:
        print(f"❌ 抓 K 線失敗：{exc}")
        return 1
    if len(klines) < 200:
        print(f"❌ K 線太少（{len(klines)}）")
        return 1

    results = compare_grid_variants(klines, symbol, grids=grids, start_cash=cash)
    bh = results[0].buy_hold_return * 100
    print(f"\n資料 {len(klines)} 根　起始資金 {cash:.0f}　{grids} 格　"
          f"買入持有 {bh:+.2f}%")
    print("\n策略              報酬      最大回撤  完成來回  重開  暫停%")
    print("─" * 62)
    for r in results:
        paused_pct = r.paused_bars / r.bars * 100 if r.bars else 0
        print(f"{r.label:<16}{r.total_return*100:+7.2f}%  {r.max_drawdown*100:6.1f}%  "
              f"{r.completed:>7}  {r.resets:>4}  {paused_pct:4.0f}%")
    print("\n判讀：比較『動態+ADX過濾』 vs『陽春固定』的報酬與回撤；"
          "若前者報酬更高或回撤更小，代表 ATR/ADX 升級有效。")
    return 0


def cmd_grid_report(bot: Bot, args) -> int:
    """實盤網格績效儀表（路 1）：從 bot.db 重建真實現金流損益。

    重要：資料庫的 realized_pnl 只記收割賣出、不含 grid:close 倒貨損益，
    所以本報表用現金流重建（見 pionexbot/report.py 模組說明）。"""
    import time as _time

    from pionexbot.report import grid_report_text

    txt = grid_report_text(bot.cfg, bot.store, bot.client, _time.time())
    print(txt)
    return 1 if txt.startswith("trades 資料表裡沒有") else 0


# 網格壓測（路 2）用與 binance-backtest 相同的三段市況
def cmd_grid_stress(bot: Bot, args) -> int:
    """網格長歷史壓力測試（路 2）：幣安 2024/2025/2026 三段市況，
    測「你現在 config 的參數」是穩健還是剛好運氣好。

    只讀資料、不碰實盤。--sweep 額外掃 atr_mult × adx_max 的小矩陣，
    看參數排名跨市況穩不穩（目的是穩健性評估、不是找最優——判讀時
    嚴禁挑某段最好的參數回頭改實盤，那是曲線擬合）。"""
    from datetime import datetime, timezone

    from pionexbot.binance_client import BinanceClient, BinanceError
    from pionexbot.grid import DynamicGridBacktester, compare_grid_variants
    from pionexbot.smc.mtf import interval_ms

    cfg = bot.cfg
    g = cfg.raw.get("grid", {})
    symbol = (args.symbol or cfg.symbol).upper()
    interval = args.interval or "15M"
    grids = int(g.get("grids", 10))
    qpg = float(g.get("quote_per_grid", 5))
    capital = grids * qpg
    cur_params = dict(
        grids=grids, quote_per_grid=qpg, start_cash=capital,
        use_atr=str(g.get("range_mode", "fixed")).lower() == "atr",
        range_pct=float(g.get("range_pct", 0.15)),
        atr_period=int(g.get("atr_period", 14)),
        atr_mult=float(g.get("atr_mult", 6)),
        use_regime=bool(g.get("regime_filter", False)),
        adx_period=int(g.get("adx_period", 14)),
        adx_max=float(g.get("adx_max", 30)),
        use_reset=bool(g.get("reset_on_breakout", True)),
        breakout_buffer=float(g.get("breakout_buffer", 0.02)),
        # 與 live 對齊：ATR/ADX 用 indicator_interval（預設 60M）算，
        # 不是資料時框——否則 ATR 偏小、區間偏窄、重開被灌水
        indicator_interval_ms=interval_ms(
            str(g.get("indicator_interval", "60M"))),
    )
    base_url = args.base_url or cfg.raw.get("backtest", {}).get("binance_base_url")
    client = BinanceClient(base_url=base_url)

    def _to_ms(s: str) -> int:
        return int(datetime.strptime(s, "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp() * 1000)

    if args.start or args.end:
        end = args.end or _DEFAULT_SEGMENTS[-1][2]
        segments = [(f"{args.start}~{end}", args.start, end)]
    else:
        segments = _DEFAULT_SEGMENTS

    print(f"網格壓力測試 {symbol}（{interval}，資本 {capital:.0f} USDT = "
          f"{grids} 格 × {qpg}）——用你現行 config 的參數對三段市況。\n")
    for label, s, e in segments:
        print(f"── {label}（{s} → {e}）抓取中 ...")
        try:
            kl = client.get_klines_range(symbol, interval, _to_ms(s), _to_ms(e))
        except BinanceError as exc:
            print(f"   ❌ 抓 K 線失敗：{exc}\n")
            continue
        if len(kl) < 500:
            print(f"   ⚠ K 線太少（{len(kl)}），跳過\n")
            continue
        cur = DynamicGridBacktester(**cur_params).run(kl, symbol, label="現行設定")
        refs = compare_grid_variants(
            kl, symbol, grids=grids, start_cash=capital,
            indicator_interval_ms=cur_params["indicator_interval_ms"])
        bh = refs[0].buy_hold_return * 100
        print(f"   {len(kl)} 根　買入持有 {bh:+.2f}%")
        print("   策略              報酬      最大回撤  來回  重開  暫停%")
        for r in [cur] + refs:
            paused = r.paused_bars / r.bars * 100 if r.bars else 0
            print(f"   {r.label:<14}{r.total_return*100:+8.2f}%  "
                  f"{r.max_drawdown*100:6.1f}%  {r.completed:>4}  "
                  f"{r.resets:>4}  {paused:4.0f}%")
        if args.sweep:
            print("   [sweep] atr_mult × adx_max（報酬% / 回撤%）：")
            for am in (4.0, 6.0, 8.0):
                cells = []
                for ax in (20.0, 30.0, 999.0):
                    p = {**cur_params, "use_atr": True, "atr_mult": am,
                         "use_regime": ax < 999, "adx_max": ax}
                    r = DynamicGridBacktester(**p).run(kl, symbol)
                    cells.append(f"adx{'off' if ax >= 999 else int(ax)}:"
                                 f"{r.total_return*100:+6.2f}/{r.max_drawdown*100:4.1f}")
                print(f"     atr_mult {am:.0f}：" + "　".join(cells))
        print()
    print("判讀：① 現行設定要在三段市況都不輸「陽春固定」且回撤可控，才算穩健；"
          "\n② sweep 看的是排名跨市況穩不穩，不是挑某段的最優參數回頭改實盤"
          "（那是曲線擬合）；\n③ 網格贏 BTC 不稀奇（熊市），要看絕對報酬是否值得資金佔用。")
    return 0


def cmd_symbol_info(bot: Bot, args) -> int:
    symbol = (args.symbol or bot.cfg.symbol).upper()
    try:
        info = bot.client.get_symbol_info(symbol)
    except PionexError as exc:
        print(f"❌ {exc}")
        return 1
    if not info:
        print(f"找不到 {symbol}")
        return 1
    print(f"=== {symbol} 交易規格 ===")
    for k, v in info.items():
        print(f"  {k}: {v}")
    for key in ("minAmount", "minTradeSize", "minTradeDumping", "minValue"):
        if key in info:
            print(f"\n➡ 最低下單相關：{key} = {info[key]}")
    return 0


def cmd_run_ict(bot: Bot) -> int:
    from pionexbot.sources.ict_runner import IctRunner
    runner = IctRunner(bot.cfg, bot.client, bot.executor, bot.notifier)
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        bot.notifier.send("🛑 ict2022 機器人已手動停止。", "info", important=True)
    except Exception as exc:  # noqa: BLE001
        bot.notifier.send(f"💥 ict2022 機器人異常結束：{exc}", "error")
        raise
    return 0


def cmd_ict_backtest(bot: Bot, args) -> int:
    from pionexbot.backtest_ict import backtest_ict2022
    from pionexbot.strategy.ict2022 import DEFAULTS

    cfg = bot.cfg
    p = {**DEFAULTS, **cfg.strategy.get("ict2022", {})}
    symbol = (args.symbol or cfg.symbol).upper()
    limit = args.limit or 5000          # entry 時框根數
    # 三個時框都抓足以覆蓋同一段時間的量
    e_int, t_int, h_int = p["entry_interval"], p["trigger_interval"], p["htf_interval"]
    from pionexbot.smc.mtf import interval_ms
    span = limit * interval_ms(e_int)
    t_total = span // interval_ms(t_int) + 400
    h_total = span // interval_ms(h_int) + 250

    print(f"抓取 {symbol}：entry {e_int}×{limit}、trigger {t_int}×{t_total}、"
          f"HTF {h_int}×{h_total} ...")
    try:
        entry_k = bot.client.get_klines_history(symbol, e_int, total=limit)
        trig_k = bot.client.get_klines_history(symbol, t_int, total=int(t_total))
        htf_k = bot.client.get_klines_history(symbol, h_int, total=int(h_total))
    except PionexError as exc:
        print(f"❌ 抓 K 線失敗：{exc}")
        return 1
    if len(entry_k) < 500:
        print(f"❌ entry K 線太少（{len(entry_k)}）")
        return 1

    smc_cfg = cfg.raw.get("smc", {})
    rep = backtest_ict2022(
        entry_k, trig_k, htf_k, ict_cfg=p, smc_cfg=smc_cfg,
        sessions_cfg=smc_cfg.get("sessions"),
        slippage_pct=float(cfg.raw.get("backtest", {}).get("slippage_pct", 0.0)),
        breakeven_after_tp=int(cfg.risk.get("breakeven_after_tp", 1)),
        breakeven_offset_pct=float(cfg.risk.get("breakeven_offset_pct", 0.001)),
    )
    print(f"\n=== ict2022 回測（{symbol}，entry {e_int}×{len(entry_k)}）===")
    print(rep.summary())
    print("\n判讀：期望值 E 為正且獲利因子 > 1 才值得進 paper；"
          "『不含滑價的理想值』請再保守打折。tags 分組看哪些條件真的加分。")
    return 0


def _binance_fetch_and_backtest(client, symbol, p, smc_cfg, cfg,
                                start_ms, end_ms):
    """抓幣安三時框 [start, end]（trigger/HTF 多抓暖身段）跑一段回測。"""
    from pionexbot.backtest_ict import (HTF_WINDOW, TRIGGER_WINDOW,
                                        backtest_ict2022)
    from pionexbot.smc.mtf import interval_ms

    e_int, t_int, h_int = p["entry_interval"], p["trigger_interval"], p["htf_interval"]
    # 暖身：trigger/HTF 要在 entry 段開始前就有足夠歷史建立結構
    lead = max(HTF_WINDOW * interval_ms(h_int),
               TRIGGER_WINDOW * interval_ms(t_int))
    entry_k = client.get_klines_range(symbol, e_int, start_ms, end_ms)
    trig_k = client.get_klines_range(symbol, t_int, start_ms - lead, end_ms)
    htf_k = client.get_klines_range(symbol, h_int, start_ms - lead, end_ms)
    if len(entry_k) < 500:
        return None, entry_k
    rep = backtest_ict2022(
        entry_k, trig_k, htf_k, ict_cfg=p, smc_cfg=smc_cfg,
        sessions_cfg=smc_cfg.get("sessions"),
        slippage_pct=float(cfg.raw.get("backtest", {}).get("slippage_pct", 0.0)),
        breakeven_after_tp=int(cfg.risk.get("breakeven_after_tp", 1)),
        breakeven_offset_pct=float(cfg.risk.get("breakeven_offset_pct", 0.001)),
    )
    return rep, entry_k


# 預設三段（依 HANDOFF：2024 多頭 / 2025 盤整 / 2026 熊市）；--start/--end 可覆寫
_DEFAULT_SEGMENTS = [
    ("2024 多頭", "2024-01-01", "2025-01-01"),
    ("2025 盤整", "2025-01-01", "2026-01-01"),
    ("2026 熊市", "2026-01-01", "2026-07-14"),
]


def cmd_binance_backtest(bot: Bot, args) -> int:
    """幣安長歷史回測（分段檢定）：解決派網 35 天樣本不足。

    預設跑三段市場週期各自檢定；--start/--end 給單一自訂區間。
    判準與派網一致（≥20 筆且 E 明顯>0，零滑價視為上界），且此處
    嚴禁改動任何偵測層凍結參數——只換資料源。"""
    from datetime import datetime, timezone

    from pionexbot.binance_client import BinanceClient, BinanceError
    from pionexbot.strategy.ict2022 import DEFAULTS

    cfg = bot.cfg
    p = {**DEFAULTS, **cfg.strategy.get("ict2022", {})}
    smc_cfg = cfg.raw.get("smc", {})
    symbol = (args.symbol or cfg.symbol).upper()
    # base_url 優先序：--base > config backtest.binance_base_url > 預設（vision 端點）
    base_url = args.base_url or cfg.raw.get("backtest", {}).get("binance_base_url")
    client = BinanceClient(base_url=base_url)

    def _to_ms(date_str: str) -> int:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

    if args.start or args.end:
        end = args.end or _DEFAULT_SEGMENTS[-1][2]
        segments = [(f"{args.start}~{end}", args.start, end)]
    else:
        segments = _DEFAULT_SEGMENTS

    print(f"幣安長歷史回測 {symbol}（entry {p['entry_interval']}）——"
          "只換資料源，偵測層參數凍結不動。\n")
    all_trades = []
    for label, s, e in segments:
        print(f"── {label}（{s} → {e}）抓取中 ...")
        try:
            rep, entry_k = _binance_fetch_and_backtest(
                client, symbol, p, smc_cfg, cfg, _to_ms(s), _to_ms(e))
        except BinanceError as exc:
            print(f"   ❌ 抓 K 線失敗：{exc}\n")
            continue
        if rep is None:
            print(f"   ⚠ entry K 線太少（{len(entry_k)}），跳過\n")
            continue
        closed = [t for t in rep.trades if t.closed]
        all_trades.extend(closed)
        print(f"   entry {p['entry_interval']}×{len(entry_k)}")
        print("   " + rep.summary().replace("\n", "\n   ") + "\n")

    # 跨段合計（判準看的是總樣本）
    if all_trades:
        rs = [t.r for t in all_trades]
        wins = [r for r in rs if r > 0]
        wr = len(wins) / len(rs)
        avg_w = sum(wins) / len(wins) if wins else 0.0
        losses = [r for r in rs if r <= 0]
        avg_l = abs(sum(losses) / len(losses)) if losses else 0.0
        e_val = wr * avg_w - (1 - wr) * avg_l
        print(f"═══ 跨段合計：{len(rs)} 筆　勝率 {wr*100:.0f}%　"
              f"E = {e_val:+.3f}R/筆　累積 {sum(rs):+.2f}R")
        verdict = ("✅ 達判準（≥20 筆且 E>0）→ 可考慮 paper forward"
                   if len(rs) >= 20 and e_val > 0
                   else ("❌ ≥20 筆但 E 未明顯>0 → 乾淨否定、收案"
                         if len(rs) >= 20
                         else "⚠ 仍 <20 筆 → 樣本不足，延長歷史或 paper 前瞻"))
        print(f"    {verdict}")
    else:
        print("═══ 跨段合計：0 筆成交。")
    print("\n判讀：零滑價為上界，請保守打折；此指令不得調整偵測層凍結參數。")
    return 0


def cmd_smc_plot(bot: Bot, args) -> int:
    """SMC 視覺化驗收（規格 §5.1）：K 線 + swing/BOS/MSS/區域/sweep/killzone。"""
    try:
        from pionexbot.smc.plot import build_figure
        import plotly  # noqa: F401
    except ImportError:
        print("❌ 需要 plotly：pip install plotly")
        return 1

    symbol = (args.symbol or bot.cfg.symbol).upper()
    interval = args.interval or "15M"
    limit = args.limit or 1000
    out = args.out or "smc.html"

    print(f"抓取 {symbol} {interval} 共 {limit} 根 K 線 ...")
    try:
        klines = bot.client.get_klines_history(symbol, interval, total=limit)
    except PionexError as exc:
        print(f"❌ 抓 K 線失敗：{exc}")
        return 1
    if len(klines) < 50:
        print(f"❌ K 線太少（{len(klines)}）")
        return 1

    fig = build_figure(klines, bot.cfg.raw.get("smc", {}),
                       plot_cfg=bot.cfg.raw.get("plot", {}))
    fig.write_html(out)
    print(f"✅ 已輸出 {out}（{len(klines)} 根）。用瀏覽器打開，"
          "對照講義範例圖人工驗收偵測結果。")
    return 0


def cmd_smc_stats(bot: Bot, args) -> int:
    """SMC 族群密度統計（v1.1 §4）：偵測層健康檢查。

    --out 給檔名（.json）時，順便把這段 K 線存成離線 fixture，
    可放進 tests/fixtures/ 讓 test_smc_density.py 離線驗收。
    --data 給快照檔時改用它、不抓 API——調參時在同一段資料上重算，
    密度變化才是參數造成的，不是行情換段造成的。"""
    import json

    from pionexbot.smc.stats import compute_stats, format_stats

    symbol = (args.symbol or bot.cfg.symbol).upper()
    interval = args.interval or "15M"
    limit = args.limit or 1000

    if args.data:
        with open(args.data, encoding="utf-8") as f:
            klines = json.load(f)
        print(f"使用快照 {args.data}（{len(klines)} 根，離線重算）")
    else:
        print(f"抓取 {symbol} {interval} 共 {limit} 根 K 線 ...")
        try:
            klines = bot.client.get_klines_history(symbol, interval,
                                                   total=limit)
        except PionexError as exc:
            print(f"❌ 抓 K 線失敗：{exc}")
            return 1
    if len(klines) < 100:
        print(f"❌ K 線太少（{len(klines)}）")
        return 1

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(klines, f)
        print(f"已存 K 線快照 → {args.out}"
              "（放進 tests/fixtures/ 供離線密度測試）")

    stats = compute_stats(klines, bot.cfg.raw.get("smc", {}))
    print(f"\n=== SMC 密度統計（{symbol} {interval}×{len(klines)}）===")
    print(format_stats(stats))
    print("\n判讀：對照 tests/test_smc_density.py 的校準區間；"
          "門檻值只能因密度/視覺證據而改，不得因回測績效而改（§6 紀律）。")
    return 0


def cmd_notify_test(bot: Bot) -> int:
    n = bot.notifier
    print(f"Discord 推播：{n.discord_enabled}　Discord 雙向：{n.discord_bot_enabled}　"
          f"Telegram 啟用：{n.tg_enabled}　LINE 啟用：{n.line_enabled}")
    if not (n.discord_enabled or n.discord_bot_enabled or n.tg_enabled or n.line_enabled):
        print("⚠ 三種通知都未啟用。請在 config.yaml 設 enabled: true，並在 .env 填好金鑰。")
        return 1
    n.send("🔔 派網訊號機器人通知測試：如果你收到這則訊息，代表通知設定成功！",
           "info", important=True)
    print("已送出測試通知，請查看你的 Discord / LINE / Telegram。")
    return 0


def cmd_manual(bot: Bot, action: Action, quote: float | None, base: float | None) -> int:
    sig = Signal(action=action, symbol=bot.cfg.symbol, source="manual:cli",
                 quote_amount=quote, base_size=base, reason="手動下單")
    result = bot.executor.handle(sig)
    print(result or "未成交（被風控擋下或 HOLD）")
    if result and result.ok and not result.simulated and result.filled_base == 0:
        print("⚠ 成交量解析為 0，原始訂單資料（請貼給我對照欄位）：")
        print(result.raw)
    return 0 if (result and result.ok) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="派網訊號機器人")
    parser.add_argument("command",
                        choices=["test", "balance", "price", "status",
                                 "run-strategy", "run-grid", "run-dca", "run-webhook",
                                 "buy", "sell",
                                 "backtest", "backtest-sweep", "optimize",
                                 "grid-backtest", "grid-compare",
                                 "grid-report", "grid-stress",
                                 "dca-backtest", "symbol-info", "notify-test",
                                 "smc-plot", "smc-stats", "run-ict",
                                 "ict-backtest", "binance-backtest"])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--quote", type=float, help="買入金額（報價幣）")
    parser.add_argument("--base", type=float, help="賣出數量（基礎幣）")
    parser.add_argument("--strategy", help="回測指定策略")
    parser.add_argument("--interval", help="K 線週期")
    parser.add_argument("--limit", type=int, help="回測 K 線根數")
    parser.add_argument("--cash", type=float, help="回測起始資金")
    parser.add_argument("--lower", type=float, help="網格下緣價格")
    parser.add_argument("--upper", type=float, help="網格上緣價格")
    parser.add_argument("--grids", type=int, help="網格數量")
    parser.add_argument("--symbol", help="覆寫交易對（如 ETH_USDT），用於回測掃描")
    parser.add_argument("--out", help="smc-plot 輸出的 HTML 檔名（預設 smc.html）")
    parser.add_argument("--data", help="smc-stats 改用 K 線快照檔（離線重算）")
    parser.add_argument("--start", help="binance-backtest 自訂區間起日 YYYY-MM-DD")
    parser.add_argument("--end", help="binance-backtest 自訂區間迄日 YYYY-MM-DD")
    parser.add_argument("--base-url", dest="base_url",
                        help="binance-backtest 覆寫端點（地區限制時用）")
    parser.add_argument("--sweep", action="store_true",
                        help="grid-stress 額外掃 atr_mult×adx_max 穩健性矩陣")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    bot = Bot(cfg)
    try:
        if args.command == "test":
            return cmd_test(bot)
        if args.command == "balance":
            return cmd_balance(bot)
        if args.command == "price":
            return cmd_price(bot)
        if args.command == "status":
            return cmd_status(bot)
        if args.command == "run-strategy":
            return cmd_run_strategy(bot)
        if args.command == "run-grid":
            return cmd_run_grid(bot)
        if args.command == "run-dca":
            return cmd_run_dca(bot)
        if args.command == "run-webhook":
            return cmd_run_webhook(bot)
        if args.command == "backtest":
            return cmd_backtest(bot, args)
        if args.command == "backtest-sweep":
            return cmd_backtest_sweep(bot, args)
        if args.command == "optimize":
            return cmd_optimize(bot, args)
        if args.command == "grid-backtest":
            return cmd_grid_backtest(bot, args)
        if args.command == "grid-compare":
            return cmd_grid_compare(bot, args)
        if args.command == "grid-report":
            return cmd_grid_report(bot, args)
        if args.command == "grid-stress":
            return cmd_grid_stress(bot, args)
        if args.command == "dca-backtest":
            return cmd_dca_backtest(bot, args)
        if args.command == "symbol-info":
            return cmd_symbol_info(bot, args)
        if args.command == "notify-test":
            return cmd_notify_test(bot)
        if args.command == "smc-plot":
            return cmd_smc_plot(bot, args)
        if args.command == "smc-stats":
            return cmd_smc_stats(bot, args)
        if args.command == "run-ict":
            return cmd_run_ict(bot)
        if args.command == "ict-backtest":
            return cmd_ict_backtest(bot, args)
        if args.command == "binance-backtest":
            return cmd_binance_backtest(bot, args)
        if args.command == "buy":
            return cmd_manual(bot, Action.BUY, args.quote or
                              float(cfg.trading.get("quote_per_trade", 20)), None)
        if args.command == "sell":
            return cmd_manual(bot, Action.SELL, None, args.base)
    finally:
        bot.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
