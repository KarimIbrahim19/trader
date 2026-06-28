# Notes for Manager — Stage 4 code review

## 1. `config_breakeven` as static method

`telegram_actor.py:287` calls `self.config_breakeven(trade)`. The method is defined as `@staticmethod` at line 296 but called via instance — which works in Python but is inconsistent. The only real redundancy is that `trade.tp1_hit` is checked both in the caller's `if trade.tp1_hit and self.config_breakeven(trade)` (line 287) and again inside the method body (line 299). Harmless, but could be either:

- **Option A**: Inline the check and drop the method
- **Option B**: Make it a regular method
- **Option C**: Leave as-is (it works)

I recommended leaving it, but wanted a second opinion.

## 2. Exit price back-calculation

`telegram_actor.py:328`:
```python
entry_price + (total / full_qty) * (1 if LONG else -1)
```

I initially flagged this as misleading for multi-leg trades (TP1 + close). On re-review, for a 50%/50% TP1/TP2 split this correctly computes `(tp1 + tp2) / 2` — the volume-weighted average exit price. This is correct and informative. No change needed.

## 3. Custom events not used

`system_structure.md` recommends NT custom events (`SignalEvent`, `TradeOpenedEvent`, etc.) but the implementation uses direct method calls on `TelegramNotifier`. This is the right call — simpler, faster, avoids NT dependency. The `events/` directory is empty but that's fine unless we add another notification channel later.
