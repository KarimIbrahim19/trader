"""
risk/reconciler.py
────────────────────────────────────────────────────────────────────────
Stage 6 — Cross-strategy ledger reconciliation.

Multi-exchange refactor: the reconciler used to assume ONE shared
NETTING position for the entire process (one symbol, one venue). That
assumption breaks once strategies can trade different symbols and/or
different venues -- each (venue, instrument) pair has its own
independent exchange position, so each needs its own expected-vs-actual
comparison, its own grace period, and its own halt flag.

Every public method now takes (venue, instrument_id) and looks up (or
lazily creates) a private `_Group` for that pair. Nothing outside this
file needs to know about the grouping internals -- callers just always
pass their own strategy's (venue, instrument_id).

Design decisions (unchanged from the original Stage 6 review):
  • Case A (exchange < expected):  WARNING + Telegram only.
    Something closed externally (liquidation, manual, ADL) -- or, as
    reconcile_case_a_analysis.md (2026-07-04) documented, an internal
    ledger bug. No auto-correction here — see docs/stage6_reply.md for
    the planned event-driven self-healing redesign.

  • Case B (exchange > expected):  HALT new entries for that
    (venue, instrument) group + CRITICAL alert. Untracked position on
    exchange — unknown risk exposure. Only strategies sharing that
    exact group halt; other groups are unaffected.
    Requires manual restart of that group after resolving.

  • Grace period: skip a group's check if any ledger mutation for that
    group happened within the last `grace_secs` seconds. Prevents false
    positives during the 100–500ms window between order submission and
    exchange confirm.

  • Bar-aligned: called at the top of on_bar(), before signal and
    SL/TP logic. By bar time all fills from the previous bar are settled.

  • Per-trade ledger kept: required for FIFO attribution (Stage 7),
    backtest parity, and granular Telegram notifications.

  • Dry-run mode: reconciler is created but immediately skips all
    checks (no exec client → no portfolio → no position data).

Architecture:
  LedgerReconciler is a shared singleton created in main.py and
  injected into each strategy via set_reconciler(). Each strategy
  registers its ledger and portfolio_fn in on_start(), keyed by its own
  (venue, instrument_id). check() reads combined exposure from all
  ledgers registered under that same group and compares to that
  group's NT portfolio position.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from risk.trade_ledger import TradeLedger

log = logging.getLogger(__name__)

# One-tenth of the minimum Binance Futures order size (0.001 BTC).
# Differences smaller than this are rounding/fee artefacts. Applies to
# whichever instrument a group is tracking, not literally BTC.
_DEFAULT_TOLERANCE: float = 0.0001


@dataclass
class ReconcileResult:
    """Result of a single reconciliation check for one (venue, instrument) group."""
    checked:   bool            # False = skipped (grace period or no portfolio)
    case:      Optional[str]   # "ok" | "A" | "B" | None
    expected:  float           # expected net qty (signed: long+, short-)
    actual:    float           # exchange net qty from NT portfolio
    diff:      float           # actual - expected
    group:     str = ""        # "{venue}:{instrument_id}"
    breakdown: dict = field(default_factory=dict)
    # breakdown: {strategy_name: expected_qty} — for alert details


class _Group:
    """Per (venue, instrument) reconciliation state."""

    def __init__(self) -> None:
        self.ledgers:          dict[str, TradeLedger] = {}
        self.portfolio_fn:     Optional[Callable[[], Optional[float]]] = None
        self.last_mutation_ns: int  = 0
        self.is_halted:        bool = False
        self.halt_notified:    bool = False


class LedgerReconciler:
    """
    Compares aggregate ledger exposure to the exchange NETTING position,
    independently per (venue, instrument) group. Strategies trading
    different symbols or venues never affect each other's checks.
    """

    def __init__(
        self,
        grace_secs:    float = 15.0,
        tolerance_btc: float = _DEFAULT_TOLERANCE,
        notifier:      object = None,
    ) -> None:
        self._groups:    dict[str, _Group] = {}
        self._grace_ns:  int   = int(grace_secs * 1e9)
        self._tolerance: float = tolerance_btc
        self._notifier         = notifier

    @staticmethod
    def _key(venue: str, instrument_id) -> str:
        return f"{venue.lower()}:{instrument_id}"

    def _get_or_create(self, venue: str, instrument_id) -> _Group:
        key = self._key(venue, instrument_id)
        group = self._groups.get(key)
        if group is None:
            group = _Group()
            self._groups[key] = group
        return group

    # ── Registration ──────────────────────────────────────────────────────
    def register_strategy(self, name: str, ledger: TradeLedger, venue: str, instrument_id) -> None:
        """
        Called from BaseSmcStrategy.on_start() for each enabled strategy.
        Aggregates exposure across all ledgers sharing this strategy's
        (venue, instrument_id) group.
        """
        group = self._get_or_create(venue, instrument_id)
        group.ledgers[name] = ledger
        log.debug(
            "Reconciler: registered strategy '%s'  group=%s", name, self._key(venue, instrument_id),
        )

    def set_portfolio_fn(
        self, venue: str, instrument_id, fn: Callable[[], Optional[float]],
    ) -> None:
        """
        Set the callable that reads this group's net position from NT's
        portfolio. Only the first call per group takes effect -- all
        strategies sharing a (venue, instrument) pair share one NT
        portfolio position, so only one reader is needed per group.
        """
        group = self._get_or_create(venue, instrument_id)
        if group.portfolio_fn is None:
            group.portfolio_fn = fn

    # ── Mutation tracking ─────────────────────────────────────────────────
    def record_mutation(self, venue: str, instrument_id, ts_ns: int) -> None:
        """
        Called by BaseSmcStrategy after any trade is opened or closed for
        its (venue, instrument) group. Starts that group's grace period.
        """
        group = self._get_or_create(venue, instrument_id)
        if ts_ns > group.last_mutation_ns:
            group.last_mutation_ns = ts_ns

    # ── Halt control ──────────────────────────────────────────────────────
    def is_halted(self, venue: str, instrument_id) -> bool:
        """
        True when Case B was detected for this (venue, instrument) group.
        Strategies in that group gate new entries on this; other groups
        are unaffected. Only cleared by manual restart.
        """
        key = self._key(venue, instrument_id)
        group = self._groups.get(key)
        return group.is_halted if group is not None else False

    # ── Main check ───────────────────────────────────────────────────────
    def check(self, venue: str, instrument_id, ts_ns: int, strategy_log: logging.Logger) -> ReconcileResult:
        """
        Run one reconciliation check for the (venue, instrument) group of
        the calling strategy. Called at the top of on_bar() from every
        strategy, before signal/SL/TP logic.
        """
        key = self._key(venue, instrument_id)
        _SKIP = ReconcileResult(
            checked=False, case=None,
            expected=0.0, actual=0.0, diff=0.0, group=key,
        )

        group = self._groups.get(key)

        # No group / no portfolio fn = dry_run mode or not yet registered — skip
        if group is None or group.portfolio_fn is None:
            return _SKIP

        # Within grace period of last mutation for this group — skip
        if (
            group.last_mutation_ns > 0
            and (ts_ns - group.last_mutation_ns) < self._grace_ns
        ):
            return _SKIP

        # Read exchange position for this group
        actual = group.portfolio_fn()
        if actual is None:
            return _SKIP    # portfolio not ready yet

        # Compute expected net across all ledgers in this group (signed: long+, short-)
        breakdown: dict[str, float] = {}
        expected = 0.0
        for name, ledger in group.ledgers.items():
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
                group=key, breakdown=breakdown,
            )

        result = ReconcileResult(
            checked=True,
            case="A" if diff < 0 else "B",
            expected=expected, actual=actual, diff=diff,
            group=key, breakdown=breakdown,
        )
        self._handle_mismatch(group, result, strategy_log)
        return result

    # ── Internal handlers ─────────────────────────────────────────────────
    def _handle_mismatch(
        self, group: _Group, result: ReconcileResult, log: logging.Logger
    ) -> None:
        breakdown_str = "  ".join(
            f"{k.upper()}={v:+.4f}" for k, v in result.breakdown.items()
        )

        if result.case == "A":
            # ── Case A: exchange < expected ───────────────────────────
            # Something closed externally, OR an internal ledger bug
            # (see reconcile_case_a_analysis.md). Warn only for now.
            log.warning(
                f"RECONCILE CASE A  group={result.group}  "
                f"exchange={result.actual:+.4f}  "
                f"expected={result.expected:+.4f}  "
                f"diff={result.diff:+.4f}  "
                f"[{breakdown_str}]  "
                f"Possible external close OR internal ledger desync. "
                f"No auto-correction applied — review manually."
            )
            self._notify("on_reconcile_warning", result)

        else:
            # ── Case B: exchange > expected ───────────────────────────
            # Untracked position. Halt new entries for this group only.
            log.error(
                f"RECONCILE CASE B  group={result.group}  "
                f"exchange={result.actual:+.4f}  "
                f"expected={result.expected:+.4f}  "
                f"diff={result.diff:+.4f}  "
                f"[{breakdown_str}]  "
                f"UNTRACKED POSITION — halting new entries for this group."
            )
            group.is_halted = True
            if not group.halt_notified:
                group.halt_notified = True
                self._notify("on_reconcile_halt", result)

    def _notify(self, method: str, result: ReconcileResult) -> None:
        if self._notifier is None:
            return
        fn = getattr(self._notifier, method, None)
        if fn is None:
            return
        try:
            fn(result.expected, result.actual, result.diff, result.breakdown, result.group)
        except TypeError:
            # Back-compat: notifier not yet updated to accept `group`.
            try:
                fn(result.expected, result.actual, result.diff, result.breakdown)
            except Exception as e:
                log.warning("Reconciler notifier %s error: %s", method, e)
        except Exception as e:
            log.warning("Reconciler notifier %s error: %s", method, e)
