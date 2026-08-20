"""實盤網格績效儀表（路 1）：從 trades 資料表重建真實損益。

為什麼不用資料庫裡的 realized_pnl 直接加總：
- 收割賣出（source='grid'）有記 realized_pnl，但**重開平倉**（source='grid:close'）
  沒有——跌破下緣倒貨的虧損不在「已實現」裡。只看 realized_pnl 會系統性高估。
- 所以本報表用**純現金流**重建：損益 = 賣出總收入 − 買入總支出 + 期末存貨市值。
  市價單的實際成交額（filled_quote）已含手續費與滑價，什麼都藏不住。

週期（cycle）切分：以 grid:close 為邊界。一個週期內存貨歸零，
週期損益 = 該週期賣出總額 − 買入總額（精確值，不需要成本配對）。
最後一個未關閉的週期，加上「目前存貨 × 現價」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GridCycle:
    start_ts: float
    end_ts: Optional[float] = None      # None = 進行中
    buys: int = 0
    sells: int = 0                      # 含收割與 grid:close
    harvests: int = 0                   # 收割賣出（source='grid' 的 SELL）
    buy_quote: float = 0.0
    sell_quote: float = 0.0
    inventory_base: float = 0.0         # 週期結束時應為 0（close 倒貨）
    closed_by: str = ""                 # grid:close 的那筆（重開原因不可考，僅記有無）

    def pnl(self, current_price: float = 0.0) -> float:
        return (self.sell_quote - self.buy_quote
                + self.inventory_base * current_price)


def build_grid_report(rows: list[dict], *, grid_capital: float,
                      current_price: float, now_ts: float) -> dict:
    """rows：trades 表中 source LIKE 'grid%' 的紀錄（依時間升冪）。

    回傳統計 dict；格式化交給 format_grid_report。"""
    rows = sorted(rows, key=lambda r: (r["ts"], r.get("id", 0)))
    if not rows:
        return {"empty": True}

    cycles: list[GridCycle] = [GridCycle(start_ts=rows[0]["ts"])]
    recorded_realized = 0.0
    simulated_rows = 0
    for r in rows:
        if r.get("simulated"):
            simulated_rows += 1
        cyc = cycles[-1]
        side = str(r["side"]).upper()
        src = str(r.get("source") or "")
        if side == "BUY":
            cyc.buys += 1
            cyc.buy_quote += float(r["quote"])
            cyc.inventory_base += float(r["base"])
        else:
            cyc.sells += 1
            cyc.sell_quote += float(r["quote"])
            cyc.inventory_base -= float(r["base"])
            if src == "grid":
                cyc.harvests += 1
        recorded_realized += float(r.get("realized_pnl") or 0.0)
        if src == "grid:close":
            cyc.end_ts = r["ts"]
            cyc.closed_by = "close"
            cycles.append(GridCycle(start_ts=r["ts"]))
    # 最後一個週期若沒有任何交易（close 後尚未開新格買入），拿掉
    if cycles[-1].buys == 0 and cycles[-1].sells == 0:
        cycles.pop()

    open_cycle = cycles[-1] if cycles and cycles[-1].end_ts is None else None
    inventory_base = open_cycle.inventory_base if open_cycle else 0.0
    inventory_value = inventory_base * current_price

    total_buy = sum(c.buy_quote for c in cycles)
    total_sell = sum(c.sell_quote for c in cycles)
    true_pnl = total_sell - total_buy + inventory_value
    closed_pnls = [c.pnl() for c in cycles if c.end_ts is not None]

    first_ts, last_ts = rows[0]["ts"], rows[-1]["ts"]
    span_days = max((now_ts - first_ts) / 86400.0, 1e-9)
    first_price = float(rows[0]["price"])
    bh_return = (current_price - first_price) / first_price if first_price else 0.0

    harvests = sum(c.harvests for c in cycles)
    harvest_pnls = [float(r.get("realized_pnl") or 0.0) for r in rows
                    if str(r.get("source")) == "grid"
                    and str(r["side"]).upper() == "SELL"]

    ann = (true_pnl / grid_capital / span_days * 365.0) if grid_capital else 0.0
    return {
        "empty": False,
        "simulated_rows": simulated_rows,
        "span_days": span_days,
        "first_ts": first_ts, "last_ts": last_ts,
        "trades": len(rows),
        "cycles_total": len(cycles),
        "cycles_closed": len(closed_pnls),
        "closed_cycle_pnls": closed_pnls,
        "open_cycle_pnl": open_cycle.pnl(current_price) if open_cycle else None,
        "harvests": harvests,
        "harvest_avg": (sum(harvest_pnls) / len(harvest_pnls))
                       if harvest_pnls else 0.0,
        "harvest_per_day": harvests / span_days,
        "total_buy_quote": total_buy,
        "total_sell_quote": total_sell,
        "inventory_base": inventory_base,
        "inventory_value": inventory_value,
        "true_pnl": true_pnl,
        "recorded_realized": recorded_realized,
        "grid_capital": grid_capital,
        "return_on_capital": true_pnl / grid_capital if grid_capital else 0.0,
        "annualized": ann,
        "current_price": current_price,
        "first_price": first_price,
        "buy_hold_return": bh_return,
    }


def format_grid_report(d: dict, price_note: str = "") -> str:
    if d.get("empty"):
        return "trades 資料表裡沒有網格交易紀錄。"
    lines = [
        "════════ 實盤網格績效儀表 ════════",
        f"期間：{d['span_days']:.1f} 天　成交 {d['trades']} 筆"
        f"（收割 {d['harvests']} 次 ≈ {d['harvest_per_day']:.2f} 次/天）",
        f"週期：共 {d['cycles_total']} 輪（已關閉 {d['cycles_closed']}、"
        f"進行中 {d['cycles_total'] - d['cycles_closed']}）",
        "",
        "── 真實現金流（含手續費滑價，藏不住重開虧損）──",
        f"買入總支出 {d['total_buy_quote']:.2f}　賣出總收入 {d['total_sell_quote']:.2f}",
        f"目前存貨 {d['inventory_base']:.8f} × 現價 {d['current_price']:.2f}"
        f"{price_note} = {d['inventory_value']:.2f}",
        f"➡ 真實總損益 = {d['true_pnl']:+.2f} USDT"
        f"（帳面 realized_pnl 合計 {d['recorded_realized']:+.2f}）",
    ]
    closed = d["closed_cycle_pnls"]
    if closed:
        worst = min(closed)
        lines.append(
            f"已關閉週期損益：{'、'.join(f'{p:+.2f}' for p in closed)}"
            f"（最差 {worst:+.2f}——重開倒貨的成本就在這裡）")
    if d["open_cycle_pnl"] is not None:
        lines.append(f"進行中週期損益（含存貨市值）：{d['open_cycle_pnl']:+.2f}")
    lines += [
        "",
        "── 相對表現 ──",
        f"配置資本 {d['grid_capital']:.2f} USDT　"
        f"報酬率 {d['return_on_capital']*100:+.2f}%　"
        f"年化 ≈ {d['annualized']*100:+.1f}%（線性外插，僅供量級參考）",
        f"同期買入持有 BTC：{d['buy_hold_return']*100:+.2f}%　"
        f"抱著 USDT 不動：+0.00%",
        "",
        "判讀：網格的對手不是 BTC（熊市誰都贏它），是「抱 USDT + 零風險」。"
        "年化要明顯高於 0 且已關閉週期沒有毀滅性虧損，才算真的在賺波動財。",
    ]
    if d.get("simulated_rows"):
        lines.append(f"⚠ 內含 {d['simulated_rows']} 筆紙上模擬紀錄（混在同一個 db）")
    return "\n".join(lines)


def account_lines(cfg, client, price: float,
                  bot_inventory: float = 0.0) -> list[str]:
    """交易所帳戶實況：總值 vs 你存進去的本金。

    只有實盤有意義；紙上模式與查詢失敗都回空清單（呼叫端不必判斷）。

    重要：帳戶裡可能有「不是機器人買的」幣（手動買、空投、其他機器人）。
    此時「帳戶總值 − 本金」**不等於機器人的績效**——會把手動持倉的漲跌
    算到機器人頭上。所以這裡比對機器人記的存貨（bot_inventory）與帳戶
    實際持有量，差距顯著就明講，不讓那個數字被誤讀成機器人賺的。"""
    if cfg.is_live is False or client is None:
        return []
    quote_coin = cfg.symbol.split("_")[-1]
    base_coin = cfg.symbol.split("_")[0]
    try:
        bal = client.get_balances()
    except Exception as exc:  # noqa: BLE001 - 查餘額失敗不該讓整份報表消失
        return ["", f"（交易所餘額查詢失敗：{exc}）"]
    free_q = float(bal.get(quote_coin, 0))
    free_b = float(bal.get(base_coin, 0))
    total = free_q + free_b * price
    out = ["", "── 交易所帳戶（最終真相）──",
           f"{quote_coin} {free_q:.2f} + {base_coin} {free_b:.8f}"
           f"（≈{free_b * price:.2f}）= 總值 {total:.2f}"]
    try:
        init = float(cfg.trading.get("initial_capital") or 0)
    except (TypeError, ValueError):
        init = 0.0
    # 帳戶裡有多少幣「不是機器人買的」——決定上面那個數字能不能解讀成績效
    extra_base = free_b - bot_inventory
    extra_value = extra_base * price
    mixed = abs(extra_value) > max(1.0, total * 0.02)   # 超過 1 USDT 或 2% 才提

    if init > 0:
        diff = total - init
        out.append(f"投入本金 {init:.2f} → 帳戶淨變化"
                   f" {diff:+.2f} {quote_coin}（{diff / init * 100:+.2f}%）")
    else:
        out.append("（在 config.yaml 的 trading.initial_capital 填入你存進"
                   "派網的本金，這裡就會算出帳戶淨變化）")

    if mixed:
        out += [
            f"⚠ 帳戶有 {extra_base:.8f} {base_coin}（≈{extra_value:+.2f}）"
            f"不是機器人買的（機器人存貨只有 {bot_inventory:.8f}）",
            "　→ 上面的「帳戶淨變化」含這部分的漲跌與你額外的入金，"
            "**不等於機器人績效**；機器人的成績請看前面的「真實總損益」。",
        ]
    elif init > 0:
        out.append("　（機器人存貨與帳戶持有量相符，此數字可視為機器人績效）")
    return out


def grid_report_text(cfg, store, client, now_ts: float) -> str:
    """完整網格績效報表（CLI `grid-report` 與 Discord 查詢共用）。"""
    rows = [dict(r) for r in store.trades_by_source("grid")]
    if not rows:
        return "trades 資料表裡沒有網格交易紀錄（確認在 ~/bot 目錄下跑）。"
    g = cfg.raw.get("grid", {})
    capital = float(g.get("grids", 10)) * float(g.get("quote_per_grid", 5))
    price_note = ""
    try:
        price = client.get_ticker_price(cfg.symbol)
    except Exception:  # noqa: BLE001 - 離線時退回最後成交價
        price = float(rows[-1]["price"])
        price_note = "（離線：以最後成交價代替現價）"
    d = build_grid_report(rows, grid_capital=capital,
                          current_price=price, now_ts=now_ts)
    return "\n".join([format_grid_report(d, price_note)]
                     + account_lines(cfg, client, price,
                                     bot_inventory=d.get("inventory_base", 0.0)))
