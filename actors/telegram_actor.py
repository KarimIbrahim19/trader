"""
actors/telegram_actor.py
────────────────────────────────────────────────────────────────────────
Telegram notification system for the live SMC trading system.

Design principles:
  • Plain Python class — NOT an NT Actor. Trade events come from our
    own PositionManager ledger, not NT's netted position events.
  • Fire-and-forget: every HTTP call runs in a thread pool and returns
    immediately. The trading loop is never blocked.
  • Graceful degradation: if Telegram is down, disabled, or misconfigured,
    every method is a silent no-op. The system always trades first.
  • Combined heartbeat: a single message covers all registered strategies.
    Timers run in daemon threads so they die cleanly with the process.

Notification settings (from settings.yaml telegram block):
  notify_signals       — signal fired (+ blocked note if entry gated)
  notify_entries       — trade opened
  notify_exits         — TP1 partial close, final close
  notify_daily_summary — combined summary at daily_summary_utc
  heartbeat_interval_mins — combined uptime + open trades message

Usage flow (wired in main.py and base_smc_strategy.py):
  1. main.py creates one TelegramNotifier from settings
  2. strategy.set_notifier(notifier) called before node.build()
  3. on_start(): strategy calls notifier.register_ledger(name, ledger)
  4. main.py calls notifier.on_system_start() + notifier.start_timers()
     after node.build() (all strategies already running)
  5. Trade events: PositionManager calls notifier methods directly
  6. Signal events: BaseSmcStrategy.on_bar() calls notifier.on_signal()
  7. on_stop(): main.py calls notifier.stop_timers() + on_system_stop()
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


# ── Formatting helpers ─────────────────────────────────────────────────────
def _fmt_duration(secs: float) -> str:
    """Human-readable duration: 3720 → '1h 2m'"""
    secs = int(abs(secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        m, s = divmod(secs, 60)
        return f"{m}m {s}s" if s else f"{m}m"
    h, rem = divmod(secs, 3600)
    m = rem // 60
    return f"{h}h {m}m" if m else f"{h}h"


def _fmt_price(p: float) -> str:
    return f"{p:,.2f}"


def _fmt_pnl(p: float) -> str:
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.2f}"


# ── Notifier ──────────────────────────────────────────────────────────────
class TelegramNotifier:
    """
    Sends formatted Telegram messages for every trade lifecycle event.

    All public methods are safe to call unconditionally — if `enabled`
    is False or tokens are missing, every call is a no-op.
    """

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

        # Thread pool — max 2 workers keeps ordering; Telegram rate-limits at 30 msg/s
        self._executor   = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tg")
        self._ledgers:   dict[str, TradeLedger] = {}
        self._start_time = datetime.now(timezone.utc)
        self._stopping   = False

        self._hb_timer:    Optional[threading.Timer] = None
        self._daily_timer: Optional[threading.Timer] = None

        if not self._enabled:
            log.info(
                "TelegramNotifier disabled — "
                "set telegram.enabled=true and provide bot_token + chat_id to activate."
            )

    # ── Ledger registry ───────────────────────────────────────────────────
    def register_ledger(self, strategy_name: str, ledger: TradeLedger) -> None:
        """
        Called by each strategy in on_start() so the heartbeat and daily
        summary can show a combined view of all running strategies.
        """
        self._ledgers[strategy_name] = ledger

    # ── Timer lifecycle ───────────────────────────────────────────────────
    def start_timers(self, heartbeat_mins: int, daily_summary_utc: str) -> None:
        """
        Start heartbeat and daily summary timers.
        Called from main.py after node.build() — all strategies running.
        """
        if heartbeat_mins > 0:
            self._schedule_heartbeat(heartbeat_mins * 60.0)
            log.info("TelegramNotifier: heartbeat every %d min", heartbeat_mins)

        if self._notify_daily and daily_summary_utc:
            delay = _secs_until_utc(daily_summary_utc)
            self._schedule_daily(delay)
            log.info(
                "TelegramNotifier: daily summary in %.0fs (at %s UTC)",
                delay, daily_summary_utc,
            )

    def stop_timers(self) -> None:
        """Called from main.py finally block before node.dispose()."""
        self._stopping = True
        for t in (self._hb_timer, self._daily_timer):
            if t is not None:
                t.cancel()
        self._executor.shutdown(wait=True)

    def _schedule_heartbeat(self, interval_secs: float) -> None:
        if self._stopping:
            return
        self._hb_timer = threading.Timer(
            interval_secs, self._fire_heartbeat, args=[interval_secs]
        )
        self._hb_timer.daemon = True
        self._hb_timer.start()

    def _fire_heartbeat(self, interval_secs: float) -> None:
        try:
            self._send_heartbeat()
        except Exception as e:
            log.warning("TelegramNotifier heartbeat error: %s", e)
        self._schedule_heartbeat(interval_secs)   # always re-schedule

    def _schedule_daily(self, delay_secs: float) -> None:
        if self._stopping:
            return
        self._daily_timer = threading.Timer(delay_secs, self._fire_daily)
        self._daily_timer.daemon = True
        self._daily_timer.start()

    def _fire_daily(self) -> None:
        try:
            self._send_daily_summary()
        except Exception as e:
            log.warning("TelegramNotifier daily summary error: %s", e)
        self._schedule_daily(86_400.0)    # re-schedule in exactly 24h

    # ── System-level events (called from main.py) ─────────────────────────
    def on_system_start(
        self,
        trader_id:      str,
        mode:           str,
        enabled_strats: dict,    # {name: StrategySettingsBase}
    ) -> None:
        if not self._enabled:
            return
        lines = [f"🚀 <b>{trader_id}</b> online  ·  <code>{mode}</code>", ""]
        for name, s in enabled_strats.items():
            htf_part = f"+{s.htf_bar}" if s.htf_filter else ""
            lines.append(
                f"{'▪'} <b>{name.upper()}</b>  {s.primary_bar}{htf_part}  "
                f"size={s.trade_size} BTC"
            )
        self.send("\n".join(lines))

    def on_system_stop(self, trader_id: str) -> None:
        if not self._enabled:
            return
        lines = [f"🔴 <b>{trader_id}</b> offline  ·  uptime {self._uptime()}", ""]
        if self._ledgers:
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
        """Called from strategy on_start() when persistence state is loaded."""
        if not self._enabled:
            return
        self.send(
            f"♻️ <b>State restored</b> [{strategy_id}]\n"
            f"{n_trades} open trade(s) from previous session\n"
            f"⚠️ Verify exchange positions manually"
        )

    # ── Signal events (called from base_smc_strategy.on_bar) ─────────────
    def on_signal(self, side: str, close: float, strategy_id: str) -> None:
        """Signal fired — sent regardless of whether entry is allowed."""
        if not self._enabled or not self._notify_signals:
            return
        icon = "📈" if side == "LONG" else "📉"
        self.send(
            f"{icon} <b>{side} signal</b>  [{strategy_id}]\n"
            f"<code>{_fmt_price(close)}</code>"
        )

    def on_entry_blocked(self, reason: str, strategy_id: str) -> None:
        """
        Called when a signal fires but entry is blocked.
        Reasons: 'max open trades (N/M)' or 'daily loss limit ($X hit)'
        Shown as a separate note after the signal message.
        """
        if not self._enabled:
            return
        self.send(f"🚫 <b>Entry blocked</b>  [{strategy_id}]\n{reason}")

    # ── Trade lifecycle events (called from position_manager) ─────────────
    def on_kill_switch(
        self, strategy_id: str, daily_pnl: float, limit: float
    ) -> None:
        """Daily loss limit hit — shown once, not on every subsequent bar."""
        if not self._enabled:
            return
        self.send(
            f"⚠️ <b>KILL SWITCH</b>  [{strategy_id}]\n"
            f"Daily PnL <code>{_fmt_pnl(daily_pnl)}</code> USDT hit limit "
            f"<code>-{limit:.2f}</code> USDT\n"
            f"No new entries until next restart"
        )

    def on_trade_opened(self, trade: OpenTrade, strategy_id: str) -> None:
        if not self._enabled or not self._notify_entries:
            return
        icon = "🟢" if trade.side == "LONG" else "🔴"
        self.send(
            f"{icon} <b>OPENED #{trade.trade_id:05d} {trade.side}</b>  [{strategy_id}]\n"
            f"Entry  <code>{_fmt_price(trade.entry_price)}</code>\n"
            f"SL     <code>{_fmt_price(trade.sl)}</code>\n"
            f"TP1    <code>{_fmt_price(trade.tp1)}</code>\n"
            f"TP2    <code>{_fmt_price(trade.tp2)}</code>\n"
            f"Size   {trade.full_qty} BTC"
        )

    def on_tp1_hit(
        self,
        trade:       OpenTrade,
        strategy_id: str,
        leg_pnl:     float,
    ) -> None:
        """
        Called after TP1 fires and state is updated (tp1_hit=True, SL moved).
        trade.sl already reflects breakeven if that toggle is on.
        """
        if not self._enabled or not self._notify_exits:
            return
        be_note = "  (SL → breakeven)" if self.config_breakeven(trade) else ""
        self.send(
            f"🎯 <b>TP1 #{trade.trade_id:05d} {trade.side}</b>  [{strategy_id}]\n"
            f"Exit   <code>{_fmt_price(trade.tp1)}</code>  "
            f"leg <code>{_fmt_pnl(leg_pnl)}</code> USDT\n"
            f"SL now <code>{_fmt_price(trade.sl)}</code>{be_note}\n"
            f"Remaining 50% still open"
        )

    @staticmethod
    def config_breakeven(trade: OpenTrade) -> bool:
        """Heuristic: SL was moved to entry if sl == entry_price after TP1."""
        return trade.tp1_hit and abs(trade.sl - trade.entry_price) < 0.01

    def on_trade_closed(
        self,
        trade:         OpenTrade,
        strategy_id:   str,
        leg_pnl:       float,
        duration_secs: float,
    ) -> None:
        """Final close — any reason except RESTART (process stop, not a real exit)."""
        if not self._enabled or not self._notify_exits:
            return
        if trade.exit_reason == "RESTART":
            return   # suppressed — not a real trading outcome

        total = trade.realized_pnl
        reason = trade.exit_reason

        if total > 0:
            icon = "✅"
        elif reason == "BE":
            icon = "⚖️"
        else:
            icon = "❌"

        self.send(
            f"{icon} <b>CLOSED #{trade.trade_id:05d} {trade.side}</b>  "
            f"[{strategy_id}]  ·  <b>{reason}</b>\n"
            f"Entry  <code>{_fmt_price(trade.entry_price)}</code>  "
            f"Total PnL  <code>{_fmt_pnl(total)}</code> USDT\n"
            f"Duration  {_fmt_duration(duration_secs)}"
        )

    # ── Internal periodic senders ─────────────────────────────────────────
    def _send_heartbeat(self) -> None:
        if not self._enabled:
            return
        lines = [f"💓 <b>Uptime {self._uptime()}</b>"]
        for name, ledger in self._ledgers.items():
            pnl = ledger.daily_pnl
            lines.append(
                f"{'▪'} <b>{name.upper()}</b>  "
                f"{ledger.open_count} open  "
                f"session <code>{_fmt_pnl(pnl)}</code> USDT"
            )
        self.send("\n".join(lines))

    def _send_daily_summary(self) -> None:
        if not self._enabled or not self._notify_daily:
            return
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lines = [f"📊 <b>Daily Summary</b>  ·  {date}", ""]
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
                lines.append(f"<b>{name.upper()}</b>  no trades today")
        if len(self._ledgers) > 1:
            lines.append(f"\n<b>Net</b>  <code>{_fmt_pnl(net)}</code> USDT")
        self.send("\n".join(lines))

    # ── HTTP send ─────────────────────────────────────────────────────────
    def send(self, text: str) -> None:
        """Submit message to thread pool — returns immediately, never blocks."""
        if not self._enabled:
            return
        self._executor.submit(self._send_sync, text)

    def _send_sync(self, text: str) -> None:
        """Actual blocking HTTP call — runs in the thread pool only."""
        url  = f"https://api.telegram.org/bot{self._token}/sendMessage"
        data = json.dumps({
            "chat_id":              self._chat_id,
            "text":                 text,
            "parse_mode":           "HTML",
            "disable_notification": False,
        }).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10):
                pass   # success — response body not needed
        except urllib.error.HTTPError as e:
            log.warning("Telegram HTTP %d — %s", e.code, e.reason)
        except Exception as e:
            log.warning("Telegram send failed: %s", e)

    # ── Helpers ───────────────────────────────────────────────────────────
    def _uptime(self) -> str:
        elapsed = (datetime.now(timezone.utc) - self._start_time).total_seconds()
        return _fmt_duration(elapsed)


# ── Module-level helper ───────────────────────────────────────────────────
def _secs_until_utc(utc_time_str: str) -> float:
    """Seconds until next occurrence of 'HH:MM' UTC."""
    now = datetime.now(timezone.utc)
    h, m = map(int, utc_time_str.split(":"))
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()
