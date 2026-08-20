"""自動網格交易執行器。

行為：
- 在現價附近建立網格（區間均分成數格）。
- 每輪：價格下跌觸及某格 → 市價買入該格；持有的格子，價格漲到上一格 → 賣出獲利。
- 風險控管（自動）：
    * 價格跌破區間下緣（含緩衝）或未實現虧損超過上限 → 平倆關閉整個網格，
      然後在「現價」重新開一個新網格繼續交易。
    * 價格突破區間上緣 → 獲利了結後向上重新定位。
    * 空手且價格出界 → 搬家零成本，不等緩衝直接重新定位。
- 狀態（區間、持有格、已實現利潤）存進 store，重啟後可接續。

注意：自製版用市價單，手續費/滑價較高；網格利潤本來就薜，要做真錢建議優先用派網內建網格。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from ..config import Config
from ..notifier import Notifier


class GridRunner:
    def __init__(self, cfg: Config, client, broker, store, notifier: Notifier):
        self.cfg = cfg
        self.client = client
        self.broker = broker
        self.store = store
        self.notifier = notifier

        g = cfg.raw.get("grid", {})
        self.symbol = cfg.symbol
        self.grids = int(g.get("grids", 10))
        self.quote_per_grid = float(g.get("quote_per_grid", 5))
        self.auto_range = bool(g.get("auto_range", True))
        self.range_pct = float(g.get("range_pct", 0.15))
        self.lower_cfg = g.get("lower")
        self.upper_cfg = g.get("upper")
        self.poll_seconds = int(g.get("poll_seconds", 20))
        self.breakout_buffer = float(g.get("breakout_buffer", 0.02))
        self.max_loss_quote = float(g.get("max_loss_quote", 0) or 0) or None
        self.reset_on_breakout = bool(g.get("reset_on_breakout", True))

        # ① ATR 動態區間：range_mode = fixed | atr
        self.range_mode = str(g.get("range_mode", "fixed")).lower()
        self.atr_period = int(g.get("atr_period", 14))
        self.atr_mult = float(g.get("atr_mult", 6))
        # ② 震盪盤過濾：ADX 高於 adx_max（強趨勢）就不開網格
        self.regime_filter = bool(g.get("regime_filter", False))
        self.adx_period = int(g.get("adx_period", 14))
        self.adx_max = float(g.get("adx_max", 30))
        # 指標用的 K 線
        self.indicator_interval = str(g.get("indicator_interval", "60M"))
        self.indicator_limit = int(g.get("indicator_limit", 150))

        self._running = False
        self._paused = False   # 是否因強趨勢暫停（避免重複通知）
        self._last_fail = ""   # 下單失敗去重，避免同一錯誤每輪洗版

        # 每日結算（utc_hour 預設 12 = 台灣晚上 8 點）
        dcfg = cfg.notify.get("daily_summary", {})
        self.summary_enabled = bool(dcfg.get("enabled", True))
        self.summary_hour = int(dcfg.get("utc_hour", 12))
        self._last_summary_day = store.get_meta("grid_summary_day", "") or ""

        # Telegram 指令輪詢：用「啟動時間」判斷，只回應啟動後傳來的訊息
        self._tg_offset = None
        self._start_ts = time.time()
        # Discord 指令輪詢：記錄看過的最新訊息 id，只回應啟動後的新訊息
        self._discord_after = None

    # ----- 網格計算 -----
    def _levels(self, lower: float, upper: float) -> list[float]:
        step = (upper - lower) / self.grids
        return [lower + i * step for i in range(self.grids + 1)]

    # ----- 指標（ATR / ADX）-----
    def _ohlc_series(self):
        """抓指標用 K 線，回傳 (high, low, close) 三個 pandas Series 或 None。"""
        if self.client is None:
            return None
        try:
            import pandas as pd
            kl = self.client.get_klines(self.symbol, self.indicator_interval,
                                        limit=self.indicator_limit)
        except Exception:  # noqa: BLE001
            return None
        if not kl or len(kl) < max(self.atr_period, self.adx_period) + 2:
            return None
        def col(keys):
            out = []
            for k in kl:
                if isinstance(k, dict):
                    v = next((k[x] for x in keys if x in k), None)
                else:
                    idx = {"h": 2, "l": 3, "c": 4}[keys[0][0]]
                    v = k[idx]
                out.append(float(v))
            return pd.Series(out)
        return col(["high", "h"]), col(["low", "l"]), col(["close", "c"])

    def _atr_value(self) -> Optional[float]:
        s = self._ohlc_series()
        if s is None:
            return None
        from ..strategy import indicators
        val = indicators.atr(s[0], s[1], s[2], self.atr_period).iloc[-1]
        return float(val) if val == val else None  # 過濾 NaN

    def _is_ranging(self) -> bool:
        """ADX 判斷是否為震盪盤（適合網格）。取不到資料時預設 True（不阻擋）。"""
        if not self.regime_filter:
            return True
        s = self._ohlc_series()
        if s is None:
            return True
        from ..strategy import indicators
        adx_val = indicators.adx(s[0], s[1], s[2], self.adx_period).iloc[-1]
        if adx_val != adx_val:  # NaN
            return True
        return float(adx_val) < self.adx_max

    def _grid_bounds(self, price: float) -> tuple[float, float]:
        # ATR 模式：用波動決定半區間
        if self.range_mode == "atr":
            atr_val = self._atr_value()
            if atr_val:
                half = self.atr_mult * atr_val
                return price - half, price + half
        # 固定模式 / ATR 取不到時退回
        if self.auto_range or not (self.lower_cfg and self.upper_cfg):
            return price * (1 - self.range_pct), price * (1 + self.range_pct)
        return float(self.lower_cfg), float(self.upper_cfg)

    def _new_grid(self, price: float) -> dict:
        lower, upper = self._grid_bounds(price)
        state = {"active": True, "lower": lower, "upper": upper,
                 "grids": self.grids, "held": {}, "realized": 0.0,
                 "created_price": price, "last_price": price}
        self.store.save_grid_state(state)
        self.notifier.send(
            f"🔲 開新網格：{lower:.2f}~{upper:.2f}（{self.grids} 格 / 每格 "
            f"{self.quote_per_grid} USDT）現價 {price:.2f}", "info", important=True)
        return state

    def _try_open(self, price: float) -> None:
        """要開新網格前，先用 ADX 確認是震盪盤；強趨勢則暫停（不接刀）。"""
        if not self._is_ranging():
            if not self._paused:
                self._paused = True
                self.notifier.send(
                    "⏸️ 趨勢過強（ADX 偏高），暫停開網格，等行情轉震盪再自動開。",
                    "info", important=True)
            return
        if self._paused:
            self._paused = False
            self.notifier.send("▶️ 行情轉為震盪，恢復開網格。", "info", important=True)
        self._new_grid(price)

    def _order_fail(self, msg: str) -> None:
        """下單失敗：第一次推播通知，之後同樣的錯誤只記 log（避免每 20 秒洗版）。"""
        if msg != self._last_fail:
            self._last_fail = msg
            self.notifier.send(f"❌ {msg}", "error")
        else:
            self.notifier.send(f"❌ {msg}", "warning", push=False)

    def _close_grid(self, state: dict, price: float, reason: str) -> None:
        held = {int(k): v for k, v in state.get("held", {}).items()}
        total_qty = sum(held.values())
        realized = state.get("realized", 0.0)
        if total_qty > 0:
            res = self.broker.market_sell(self.symbol, total_qty)
            if not res.ok:
                # 平倆失敗：保留狀態下一輪重試，絕不能弄丟持倆追蹤
                self._order_fail(f"關閉網格平倆失敗（{total_qty:.8f} BTC）：{res.error}")
                return
            # 倒貨的損益也要進 realized_pnl——否則「歷次累計已實現」只算
            # 收割的贏、藏掉重開的虧，帳面會系統性高估（2026-07 儀表稽核發現）
            n = int(state.get("grids", self.grids))
            step = (state["upper"] - state["lower"]) / n if n else 0
            cost = sum(q * (state["lower"] + i * step) for i, q in held.items())
            self.store.record_trade(
                symbol=self.symbol, side="SELL", base=res.filled_base,
                quote=res.filled_quote, price=res.avg_price,
                simulated=res.simulated, source="grid:close",
                realized_pnl=res.filled_quote - cost)
        state["active"] = False
        state["held"] = {}
        self.store.save_grid_state(state)
        self.notifier.send(
            f"⚠️ 關閉網格（{reason}）：賣出存貨 {total_qty:.8f} @ {price:.2f}"
            f"｜本輪累積已實現利潤 {realized:+.2f}", "info", important=True)

    # ----- 狀態查詢 / 通知互動 -----
    def status_text(self) -> str:
        state = self.store.load_grid_state()
        if not state or not state.get("active"):
            return "目前沒有運行中的網格。"
        lower, upper = state["lower"], state["upper"]
        levels = self._levels(lower, upper)
        held = {int(k): v for k, v in state.get("held", {}).items()}
        try:
            price = self.broker.get_price(self.symbol)
        except Exception:  # noqa: BLE001
            price = 0.0
        invested = sum(q * levels[i] for i, q in held.items())
        unreal = sum(q * (price - levels[i]) for i, q in held.items()) if price else 0.0
        lines = [
            f"🔲 網格狀態（{self.symbol}）",
            f"區間 {lower:.0f}~{upper:.0f}，共 {self.grids} 格",
            f"現價 {price:.2f}",
            f"持有 {len(held)} 格：",
        ]
        for i in sorted(held):
            lines.append(f"  ・買在 {levels[i]:.0f}（{held[i]:.8f}）")
        if not held:
            lines.append("  （目前空手）")
        lines.append(f"投入成本 {invested:.2f} USDT，未實現 {unreal:+.2f}")
        lines.append(f"本網格已實現利潤 {state.get('realized', 0.0):+.2f}")
        try:
            total_pnl = self.store.stats_since(0)[1]
            lines.append(f"歷次累計已實現 {total_pnl:+.2f}")
        except Exception:  # noqa: BLE001 - 統計失敗不影響狀態顯示
            pass
        return "\n".join(lines)

    def _poll_commands(self) -> None:
        """讀取 Telegram 訊息：收到任何訊息就回覆網格狀態。"""
        if not self.notifier.tg_enabled:
            return
        updates = self.notifier.get_updates(offset=self._tg_offset)
        for u in updates:
            self._tg_offset = int(u["update_id"]) + 1
            msg = u.get("message") or u.get("channel_post") or {}
            if not msg.get("text"):
                continue
            # 略過機器人啟動之前傳的舊訊息
            if float(msg.get("date", 0)) < self._start_ts:
                continue
            # 只回應設定的 chat_id（避免陌生人查你的部位）
            chat = str((msg.get("chat") or {}).get("id", ""))
            if self.notifier.tg_chat and chat != str(self.notifier.tg_chat):
                continue
            self.notifier.send(self.status_text(), important=True)

    def _poll_discord_commands(self) -> None:
        """讀取 Discord 訊息：收到任何訊息就回覆網格狀態（唯讀查詢）。"""
        if not self.notifier.discord_bot_enabled:
            return
        msgs = self.notifier.get_discord_messages(after=self._discord_after)
        if not msgs:
            return
        # Discord 訊息 id 是遞增雪花碼，記錄最新的當作下次輪詢基準
        newest = max(int(m["id"]) for m in msgs if str(m.get("id", "")).isdigit())
        first_poll = self._discord_after is None
        self._discord_after = str(newest)
        # 第一次輪詢只設基準，不回覆機器人啟動前的歷史訊息
        if first_poll:
            return
        for m in msgs:
            # 略過機器人／Webhook 自己發的訊息，否則會對自己的推播無限回覆
            if m.get("webhook_id") or (m.get("author") or {}).get("bot"):
                continue
            # 只回應設定的 Discord 使用者（避免陌生人查你的部位）
            author = str((m.get("author") or {}).get("id", ""))
            if self.notifier.discord_user_id and author != str(self.notifier.discord_user_id):
                continue
            # 關鍵字 → 績效儀表（真實現金流損益）；其餘任何訊息 → 網格即時狀態
            txt = str(m.get("content", "")).strip().lower()
            if any(w in txt for w in ("報表", "損益", "績效", "賺", "結算",
                                      "pnl", "report")):
                from ..report import grid_report_text
                self.notifier.send(
                    grid_report_text(self.cfg, self.store, self.client,
                                     time.time()), important=True)
            else:
                self.notifier.send(self.status_text(), important=True)
            break  # 一輪只回一次，避免洗版

    def _maybe_daily_summary(self) -> None:
        if not self.summary_enabled:
            return
        now = datetime.now(timezone.utc)
        if now.hour < self.summary_hour:
            return
        today = now.strftime("%Y-%m-%d")
        if self._last_summary_day == today:
            return
        self._last_summary_day = today
        self.store.set_meta("grid_summary_day", today)
        self.notifier.send("📊 每日結算\n" + self.status_text(), important=True)

    # ----- 主迴圈 -----
    def run_once(self) -> None:
        try:
            price = self.broker.get_price(self.symbol)
        except Exception as exc:  # noqa: BLE001
            self.notifier.send(f"網格取價失敗：{exc}", "warning")
            return
        if not price or price <= 0:
            return

        state = self.store.load_grid_state()
        if not state or not state.get("active"):
            self._try_open(price)
            return

        lower, upper = state["lower"], state["upper"]
        levels = self._levels(lower, upper)
        held = {int(k): v for k, v in state.get("held", {}).items()}
        last = state.get("last_price", price)   # 上一輪價格，用來判斷「穿越」

        # 空手且價格出界：沒有存貨要賣，搬家零成本 → 不必等緩衝，直接重新定位
        # （緩衝的目的是避免「抱著貨時」在邊界反覆停損重開；空手時不適用）
        if not held and (price > upper or price < lower) and self.reset_on_breakout:
            direction = "向上" if price > upper else "向下"
            self._close_grid(state, price, f"空手出界，{direction}重新定位")
            self._try_open(price)
            return

        # 風險檢查：跌破下緣 or 未實現虧損超限 → 關閉並重開
        unreal = sum(q * (price - levels[i]) for i, q in held.items())
        breach_down = price < lower * (1 - self.breakout_buffer)
        max_loss_hit = self.max_loss_quote is not None and unreal <= -self.max_loss_quote
        if breach_down or max_loss_hit:
            reason = "跌破下緣" if breach_down else f"未實現虧損達 {unreal:.2f}"
            self._close_grid(state, price, reason)
            if self.reset_on_breakout:
                self._try_open(price)   # 重開前先確認不是強趨勢
            return

        # 突破上緣 → 獲利了結後向上重新定位
        if price > upper * (1 + self.breakout_buffer) and self.reset_on_breakout:
            self._close_grid(state, price, "突破上緣，向上重新定位")
            self._try_open(price)
            return

        # 正常網格成交
        for i in range(self.grids):
            buy_px, sell_px = levels[i], levels[i + 1]
            # 賣出：持有，且價格已達上一格（不要求「穿越」→ 失敗/重啟後可自動補賣重試）
            if i in held and price >= sell_px:
                res = self.broker.market_sell(self.symbol, held[i])
                if res.ok:
                    profit = (res.avg_price - buy_px) * res.filled_base
                    state["realized"] = state.get("realized", 0.0) + profit
                    self.store.record_trade(
                        symbol=self.symbol, side="SELL", base=res.filled_base,
                        quote=res.filled_quote, price=res.avg_price,
                        simulated=res.simulated, source="grid", realized_pnl=profit)
                    del held[i]
                    # 成交頻繁：只記 log、不外推通知
                    self.notifier.send(f"🟢 網格賣出 @ {res.avg_price:.2f}（+{profit:.2f}）",
                                       "info", push=False)
                else:
                    self._order_fail(
                        f"網格賣出失敗（格 {buy_px:.0f}→{sell_px:.0f}，"
                        f"size={held[i]}）：{res.error}")
            # 買入：未持有，且價格由上『向下穿過』該格
            if i not in held and last > buy_px >= price:
                res = self.broker.market_buy(self.symbol, self.quote_per_grid)
                if res.ok:
                    held[i] = res.filled_base
                    self.store.record_trade(
                        symbol=self.symbol, side="BUY", base=res.filled_base,
                        quote=res.filled_quote, price=res.avg_price,
                        simulated=res.simulated, source="grid")
                    self.notifier.send(f"🔴 網格買入 @ {res.avg_price:.2f}",
                                       "info", push=False)
                else:
                    self._order_fail(f"網格買入失敗（格 {buy_px:.0f}）：{res.error}")

        state["held"] = {str(k): v for k, v in held.items()}
        state["last_price"] = price
        self.store.save_grid_state(state)

    def run_forever(self) -> None:
        self._running = True
        mode = "實盤" if self.cfg.is_live else "紙上"
        need = self.quote_per_grid * self.grids
        self.notifier.send(
            f"🤖 網格機器人啟動（{mode}模式）｜{self.symbol}｜{self.grids} 格 / 每格 "
            f"{self.quote_per_grid} USDT（需約 {need:.0f} USDT）", "info", important=True)
        while self._running:
            try:
                self._poll_commands()          # 回覆 Telegram 查詢
                self._poll_discord_commands()  # 回覆 Discord 查詢
                self.run_once()                # 網格交易
                self._maybe_daily_summary() # 每日結算
            except Exception as exc:  # noqa: BLE001
                self.notifier.send(f"⚠️ 網格迴圈錯誤：{exc}", "error")
            time.sleep(self.poll_seconds)

    def stop(self) -> None:
        self._running = False
