"""
actors/telegram_actor.py
────────────────────────────────────────────────────────────────────────
Stage 6 additions:
  • on_reconcile_warning() — Case A alert (exchange < expected).
    Possible external close. Notify only, no auto-correction.
  • on_reconcile_halt()    — Case B alert (exchange > expected).
    Untracked position. All new entries halted. Urgent, manual action.

All Stage 4/5 methods unchanged.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional

from risk.trade_ledger import OpenTrade, TradeLedger

log = logging.getLogger(__name__)


def _fmt_duration(secs: float) -> str:
    secs = int(abs(secs))
    if secs < 60:   return f"{secs}s"
    if secs < 3600:
        m, s = divmod(secs, 60)
        return f"{m}m {s}s" if s else f"{m}m"
    h, rem = divmod(secs, 3600)
    m = rem // 60
    return f"{h}h {m}m" if m else f"{h}h"

def _fmt_price(p: float) -> str: return f"{p:,.2f}"
def _fmt_pnl(p: float)   -> str:
    return f"+{p:.2f}" if p >= 0 else f"{p:.2f}"
def _fmt_qty(q: float)   -> str: return f"{q:+.4f}"


class TelegramNotifier:

    def __init__(
        self,
        bot_token:            str,
        chat_id:              str,
        enabled:              bool,
        notify_signals:       bool = True,
        notify_entries:       bool = True,
        notify_exits:         bool = True,
        notify_daily_summary: bool = True,
    ) -> None:
        self._token           = bot_token
        self._chat_id         = chat_id
        self._enabled         = enabled and bool(bot_token) and bool(chat_id)
        self._notify_signals  = notify_signals
        self._notify_entries  = notify_entries
        self._notify_exits    = notify_exits
        self._notify_daily    = notify_daily_summary
        self._executor        = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tg")
        self._ledgers:        dict[str, TradeLedger] = {}
        self._start_time      = datetime.now(timezone.utc)
        self._stopping        = False
        self._hb_timer:    Optional[threading.Timer] = None
        self._daily_timer: Optional[threading.Timer] = None

        if not self._enabled:
            log.info("TelegramNotifier disabled.")

    # ── Ledger registry ───────────────────────────────────────────────────
    def register_ledger(self, strategy_name: str, ledger: TradeLedger) -> None:
        self._ledgers[strategy_name] = ledger

    # ── Timer lifecycle ───────────────────────────────────────────────────
    def start_timers(self, heartbeat_mins: int, daily_summary_utc: str) -> None:
        if heartbeat_mins > 0:
            self._schedule_heartbeat(heartbeat_mins * 60.0)
        if self._notify_daily and daily_summary_utc:
            self._schedule_daily(_secs_until_utc(daily_summary_utc))

    def stop_timers(self) -> None:
        self._stopping = True
        for t in (self._hb_timer, self._daily_timer):
            if t is not None:
                t.cancel()
        self._executor.shutdown(wait=True)

    def _schedule_heartbeat(self, interval_secs: float) -> None:
        if self._stopping: return
        self._hb_timer = threading.Timer(
            interval_secs, self._fire_heartbeat, args=[interval_secs]
        )
        self._hb_timer.daemon = True
        self._hb_timer.start()

    def _fire_heartbeat(self, interval_secs: float) -> None:
        try:    self._send_heartbeat()
        except Exception as e: log.warning("TG heartbeat error: %s", e)
        self._schedule_heartbeat(interval_secs)

    def _schedule_daily(self, delay_secs: float) -> None:
        if self._stopping: return
        self._daily_timer = threading.Timer(delay_secs, self._fire_daily)
        self._daily_timer.daemon = True
        self._daily_timer.start()

    def _fire_daily(self) -> None:
        try:    self._send_daily_summary()
        except Exception as e: log.warning("TG daily summary error: %s", e)
        self._schedule_daily(86_400.0)

    # ── System events ─────────────────────────────────────────────────────
    def on_system_start(self, trader_id: str, mode: str, enabled_strats: dict) -> None:
        if not self._enabled: return
        lines = [f"🚀 <b>{trader_id}</b> online  ·  <code>{mode}</code>", ""]
        for name, s in enabled_strats.items():
            htf_part = f"+{s.htf_bar}" if s.htf_filter else ""
            lines.append(
                f"▪ <b>{name.upper()}</b>  {s.venue}:{s.symbol}  {s.primary_bar}{htf_part}  "
                f"size={s.trade_size}"
            )
        self.send("\n".join(lines))

    def on_system_stop(self, trader_id: str) -> None:
        if not self._enabled: return
        lines = [f"🔴 <b>{trader_id}</b> offline  ·  uptime {self._uptime()}", ""]
        net = 0.0
        for name, ledger in self._ledgers.items():
            s = ledger.summary_all()
            if s:
                net += s["total"]
                lines.append(
                    f"<b>{name.upper()}</b>  {s['trades']} trades  "
                    f"{s['wr']:.0f}% WR  <code>{_fmt_pnl(s['total'])}</code> USDT"
                )
            else:
                lines.append(f"<b>{name.upper()}</b>  no closed trades")
        if len(self._ledgers) > 1:
            lines.append(f"\n<b>Net</b>  <code>{_fmt_pnl(net)}</code> USDT")
        self.send("\n".join(lines))

    def on_state_restored(self, strategy_id: str, n_trades: int) -> None:
        if not self._enabled: return
        self.send(
            f"♻️ <b>State restored</b> [{strategy_id}]\n"
            f"{n_trades} open trade(s) from previous session\n"
            f"⚠️ Verify exchange positions manually"
        )

    # ── Signal events ─────────────────────────────────────────────────────
    def on_signal(self, side: str, close: float, strategy_id: str) -> None:
        if not self._enabled or not self._notify_signals: return
        icon = "📈" if side == "LONG" else "📉"
        self.send(
            f"{icon} <b>{side} signal</b>  [{strategy_id}]\n"
            f"<code>{_fmt_price(close)}</code>"
        )

    def on_entry_blocked(self, reason: str, strategy_id: str) -> None:
        if not self._enabled: return
        self.send(f"🚫 <b>Entry blocked</b>  [{strategy_id}]\n{reason}")

    # ── Trade lifecycle events ────────────────────────────────────────────
    def on_kill_switch(self, strategy_id: str, daily_pnl: float, limit: float) -> None:
        if not self._enabled: return
        self.send(
            f"⚠️ <b>KILL SWITCH</b>  [{strategy_id}]\n"
            f"Daily PnL <code>{_fmt_pnl(daily_pnl)}</code> USDT  "
            f"hit limit <code>-{limit:.2f}</code> USDT\n"
            f"No new entries until next restart"
        )

    def on_trade_opened(self, trade: OpenTrade, strategy_id: str, symbol: str = "") -> None:
        if not self._enabled or not self._notify_entries: return
        icon = "🟢" if trade.side == "LONG" else "🔴"
        self.send(
            f"{icon} <b>OPENED #{trade.trade_id:05d} {trade.side}</b>  [{strategy_id}]\n"
            f"Entry  <code>{_fmt_price(trade.entry_price)}</code>\n"
            f"SL     <code>{_fmt_price(trade.sl)}</code>\n"
            f"TP1    <code>{_fmt_price(trade.tp1)}</code>\n"
            f"TP2    <code>{_fmt_price(trade.tp2)}</code>\n"
            f"Size   {trade.full_qty}" + (f" {symbol}" if symbol else "")
        )

    def on_tp1_hit(self, trade: OpenTrade, strategy_id: str, leg_pnl: float, symbol: str = "") -> None:
        if not self._enabled or not self._notify_exits: return
        be_note = "  (SL → breakeven)" if self._is_breakeven(trade) else ""
        self.send(
            f"🎯 <b>TP1 #{trade.trade_id:05d} {trade.side}</b>  [{strategy_id}]\n"
            f"Exit   <code>{_fmt_price(trade.tp1)}</code>  "
            f"leg <code>{_fmt_pnl(leg_pnl)}</code> USDT\n"
            f"SL now <code>{_fmt_price(trade.sl)}</code>{be_note}\n"
            f"Remaining 50% still open"
        )

    def on_trade_closed(
        self, trade: OpenTrade, strategy_id: str,
        leg_pnl: float, duration_secs: float, symbol: str = "",
    ) -> None:
        if not self._enabled or not self._notify_exits: return
        if trade.exit_reason == "RESTART": return
        total  = trade.realized_pnl
        reason = trade.exit_reason
        icon   = "✅" if total > 0 else ("⚖️" if reason == "BE" else "❌")
        self.send(
            f"{icon} <b>CLOSED #{trade.trade_id:05d} {trade.side}</b>  "
            f"[{strategy_id}]  ·  <b>{reason}</b>\n"
            f"Entry  <code>{_fmt_price(trade.entry_price)}</code>  "
            f"Total PnL  <code>{_fmt_pnl(total)}</code> USDT\n"
            f"Duration  {_fmt_duration(duration_secs)}"
        )

    def on_netted_flip(
        self, strategy_id: str, net_side: str,
        net_qty: str, sum_opposing: str,
        opposing_count: int, new_qty: str, symbol: str = "",
    ) -> None:
        if not self._enabled: return
        arrow = "⬆️" if net_side == "BUY" else "⬇️"
        new_part = f" + open {new_qty}" if new_qty != "0" else ""
        unit = f" {symbol}" if symbol else ""
        self.send(
            f"🔄 <b>NETTED FLIP</b>  [{strategy_id}]\n"
            f"{arrow} <code>{net_side} {net_qty}{unit}</code>  "
            f"(close {sum_opposing}{new_part})\n"
            f"Closed {opposing_count} opposing trade(s) atomically"
        )

    # ── Stage 5: order rejection events ───────────────────────────────────
    def on_order_rejected(
        self, strategy_id: str, trade_id: int, reason: str
    ) -> None:
        if not self._enabled: return
        self.send(
            f"⛔ <b>ORDER REJECTED</b>  [{strategy_id}]\n"
            f"Trade #{trade_id:05d} entry rejected by exchange\n"
            f"Reason: <code>{reason}</code>\n"
            f"Trade removed from ledger — no position opened"
        )

    def on_close_order_rejected(
        self, strategy_id: str, client_order_id: str, reason: str
    ) -> None:
        if not self._enabled: return
        self.send(
            f"🚨 <b>CLOSE ORDER REJECTED</b>  [{strategy_id}]\n"
            f"coid: <code>{client_order_id}</code>\n"
            f"Reason: <code>{reason}</code>\n"
            f"⚠️ <b>POSITION MAY BE UNPROTECTED</b>\n"
            f"Check exchange immediately and close manually if needed"
        )

    def on_close_reverted(
        self, strategy_id: str, trade_id: int, reason: str
    ) -> None:
        if not self._enabled: return
        self.send(
            f"🔄 <b>CLOSE REVERTED</b>  [{strategy_id}]\n"
            f"Trade #{trade_id:05d} close rejected (<code>{reason}</code>)\n"
            f"Trade returned to open_trades — will retry next bar"
        )

    # ── Stage 6: reconciliation events ────────────────────────────────────
    def on_reconcile_warning(
        self,
        expected:  float,
        actual:    float,
        diff:      float,
        breakdown: dict[str, float],
        group:     str = "",
    ) -> None:
        """
        Case A: exchange position is less than expected, for the given
        (venue, instrument) group.
        Possible external close (liquidation, manual, ADL) -- or an
        internal ledger bug (see reconcile_case_a_analysis.md).
        Notify only — no auto-correction applied yet.
        """
        if not self._enabled: return
        lines = [
            "⚠️ <b>RECONCILE WARNING</b>  Case A" + (f"  [{group}]" if group else ""),
            "Exchange shows less exposure than expected",
            "",
            f"Expected : <code>{expected:+.6f}</code>",
            f"Actual   : <code>{actual:+.6f}</code>",
            f"Diff     : <code>{diff:+.6f}</code>",
            "",
            "Possible cause: liquidation, manual close, ADL, or an internal ledger bug",
            "No auto-correction applied — review and confirm manually",
        ]
        if breakdown:
            lines.append("")
            for name, qty in breakdown.items():
                lines.append(
                    f"  {name.upper()}: expected <code>{_fmt_qty(qty)}</code>"
                )
        self.send("\n".join(lines))

    def on_reconcile_halt(
        self,
        expected:  float,
        actual:    float,
        diff:      float,
        breakdown: dict[str, float],
        group:     str = "",
    ) -> None:
        """
        Case B: exchange position is more than expected, for the given
        (venue, instrument) group.
        Untracked position — new entries for that group halted immediately.
        Other (venue, instrument) groups keep trading normally.
        Requires manual restart to resume trading for this group.
        """
        if not self._enabled: return
        lines = [
            "🚨 <b>RECONCILE HALT</b>  Case B" + (f"  [{group}]" if group else ""),
            "Untracked position detected — <b>new entries HALTED for this group</b>",
            "",
            f"Expected : <code>{expected:+.6f}</code>",
            f"Actual   : <code>{actual:+.6f}</code>",
            f"Diff     : <code>{diff:+.6f}</code>",
            "",
            "⚠️ <b>Manual intervention required</b>",
            "Resolve the discrepancy on the exchange,",
            "then restart the system to resume trading",
        ]
        if breakdown:
            lines.append("")
            for name, qty in breakdown.items():
                lines.append(
                    f"  {name.upper()}: expected <code>{_fmt_qty(qty)}</code>"
                )
        self.send("\n".join(lines))

    # ── Internal periodic senders ─────────────────────────────────────────
    def _send_heartbeat(self) -> None:
        if not self._enabled: return
        lines = [f"💓 <b>Uptime {self._uptime()}</b>"]
        for name, ledger in self._ledgers.items():
            lines.append(
                f"▪ <b>{name.upper()}</b>  {ledger.open_count} open  "
                f"session <code>{_fmt_pnl(ledger.daily_pnl)}</code> USDT"
            )
        self.send("\n".join(lines))

    def _send_daily_summary(self) -> None:
        if not self._enabled or not self._notify_daily: return
        date  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lines = [f"📊 <b>Daily Summary</b>  ·  {date}", ""]
        net   = 0.0
        for name, ledger in self._ledgers.items():
            s = ledger.summary_all()
            if s:
                net += s["total"]
                lines.append(
                    f"<b>{name.upper()}</b>  {s['trades']} trades  "
                    f"{s['wr']:.0f}% WR  <code>{_fmt_pnl(s['total'])}</code> USDT"
                )
            else:
                lines.append(f"<b>{name.upper()}</b>  no trades today")
        if len(self._ledgers) > 1:
            lines.append(f"\n<b>Net</b>  <code>{_fmt_pnl(net)}</code> USDT")
        self.send("\n".join(lines))

    # ── HTTP send ─────────────────────────────────────────────────────────
    def send(self, text: str) -> None:
        if not self._enabled: return
        self._executor.submit(self._send_sync, text)

    def _send_sync(self, text: str) -> None:
        url  = f"https://api.telegram.org/bot{self._token}/sendMessage"
        data = json.dumps({
            "chat_id": self._chat_id, "text": text, "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10): pass
        except urllib.error.HTTPError as e:
            log.warning("Telegram HTTP %d — %s", e.code, e.reason)
        except Exception as e:
            log.warning("Telegram send failed: %s", e)

    # ── Helpers ───────────────────────────────────────────────────────────
    def _uptime(self) -> str:
        return _fmt_duration(
            (datetime.now(timezone.utc) - self._start_time).total_seconds()
        )

    @staticmethod
    def _is_breakeven(trade: OpenTrade) -> bool:
        return trade.tp1_hit and abs(trade.sl - trade.entry_price) < 0.01


def _secs_until_utc(utc_time_str: str) -> float:
    now = datetime.now(timezone.utc)
    h, m = map(int, utc_time_str.split(":"))
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()
