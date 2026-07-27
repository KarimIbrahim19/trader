# Hedge Mode Implementation

**Date:** 2026-07-08 (updated 2026-07-26)
**Status:** Enabled and running. `binance`'s `position_mode` is now set to
`hedge` in `settings.yaml`. This doc supersedes
`docs/position_mode_netting_vs_hedge.md`'s original "stay on netting"
recommendation for the specific case of running more than one strategy
on the same symbol; see "Why we changed course" below.

## Why we changed course

`docs/position_mode_netting_vs_hedge.md` (earlier) recommended staying on
NETTING, accepting that multi-strategy independence wasn't free under it.
Two things changed that:

1. **A live incident (2026-07-06/07)** on `binance:BTCUSDT`, running
   `ms_btc` and `fvg_btc` concurrently with `enable_exit_signal: false`:
   `fvg_btc` flipped from SHORT to LONG. Its flip order was sized from
   *its own* ledger only (0.005 remaining short + 0.01 new long = 0.015
   BUY) — but the actual blended exchange position at that moment was
   also 0.015 short, because `ms_btc`'s unrelated 0.01 SHORT happened to
   be sitting in the same NETTING pool. The BUY flattened both. `ms_btc`'s
   ledger never learned this; its SL kept re-triggering every bar for 7
   minutes against a `reduceOnly` order Binance kept rejecting (-2022),
   since there was nothing left to reduce.
2. **The reconciler couldn't have caught it even in principle.** After
   the flip, `fvg_btc`'s ledger expected +0.01 (its new long), `ms_btc`'s
   still expected -0.01 (unaware), and the real exchange position was 0
   — the group's *sum* matched perfectly (0 == 0) while both strategies'
   individual attribution was completely wrong. A sum-based check has no
   way to see this, no matter how Case A/B thresholds are tuned.

Hedge mode's independent LONG/SHORT exchange slots close this gap
structurally rather than needing a coordination workaround layered on
top of NETTING (see chat history for the fuller options analysis before
this was decided).

A second, smaller bug surfaced during the same investigation:
`_execute_netted_flip()` didn't check `enable_exit_signal` at all — it's
a separate code path from the exit-signal handling in
`_manage_open_trades()`, so setting `enable_exit_signal: false` didn't
actually prevent flips, only ordinary opposite-signal closes. That bug
is **not fixed** by this work — it still exists in the NETTING code
path (`_is_flip_scenario`/`_execute_netted_flip` are unchanged for
netting groups). It simply never fires under hedge mode, since that
whole mechanism is skipped there by construction. If NETTING is ever
used again with multiple strategies on one symbol, that bug is still
live and should be fixed separately.

## What changed

See `CHANGELOG.md`'s 2026-07-08 entry for the full file-by-file list.
The short version: `position_mode` is configured once per venue
(`venues.<name>.position_mode`, since Binance's position mode is
account-wide — `POST /fapi/v1/positionSide/dual` applies to *every*
symbol on the account, not per symbol). Everything downstream — the
Binance exec client config, `PositionManager`'s order submission and
pending-order buffering, the strategy's position/balance queries, and
the reconciler's expected-vs-actual comparison — reads that one setting
and branches. NETTING's code path is untouched in every case (verified
with regression tests, not just left alone by assumption).

## How hedge mode changes strategy behavior

Under hedge mode, `PositionManager.on_bar()` never calls
`_is_flip_scenario()`/`_execute_netted_flip()` — LONG and SHORT are
independent exchange slots (Binance `positionSide=LONG`/`SHORT`), so
there's no flip to detect. A strategy can hold a LONG and a SHORT open
at the same time with zero conflict. Concretely, with
`enable_exit_signal: false` (the config active during the incident), a
new opposite-direction signal now just opens its own independent trade
— it does not touch the existing opposing one, which keeps running
toward its own SL/TP untouched. This is the same scenario that caused
the incident, and it's now structurally impossible to reproduce the
same way.

Every order gets tagged with `position_id` = `"{instrument_id}-LONG"`
or `"{instrument_id}-SHORT"` (via NT's `submit_order(order,
position_id=...)`), which NT's Binance adapter translates into the
`positionSide` parameter Binance requires on every order in hedge mode.
`reduce_only` is never sent in hedge mode — Binance rejects it combined
with `positionSide`, and it's unnecessary anyway since BUY/SELL against
a trade's own LONG/SHORT slot is already unambiguous.

## Reconciliation under hedge mode

`risk/reconciler.py`'s `check()` detects whether `portfolio_fn()`
returned a plain float (netting — one blended position) or a
`{"LONG": qty, "SHORT": qty}` dict (hedge), and branches. Hedge mode
compares each side's expected ledger exposure against that side's
actual exchange slot **independently** — there's no sum, so there's no
way for two unrelated positions to cancel out and hide a problem the
way they did in the incident. A Case B halt (untracked position) on one
side only blocks new entries on that side (`is_halted(venue,
instrument_id, side="LONG"|"SHORT")`); the other side keeps trading
normally.

## What did *not* change

- NETTING remains the default and is fully unaffected — same flip
  logic, same order buffering, same reconciler comparison, same
  `use_reduce_only=True`. Every change here is additive and gated on
  `position_mode`.
- `docs/stage6_reply.md`'s planned reconciler self-healing redesign is
  unrelated to this work and still pending. It would need to account
  for hedge groups' per-side structure when it's eventually built.
- The `enable_exit_signal`/`_execute_netted_flip` inconsistency
  described above, for NETTING groups specifically — not fixed here,
  and moot as long as a symbol only ever runs under hedge mode with
  multiple strategies.

## Turning it on (completed)

Hedge mode was enabled on 2026-07-11 following the steps below. The
`binance` venue now has `position_mode: hedge` in `settings.yaml` and
has been running in paper mode since activation. For reference, the
steps were:

1. Flatten every position and cancel every open order on the target
   Binance account (required — Binance rejects the mode change
   otherwise).
2. Switch the account's position mode manually (Binance's UI, or a
   one-off authenticated call to `POST /fapi/v1/positionSide/dual`).
3. Add `position_mode: hedge` under that venue in `settings.yaml`.
4. Run `python main.py --check` — the position mode check confirms
   the account's actual mode matches config before starting. `main.py`
   also runs this same check at startup and refuses to start on a
   mismatch.
5. Start in `dry_run` or `paper` first before going live.

## Known follow-ups (not addressed here)

- Cosmetic: Telegram/log messages don't yet distinguish "LONG slot" vs
  "SHORT slot" outside the reconciler's own alerts (e.g.
  `on_trade_opened` doesn't say which slot). Low priority, doesn't
  affect correctness.
- The `_execute_netted_flip`/`enable_exit_signal` bug described above
  remains open for NETTING groups.
- No automated test exists yet exercising a full hedge-mode bar
  sequence through `BaseSmcStrategy` end-to-end (only `PositionManager`
  and `LedgerReconciler` were unit-tested directly, plus the config/
  node-building pipeline) — worth adding before first live use if it
  matters to you beyond the paper-testing period.
