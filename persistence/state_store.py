"""
persistence/state_store.py
────────────────────────────────────────────────────────────────────────
Saves and loads open trade state per strategy so a process restart
does not orphan live positions.

Each strategy gets its own JSON file named by strategy_id so MS and FVG
(and any future strategy) never collide:
    state/ms-001_state.json
    state/fvg-001_state.json

On a clean shutdown:   save()  is called → file written.
On restart:            load()  is called → open trades restored into the
                                           TradeLedger via
                                           ledger.restore_from_persistence().
On a crash (no save):  load()  finds the file from the previous run and
                                restores whatever was last persisted.

Important reconciliation note:
  In dry_run mode no real orders are placed, so state is purely virtual
  and restoration is trivially correct.
  In paper/live mode the restored trades represent orders that are still
  open on the exchange. The strategy logs a WARNING with each restored
  trade so the operator can verify manually before trusting the state.
  Automated reconciliation against exchange positions is planned for
  Stage 6 (reliability & monitoring).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from risk.trade_ledger import OpenTrade, TradeLedger

log = logging.getLogger(__name__)


# ── Serialisation helpers ─────────────────────────────────────────────────
def _trade_to_dict(t: OpenTrade) -> dict:
    return {
        "trade_id":      t.trade_id,
        "side":          t.side,
        "entry_price":   t.entry_price,
        "entry_ts":      t.entry_ts,
        "full_qty":      str(t.full_qty),      # Decimal → str, exact
        "sl":            t.sl,
        "tp1":           t.tp1,
        "tp2":           t.tp2,
        "tp1_hit":       t.tp1_hit,
        "realized_pnl":  t.realized_pnl,
        "exit_ts":       t.exit_ts,
        "exit_reason":   t.exit_reason,
        "best_price":    t.best_price,
        "trail_distance": t.trail_distance,
    }


def _trade_from_dict(d: dict) -> OpenTrade:
    return OpenTrade(
        trade_id      = d["trade_id"],
        side          = d["side"],
        entry_price   = float(d["entry_price"]),
        entry_ts      = int(d["entry_ts"]),
        full_qty      = Decimal(d["full_qty"]),
        sl            = float(d["sl"]),
        tp1           = float(d["tp1"]),
        tp2           = float(d["tp2"]),
        tp1_hit       = bool(d["tp1_hit"]),
        realized_pnl  = float(d["realized_pnl"]),
        exit_ts       = d.get("exit_ts"),
        exit_reason   = d.get("exit_reason", ""),
        best_price    = d.get("best_price"),
        trail_distance = d.get("trail_distance"),
    )


# ── StateStore ────────────────────────────────────────────────────────────
class StateStore:
    """
    Thin JSON persistence layer for one strategy's open trade state.

    Instantiated once per strategy, not shared.
    """

    def __init__(self, strategy_id: str, state_dir: str = "state") -> None:
        self._state_dir = Path(state_dir)
        # Normalise strategy_id to a safe filename: "MS-001" → "ms-001"
        safe_id = strategy_id.lower().replace(" ", "_")
        self._path = self._state_dir / f"{safe_id}_state.json"

    @property
    def path(self) -> Path:
        return self._path

    # ── Save ──────────────────────────────────────────────────────────────
    def save(self, ledger: TradeLedger) -> None:
        """
        Persist the current open trades and next_id to disk.
        Called from the strategy's on_stop().
        If there are no open trades the file is deleted (clean state).
        """
        self._state_dir.mkdir(parents=True, exist_ok=True)

        if not ledger.open_trades:
            # Clean shutdown with no open positions — remove stale file
            if self._path.exists():
                self._path.unlink()
                log.info("StateStore: clean shutdown — removed %s", self._path)
            else:
                log.info("StateStore: no open trades, nothing to save.")
            return

        payload = {
            "saved_at":    datetime.now(timezone.utc).isoformat(),
            "next_id":     ledger.next_id,
            "open_trades": [_trade_to_dict(t) for t in ledger.open_trades],
        }

        try:
            with open(self._path, "w") as f:
                json.dump(payload, f, indent=2)
            log.info(
                "StateStore: saved %d open trade(s) → %s",
                len(ledger.open_trades), self._path,
            )
        except OSError as e:
            log.error("StateStore: failed to save state: %s", e)

    # ── Load ──────────────────────────────────────────────────────────────
    def load(self) -> Optional[tuple[list[OpenTrade], int]]:
        """
        Load persisted state from disk.

        Returns (open_trades, next_id) if a valid state file is found,
        or None if there is no file (fresh start).
        Logs a WARNING for each restored trade as a manual check reminder.
        """
        if not self._path.exists():
            log.info("StateStore: no state file found at %s — fresh start.", self._path)
            return None

        try:
            with open(self._path) as f:
                payload = json.load(f)

            open_trades = [_trade_from_dict(d) for d in payload["open_trades"]]
            next_id     = int(payload["next_id"])
            saved_at    = payload.get("saved_at", "unknown")

            log.warning(
                "StateStore: restoring %d open trade(s) saved at %s",
                len(open_trades), saved_at,
            )
            for t in open_trades:
                log.warning(
                    "  RESTORED #%05d %-5s  entry=%.1f  sl=%.1f  "
                    "tp1_hit=%s  pnl_so_far=%+.2f",
                    t.trade_id, t.side, t.entry_price,
                    t.sl, t.tp1_hit, t.realized_pnl,
                )

            return open_trades, next_id

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            log.error(
                "StateStore: corrupt state file %s (%s). "
                "Ignoring and starting fresh. "
                "⚠ Check exchange for open positions manually.",
                self._path, e,
            )
            return None

    # ── Clear ─────────────────────────────────────────────────────────────
    def clear(self) -> None:
        """
        Delete the state file.
        Useful in tests or after a manual position close.
        """
        if self._path.exists():
            self._path.unlink()
            log.info("StateStore: cleared %s", self._path)
