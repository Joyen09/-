"""網格績效儀表（路 1）＋壓測 wiring（路 2）測試——離線可跑。

核心情境：資料庫的 realized_pnl 只記收割、不記 grid:close 倒貨，
儀表必須用現金流重建把隱藏的重開虧損挖出來。

執行：python tests/test_grid_report.py
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pionexbot.report import build_grid_report, format_grid_report  # noqa: E402

DAY = 86400.0


def _row(ts, side, base, quote, price, source, pnl=0.0, sim=0):
    return {"ts": ts, "side": side, "base": base, "quote": quote,
            "price": price, "source": source, "realized_pnl": pnl,
            "simulated": sim}


def _scenario():
    """週期1：買 100+90、收割賣 100（記+10）、跌破倒貨賣 70（沒記虧損）→ 真實 −20。
    週期2（進行中）：買 70、收割賣 75（記+5）、再買 68，存貨 10 顆。"""
    return [
        _row(1000, "BUY", 10, 100.0, 10.0, "grid"),
        _row(2000, "BUY", 10, 90.0, 9.0, "grid"),
        _row(3000, "SELL", 10, 100.0, 10.0, "grid", pnl=10.0),   # 收割
        _row(5000, "SELL", 10, 70.0, 7.0, "grid:close"),          # 倒貨（沒記虧）
        _row(6000, "BUY", 10, 70.0, 7.0, "grid"),
        _row(7000, "SELL", 10, 75.0, 7.5, "grid", pnl=5.0),       # 收割
        _row(8000, "BUY", 10, 68.0, 6.8, "grid"),
    ]


def test_true_pnl_exposes_hidden_reset_loss():
    d = build_grid_report(_scenario(), grid_capital=96.0,
                          current_price=7.0, now_ts=1000 + 10 * DAY)
    # 現金流：賣 245 − 買 328 + 存貨 10×7 = −13
    assert abs(d["true_pnl"] - (-13.0)) < 1e-9, d["true_pnl"]
    # 帳面 realized 只有收割的 +15 —— 差距就是被藏起來的重開虧損
    assert abs(d["recorded_realized"] - 15.0) < 1e-9
    assert d["true_pnl"] < d["recorded_realized"], "儀表必須挖出隱藏虧損"


def test_cycle_split_and_pnls():
    d = build_grid_report(_scenario(), grid_capital=96.0,
                          current_price=7.0, now_ts=1000 + 10 * DAY)
    assert d["cycles_total"] == 2 and d["cycles_closed"] == 1
    # 週期1（已關）：170 − 190 = −20；週期2（進行中）：75 − 138 + 70 = +7
    assert abs(d["closed_cycle_pnls"][0] - (-20.0)) < 1e-9
    assert abs(d["open_cycle_pnl"] - 7.0) < 1e-9
    assert abs(sum(d["closed_cycle_pnls"]) + d["open_cycle_pnl"]
               - d["true_pnl"]) < 1e-9, "週期損益總和必須等於真實總損益"


def test_harvest_stats_and_annualization():
    d = build_grid_report(_scenario(), grid_capital=96.0,
                          current_price=7.0, now_ts=1000 + 10 * DAY)
    assert d["harvests"] == 2
    assert abs(d["harvest_avg"] - 7.5) < 1e-9          # (10+5)/2
    assert abs(d["span_days"] - 10.0) < 1e-3
    # 年化 = −13/96/10×365 ≈ −49.4%（負得很誠實）
    assert d["annualized"] < 0
    assert abs(d["annualized"] - (-13.0 / 96 / 10 * 365)) < 1e-6
    # 同期買入持有：10 → 7 = −30%
    assert abs(d["buy_hold_return"] - (-0.30)) < 1e-9


def test_inventory_and_report_text():
    d = build_grid_report(_scenario(), grid_capital=96.0,
                          current_price=7.0, now_ts=1000 + 10 * DAY)
    assert abs(d["inventory_base"] - 10.0) < 1e-9
    txt = format_grid_report(d)
    assert "真實總損益 = -13.00" in txt
    assert "USDT" in txt and "年化" in txt


def test_empty_rows():
    d = build_grid_report([], grid_capital=96.0, current_price=7.0, now_ts=0)
    assert d["empty"] and "沒有網格" in format_grid_report(d)


def test_store_trades_by_source(tmp_path=None):
    import tempfile

    from pionexbot.store import Store
    with tempfile.TemporaryDirectory() as td:
        st = Store(os.path.join(td, "t.db"))
        st.record_trade(symbol="BTC_USDT", side="BUY", base=1, quote=10,
                        price=10, simulated=False, source="grid")
        st.record_trade(symbol="BTC_USDT", side="SELL", base=1, quote=11,
                        price=11, simulated=False, source="grid:close")
        st.record_trade(symbol="BTC_USDT", side="BUY", base=1, quote=10,
                        price=10, simulated=False, source="strategy:rsi")
        rows = st.trades_by_source("grid")
        assert len(rows) == 2, "前綴過濾：strategy 不得混入"
        assert rows[0]["side"] == "BUY" and rows[1]["source"] == "grid:close"
        st.close()


def test_close_grid_records_realized_pnl():
    """病根修正：grid:close 倒貨必須把「賣得 − 成本」記進 realized_pnl，
    「歷次累計已實現」才不會只算收割贏、藏掉重開虧。"""
    import tempfile

    from pionexbot.sources.grid_runner import GridRunner
    from pionexbot.store import Store

    class FakeRes:
        ok, simulated, error = True, False, ""
        filled_base, filled_quote, avg_price = 20.0, 140.0, 7.0

    class FakeBroker:
        def market_sell(self, symbol, qty):
            return FakeRes()

    class FakeNotifier:
        tg_enabled = discord_bot_enabled = False

        def send(self, *a, **kw):
            pass

    with tempfile.TemporaryDirectory() as td:
        store = Store(os.path.join(td, "t.db"))
        cfg = SimpleNamespace(symbol="BTC_USDT",
                              raw={"grid": {"grids": 10}}, notify={})
        gr = GridRunner(cfg, None, FakeBroker(), store, FakeNotifier())
        # 網格 [100, 110]×10 格（step 1）：持有格 0（買在 100）與格 5（買在 105）
        state = {"active": True, "lower": 100.0, "upper": 110.0, "grids": 10,
                 "held": {"0": 10.0, "5": 10.0}, "realized": 3.0}
        gr._close_grid(state, 7.0, "跌破下緣")
        row = store.trades_by_source("grid:close")[0]
        # 成本 = 10×100 + 10×105 = 2050；賣得 140 → realized −1910
        assert abs(row["realized_pnl"] - (140.0 - 2050.0)) < 1e-9, \
            f"倒貨損益應入帳，實際 {row['realized_pnl']}"
        assert not state["active"] and state["held"] == {}
        store.close()


def test_indicator_series_uses_closed_higher_tf_buckets():
    """儀器保真度：indicator_interval_ms 設 60M 時，ATR 要用聚合後的 60M K
    計算，且每根 15M 資料 K 只能看到「上一個已收盤」的 60M 桶（防前視）。"""
    from pionexbot.grid import DynamicGridBacktester, _ohlc

    def k(t, h, low, c):
        return {"time": t, "open": c, "high": h, "low": low, "close": c}

    m15 = 900_000
    # 兩個小時桶：第 1 小時 range=4（高10低6）、第 2 小時 range=8（高14低6）
    ks = [k(0 * m15, 10, 8, 9), k(1 * m15, 9, 7, 8),
          k(2 * m15, 8, 6, 7), k(3 * m15, 9, 7, 8),
          k(4 * m15, 14, 10, 12), k(5 * m15, 13, 9, 11),
          k(6 * m15, 12, 8, 10), k(7 * m15, 11, 6, 9)]
    bt = DynamicGridBacktester(atr_period=1, adx_period=1,
                               indicator_interval_ms=3_600_000)
    atr, adx = bt._indicator_series(ks, [_ohlc(x) for x in ks])
    # 第 1 小時內：還沒有已收盤的 60M 桶 → NaN
    assert all(a != a for a in atr[:4]), "第一個桶內不得有指標值（防前視）"
    # 第 2 小時內：用「第 1 小時」的 ATR = 10−6 = 4
    assert all(abs(a - 4.0) < 1e-9 for a in atr[4:]), atr[4:]
    # 不設 indicator_interval_ms → 逐根 15M 計算（舊行為，值不同）
    atr15, _ = DynamicGridBacktester(atr_period=1, adx_period=1) \
        ._indicator_series(ks, [_ohlc(x) for x in ks])
    assert abs(atr15[5] - (13 - 9)) < 1e-9, "資料時框 ATR 應為單根 range"


def test_grid_stress_wiring_offline():
    """grid-stress 端到端 wiring：假幣安客戶端 + fixture K 線，離線跑通。"""
    import json

    import main as m

    ks = json.load(open(os.path.join(os.path.dirname(__file__),
                                     "fixtures", "btc_15m_v11.json")))

    class FakeBinance:
        def __init__(self, *a, **kw):
            pass

        def get_klines_range(self, symbol, interval, s, e):
            return ks

    orig = m.__dict__.get("BinanceClient")
    import pionexbot.binance_client as bc
    orig_cls = bc.BinanceClient
    bc.BinanceClient = FakeBinance
    try:
        cfg = SimpleNamespace(
            symbol="BTC_USDT",
            raw={"grid": {"grids": 8, "quote_per_grid": 12,
                          "range_mode": "atr", "atr_mult": 6,
                          "regime_filter": True, "adx_max": 30},
                 "backtest": {}})
        bot = SimpleNamespace(cfg=cfg)
        args = SimpleNamespace(symbol=None, interval="15M",
                               start="2026-07-01", end="2026-07-14",
                               base_url=None, sweep=True)
        rc = m.cmd_grid_stress(bot, args)
        assert rc == 0
    finally:
        bc.BinanceClient = orig_cls
        if orig is not None:
            m.__dict__["BinanceClient"] = orig


# ---------------- 帳戶對照 + Discord/CLI 共用包裝 ----------------
class _FakeClient:
    """假交易所：回固定餘額與現價。"""

    def __init__(self, balances=None, price=7.0, fail=False):
        self._bal = balances or {"USDT": 30.0, "BTC": 1.0}
        self._price = price
        self._fail = fail

    def get_balances(self):
        if self._fail:
            raise RuntimeError("API 掛了")
        return dict(self._bal)

    def get_ticker_price(self, symbol):
        return self._price


def _cfg(live=True, initial=0.0):
    from pionexbot.config import Config
    return Config(mode="live" if live else "paper", raw={
        "trading": {"symbol": "BTC_USDT", "initial_capital": initial},
        "grid": {"grids": 10, "quote_per_grid": 5},
    })


def test_account_lines_compares_capital():
    from pionexbot.report import account_lines
    # 帳戶：30 USDT + 1 BTC×7 = 37；本金 40 → 賠 3
    out = "\n".join(account_lines(_cfg(initial=40.0), _FakeClient(), 7.0))
    assert "總值 37.00" in out, out
    assert "賠 -3.00" in out and "-7.50%" in out, out


def test_account_lines_prompts_when_no_capital_set():
    from pionexbot.report import account_lines
    out = "\n".join(account_lines(_cfg(initial=0.0), _FakeClient(), 7.0))
    assert "initial_capital" in out, "未填本金時要提示怎麼填"


def test_account_lines_skipped_for_paper_and_survives_api_failure():
    from pionexbot.report import account_lines
    assert account_lines(_cfg(live=False), _FakeClient(), 7.0) == [], \
        "紙上模式沒有交易所帳戶可查"
    out = "\n".join(account_lines(_cfg(initial=40.0), _FakeClient(fail=True), 7.0))
    assert "查詢失敗" in out, "查餘額失敗要降級成一行提示，不能讓整份報表消失"


def test_grid_report_text_combines_report_and_account():
    import tempfile

    from pionexbot.report import grid_report_text
    from pionexbot.store import Store
    with tempfile.TemporaryDirectory() as d:
        s = Store(os.path.join(d, "t.db"))
        for r in _scenario():
            s.record_trade(symbol="BTC_USDT", side=r["side"], base=r["base"],
                           quote=r["quote"], price=r["price"], simulated=False,
                           source=r["source"], realized_pnl=r["realized_pnl"])
        txt = grid_report_text(_cfg(initial=40.0), s, _FakeClient(), 9000.0)
    assert "實盤網格績效儀表" in txt and "真實總損益" in txt
    assert "交易所帳戶（最終真相）" in txt, "應接上帳戶對照"


def test_grid_report_text_without_trades():
    import tempfile

    from pionexbot.report import grid_report_text
    from pionexbot.store import Store
    with tempfile.TemporaryDirectory() as d:
        txt = grid_report_text(_cfg(), Store(os.path.join(d, "t.db")),
                               _FakeClient(), 9000.0)
    assert "沒有網格交易紀錄" in txt


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
    print("\n全部通過" if not failed else f"\n{failed} 個測試失敗")
    sys.exit(1 if failed else 0)
