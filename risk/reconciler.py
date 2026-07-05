"""
risk/reconciler.py
────────────────────────────────────────────────────────────────────────
Stage 6 — Cross-strategy ledger reconciliation.

Design decisions (finalised after review):
  • Case A (exchange < expected):  WARNING + Telegram only.
    Something closed externally (liquidation, manual, ADL).
    No auto-correction — learn from every mismatch first.
    Auto-heal deferred to Stage 7 if mismatches prove safe.

  • Case B (exchange > expected):  HALT new entries + CRITICAL alert.
    Untracked position on exchange — unknown risk exposure.
    All strategies halt (NETTING can't isolate which one).
    Requires manual restart after resolving.

  • Grace period: skip check if any ledger mutation happened within
    the last `grace_secs` seconds. Prevents false positives during
    the 100–500ms window between order submission and exchange confirm.

  • Bar-aligned: called at the top of on_bar(), before signal and
    SL/TP logic. By bar time all fills from the previous bar are settled.

  • Per-trade ledger kept: required for FIFO attribution (Stage 7),
    backtest parity, and granular Telegram notifications.

  • Dry-run mode: reconciler is created but immediately skips all
    checks (no exec client → no portfolio → no position data).

Architecture:
  LedgerReconciler is a shared singleton created in main.py and
  injected into each strategy via set_reconciler(). Each strategy
  registers its ledger and portfolio_fn in on_start(). The check()
  method reads combined exposure from all registered ledgers and
  compares to the single NT portfolio position (NETTING model).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from risk.trade_ledger import TradeLedger

log = logging.getLogger(__name__)

# One-tenth of the minimum Binance Futures order size (0.001 BTC).
# Differences smaller than this are rounding/fee artefacts.
_DEFAULT_TOLERANCE_BTC: float = 0.0001


@dataclass
class ReconcileResult:
    """Result of a single reconciliation check."""
    checked:   bool            # False = skipped (grace period or no portfolio)
    case:      Optional[str]   # "ok" | "A" | "B" | None
    expected:  float           # expected net BTC (signed: long+, short-)
    actual:    float           # exchange net BTC from NT portfolio
    diff:      float           # actual - expected
    breakdown: dict[str, float] = field(default_factory=dict)
    # breakdown: {strategy_name: expected_qty} — for alert details


class LedgerReconciler:
    """
    Compares aggregate ledger exposure across ALL strategies to the
    single Binance NETTING position at the start of each primary bar.
    """

    def __init__(
        self,
        grace_secs:    float = 15.0,
        tolerance_btc: float = _DEFAULT_TOLERANCE_BTC,
        notifier:      object = None,
    ) -> None:
        self._ledgers:          dict[str, TradeLedger]             = {}
        self._portfolio_fn:     Optional[Callable[[], Optional[float]]] = None
        self._grace_ns:         int   = int(grace_secs * 1e9)
        self._tolerance:        float = tolerance_btc
        self._notifier               = notifier
        self._last_mutation_ns: int   = 0
        self._is_halted:        bool  = False
        self._halt_notified:    bool  = False   # send Case B alert once only

    # ── Registration ──────────────────────────────────────────────────────
    def register_strategy(self, name: str, ledger: TradeLedger) -> None:
        """
        Called from BaseSmcStrategy.on_start() for each enabled strategy.
        The reconciler aggregates exposure across all registered ledgers.
        """
        self._ledgers[name] = ledger
        log.debug("Reconciler: registered strategy '%s'", name)

    def set_portfolio_fn(self, fn: Callable[[], Optional[float]]) -> None:
        """
        Set the callable that reads the net position from NT's portfolio.
        Only the first call takes effect — all strategies share the same
        NT portfolio so only one reader is needed.
        """
        if self._portfolio_fn is None:
            self._portfolio_fn = fn

    # ── Mutation tracking ─────────────────────────────────────────────────
    def record_mutation(self, ts_ns: int) -> None:
        """
        Called by BaseSmcStrategy after any trade is opened or closed
        (i.e., after pm.flush_state() returns True). Starts the grace
        period to prevent false positives on the next bar(s).
        """
        if ts_ns > self._last_mutation_ns:
            self._last_mutation_ns = ts_ns

    # ── Halt control ──────────────────────────────────────────────────────
    @property
    def is_halted(self) -> bool:
        """
        True when Case B was detected. All strategies gate new entries
        on this flag in on_bar(). Only cleared by manual restart.
        """
        return self._is_halted

    # ── Main check ───────────────────────────────────────────────────────
    def check(self, ts_ns: int, strategy_log: logging.Logger) -> ReconcileResult:
        """
        Run one reconciliation check. Called at the top of on_bar() from
        every strategy, before signal/SL/TP logic.

        Returns a ReconcileResult describing what was found.
        The caller (BaseSmcStrategy) uses is_halted to suppress entries.
        """
        _SKIP = ReconcileResult(
            checked=False, case=None,
            expected=0.0, actual=0.0, diff=0.0,
        )

        # No portfolio fn = dry_run mode or not yet initialised — skip silently
        if self._portfolio_fn is None:
            return _SKIP

        # Within grace period of last mutation — skip to avoid false positives
        if (
            self._last_mutation_ns > 0
            and (ts_ns - self._last_mutation_ns) < self._grace_ns
        ):
            return _SKIP

        # Read exchange position
        actual = self._portfolio_fn()
        if actual is None:
            return _SKIP    # portfolio not ready yet

        # Compute expected net across all ledgers (signed: long+, short-)
        breakdown: dict[str, float] = {}
        expected = 0.0
        for name, ledger in self._ledgers.items():
            strat_qty = 0.0
            for t in ledger.open_trades:
                remaining = float(t.full_qty) * (0.5 if t.tp1_hit else 1.0)
                strat_qty += remaining if t.side == "LONG" else -remaining
            breakdown[name] = strat_qty
            expected += strat_qty

        diff = actual - expected

        # Within tolerance — all good
        if abs(diff) <= self._tolerance:
            return ReconcileResult(
                checked=True, case="ok",
                expected=expected, actual=actual, diff=diff,
                breakdown=breakdown,
            )

        result = ReconcileResult(
            checked=True,
            case="A" if diff < 0 else "B",
            expected=expected, actual=actual, diff=diff,
            breakdown=breakdown,
        )
        self._handle_mismatch(result, strategy_log)
        return result

    # ── Internal handlers ─────────────────────────────────────────────────
    def _handle_mismatch(
        self, result: ReconcileResult, log: logging.Logger
    ) -> None:
        breakdown_str = "  ".join(
            f"{k.upper()}={v:+.4f}" for k, v in result.breakdown.items()
        )

        if result.case == "A":
            # ── Case A: exchange < expected ───────────────────────────
            # Something closed externally. Warn only — no auto-correct.
            log.warning(
                f"RECONCILE CASE A  "
                f"exchange={result.actual:+.4f} BTC  "
                f"expected={result.expected:+.4f} BTC  "
                f"diff={result.diff:+.4f}  "
                f"[{breakdown_str}]  "
                f"Possible external close (liquidation/manual/ADL). "
                f"No auto-correction applied — review manually."
            )
            self._notify("on_reconcile_warning",
                         result.expected, result.actual,
                         result.diff, result.breakdown)

        else:
            # ── Case B: exchange > expected ───────────────────────────
            # Untracked position. Halt new entries, alert.
            log.error(
                f"RECONCILE CASE B  "
                f"exchange={result.actual:+.4f} BTC  "
                f"expected={result.expected:+.4f} BTC  "
                f"diff={result.diff:+.4f}  "
                f"[{breakdown_str}]  "
                f"UNTRACKED POSITION — halting all new entries."
            )
            self._is_halted = True
            if not self._halt_notified:
                self._halt_notified = True
                self._notify("on_reconcile_halt",
                             result.expected, result.actual,
                             result.diff, result.breakdown)

    def _notify(self, method: str, *args) -> None:
        if self._notifier is None:
            return
        fn = getattr(self._notifier, method, None)
        if fn is None:
            return
        try:
            fn(*args)
        except Exception as e:
            log.warning("Reconciler notifier %s error: %s", method, e)
