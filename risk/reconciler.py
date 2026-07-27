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

Hedge mode support: a group's portfolio_fn (set via set_portfolio_fn)
can return EITHER a plain float (netting -- one signed blended
position) OR a dict {"LONG": qty, "SHORT": qty} (hedge -- two
independent exchange slots). check() detects which shape it got and
branches accordingly; callers never need to know or declare the mode
up front. Under hedge, LONG and SHORT are compared completely
independently -- this also closes a real blind spot netting mode had:
two unrelated positions on opposite sides could sum to a "correct"
aggregate while being individually wrong (see reconcile_case_a_analysis.md
and the 2026-07 MS/FVG incident this was built to prevent). Hedge mode's
per-side comparison can never have that blind spot, since there's no
cross-side cancellation to hide behind. is_halted() takes an optional
`side` for hedge groups -- a halt on one side only blocks new entries on
that side; the other keeps trading normally.

Design decisions (unchanged from the original Stage 6 review):
  • Case A (exchange < expected):  WARNING + Telegram only.
    Something closed externally (liquidation, manual, ADL) -- or, as
    reconcile_case_a_analysis.md (2026-07-04) documented, an internal
    ledger bug. No auto-correction here — see docs/stage6_reply.md for
    the planned event-driven self-healing redesign.

  • Case B (exchange > expected):  HALT new entries for that
    (venue, instrument[, side]) group + CRITICAL alert. Untracked
    position on exchange — unknown risk exposure. Only strategies
    sharing that exact group (and, in hedge mode, that exact side)
    halt; other groups/sides are unaffected.
    Requires manual restart of that group after resolving.

  • Grace period: skip a group's check if any ledger mutation for that
    group happened within the last `grace_secs` seconds. Prevents false
    positives during the 100–500ms window between order submission and
    exchange confirm. Shared across both sides in hedge mode (one
    mutation timestamp per group, not per side) -- simpler, and a
    mutation on either side is a reasonable signal to wait out for both.

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
  group's NT portfolio position (or, in hedge mode, positions).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional, Union

from risk.trade_ledger import TradeLedger

log = logging.getLogger(__name__)

# One-tenth of the minimum Binance Futures order size (0.001 BTC).
# Differences smaller than this are rounding/fee artefacts. Applies to
# whichever instrument a group is tracking, not literally BTC.
_DEFAULT_TOLERANCE: float = 0.0001

_SIDES = ("LONG", "SHORT")

PortfolioValue = Union[float, dict]   # float = netting, dict = hedge {"LONG":.., "SHORT":..}


@dataclass
class SideResult:
    """Per-side breakdown of a hedge-mode reconciliation check."""
    case:     str    # "ok" | "A" | "B"
    expected: float
    actual:   float
    diff:     float


@dataclass
class ReconcileResult:
    """
    Result of a single reconciliation check for one (venue, instrument) group.

    Netting: `case`/`expected`/`actual`/`diff` describe the one blended
    position directly; `sides` is None.

    Hedge: `sides` holds a per-side SideResult for "LONG" and "SHORT".
    `case` is "ok" only if both sides are ok, otherwise the worse of the
    two ("B" if either side is B, else "A"). `expected`/`actual`/`diff`
    are the sums across both sides -- informational only; the real
    per-side numbers are in `sides`.
    """
    checked:   bool            # False = skipped (grace period or no portfolio)
    case:      Optional[str]   # "ok" | "A" | "B" | None
    expected:  float
    actual:    float
    diff:      float
    group:     str = ""        # "{venue}:{instrument_id}"
    breakdown: dict = field(default_factory=dict)
    # breakdown: netting -> {strategy_name: expected_qty}
    #            hedge   -> {strategy_name: {"LONG": qty, "SHORT": qty}}
    sides: Optional[dict] = None   # hedge only: {"LONG": SideResult, "SHORT": SideResult}


class _Group:
    """Per (venue, instrument) reconciliation state."""

    def __init__(self) -> None:
        self.ledgers:          dict[str, TradeLedger] = {}
        self.portfolio_fn:     Optional[Callable[[], Optional[PortfolioValue]]] = None
        self.last_mutation_ns: int  = 0
        # Netting halt state
        self.is_halted:        bool = False
        self.halt_notified:    bool = False
        # Hedge halt state -- independent per side
        self.is_halted_long:      bool = False
        self.is_halted_short:     bool = False
        self.halt_notified_long:  bool = False
        self.halt_notified_short: bool = False


class LedgerReconciler:
    """
    Compares aggregate ledger exposure to the exchange position(s),
    independently per (venue, instrument) group. Strategies trading
    different symbols or venues never affect each other's checks.
    Within a group, netting mode compares one blended position; hedge
    mode compares LONG and SHORT independently (see module docstring).
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
        self, venue: str, instrument_id, fn: Callable[[], Optional[PortfolioValue]],
    ) -> None:
        """
        Set the callable that reads this group's exchange position(s).
        Returns a float for netting groups, a {"LONG":.., "SHORT":..}
        dict for hedge groups -- check() branches on the shape at read
        time. Only the first call per group takes effect -- all
        strategies sharing a (venue, instrument) pair share the same
        exchange position(s), so only one reader is needed per group.
        """
        group = self._get_or_create(venue, instrument_id)
        if group.portfolio_fn is None:
            group.portfolio_fn = fn

    # ── Mutation tracking ─────────────────────────────────────────────────
    def record_mutation(self, venue: str, instrument_id, ts_ns: int) -> None:
        """
        Called by BaseSmcStrategy after any trade is opened or closed for
        its (venue, instrument) group. Starts that group's grace period
        (shared across both sides in hedge mode).
        """
        group = self._get_or_create(venue, instrument_id)
        if ts_ns > group.last_mutation_ns:
            group.last_mutation_ns = ts_ns

    # ── Halt control ──────────────────────────────────────────────────────
    def is_halted(self, venue: str, instrument_id, side: Optional[str] = None) -> bool:
        """
        True when Case B was detected for this (venue, instrument) group.
        Strategies in that group gate new entries on this; other groups
        are unaffected. Only cleared by manual restart.

        `side`: None for netting groups (whole-group halt, as before).
        "LONG"/"SHORT" for hedge groups -- only that side's halt flag is
        checked, so a Case B on SHORT doesn't block LONG entries.
        """
        key = self._key(venue, instrument_id)
        group = self._groups.get(key)
        if group is None:
            return False
        if side == "LONG":
            return group.is_halted_long
        if side == "SHORT":
            return group.is_halted_short
        return group.is_halted

    # ── Main check ───────────────────────────────────────────────────────
    def check(self, venue: str, instrument_id, ts_ns: int, strategy_log: logging.Logger) -> ReconcileResult:
        """
        Run one reconciliation check for the (venue, instrument) group of
        the calling strategy. Called at the top of on_bar() from every
        strategy, before signal/SL/TP logic. Branches to netting- or
        hedge-mode comparison based on the shape portfolio_fn() returns.
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

        # Read exchange position(s) for this group
        actual = group.portfolio_fn()
        if actual is None:
            return _SKIP    # portfolio/cache not ready yet

        if isinstance(actual, dict):
            return self._check_hedge(group, key, actual, strategy_log)
        return self._check_netting(group, key, actual, strategy_log)

    # ── Netting comparison (unchanged from original Stage 6 logic) ────────
    def _check_netting(self, group: _Group, key: str, actual: float, log_: logging.Logger) -> ReconcileResult:
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
        self._handle_mismatch(group, result, log_)
        return result

    # ── Hedge comparison (LONG and SHORT compared independently) ──────────
    def _check_hedge(self, group: _Group, key: str, actual: dict, log_: logging.Logger) -> ReconcileResult:
        breakdown: dict[str, dict] = {}
        expected = {"LONG": 0.0, "SHORT": 0.0}
        for name, ledger in group.ledgers.items():
            side_qty = {"LONG": 0.0, "SHORT": 0.0}
            for t in ledger.open_trades:
                remaining = float(t.full_qty) * (0.5 if t.tp1_hit else 1.0)
                side_qty[t.side] += remaining
            breakdown[name] = side_qty
            expected["LONG"]  += side_qty["LONG"]
            expected["SHORT"] += side_qty["SHORT"]

        sides: dict[str, SideResult] = {}
        worst = "ok"
        for side in _SIDES:
            exp  = expected[side]
            act  = float(actual.get(side, 0.0))
            diff = act - exp
            if abs(diff) <= self._tolerance:
                case = "ok"
            else:
                case = "A" if diff < 0 else "B"
            sides[side] = SideResult(case=case, expected=exp, actual=act, diff=diff)
            if case == "B":
                worst = "B"
            elif case == "A" and worst != "B":
                worst = "A"

        total_expected = expected["LONG"] + expected["SHORT"]
        total_actual   = float(actual.get("LONG", 0.0)) + float(actual.get("SHORT", 0.0))
        result = ReconcileResult(
            checked=True,
            case=worst,
            expected=total_expected,
            actual=total_actual,
            diff=total_actual - total_expected,
            group=key,
            breakdown=breakdown,
            sides={s: r for s, r in sides.items()},
        )

        for side, side_result in sides.items():
            if side_result.case in ("A", "B"):
                side_breakdown = {name: b[side] for name, b in breakdown.items()}
                self._handle_mismatch_side(group, key, side, side_result, side_breakdown, log_)

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
            self._notify("on_reconcile_warning", result.expected, result.actual, result.diff, result.breakdown, result.group)

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
                self._notify("on_reconcile_halt", result.expected, result.actual, result.diff, result.breakdown, result.group)

    def _handle_mismatch_side(
        self, group: _Group, key: str, side: str, side_result: SideResult,
        side_breakdown: dict, log: logging.Logger,
    ) -> None:
        label = f"{key}:{side}"
        breakdown_str = "  ".join(
            f"{k.upper()}={v:+.4f}" for k, v in side_breakdown.items()
        )

        if side_result.case == "A":
            log.warning(
                f"RECONCILE CASE A  group={label}  "
                f"exchange={side_result.actual:+.4f}  "
                f"expected={side_result.expected:+.4f}  "
                f"diff={side_result.diff:+.4f}  "
                f"[{breakdown_str}]  "
                f"Possible external close OR internal ledger desync ({side} slot). "
                f"No auto-correction applied — review manually."
            )
            self._notify(
                "on_reconcile_warning",
                side_result.expected, side_result.actual, side_result.diff, side_breakdown, label,
            )
        else:
            log.error(
                f"RECONCILE CASE B  group={label}  "
                f"exchange={side_result.actual:+.4f}  "
                f"expected={side_result.expected:+.4f}  "
                f"diff={side_result.diff:+.4f}  "
                f"[{breakdown_str}]  "
                f"UNTRACKED POSITION ({side} slot) — halting new {side} entries for this group."
            )
            if side == "LONG":
                group.is_halted_long = True
                already_notified = group.halt_notified_long
                group.halt_notified_long = True
            else:
                group.is_halted_short = True
                already_notified = group.halt_notified_short
                group.halt_notified_short = True
            if not already_notified:
                self._notify(
                    "on_reconcile_halt",
                    side_result.expected, side_result.actual, side_result.diff, side_breakdown, label,
                )

    def _notify(
        self, method: str, expected: float, actual: float, diff: float,
        breakdown: dict, group_label: str,
    ) -> None:
        if self._notifier is None:
            return
        fn = getattr(self._notifier, method, None)
        if fn is None:
            return
        try:
            fn(expected, actual, diff, breakdown, group_label)
        except TypeError:
            # Back-compat: notifier not yet updated to accept `group`.
            try:
                fn(expected, actual, diff, breakdown)
            except Exception as e:
                log.warning("Reconciler notifier %s error: %s", method, e)
        except Exception as e:
            log.warning("Reconciler notifier %s error: %s", method, e)
