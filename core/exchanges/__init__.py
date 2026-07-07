"""
core/exchanges/__init__.py
────────────────────────────────────────────────────────────────────────
Registry mapping venue name (lowercase, as used in settings.yaml's
`venues:` / `symbols:` blocks and each strategy's `venue:` field) to an
adapter instance implementing core.exchanges.base.ExchangeAdapter.

Adding a new exchange = write core/exchanges/<name>.py + add one line
here. No changes needed in core/config.py, core/node_builder.py,
main.py, scripts/check_infra.py, or strategy code.
"""

from __future__ import annotations

from core.exchanges.binance import BinanceAdapter

ADAPTERS: dict = {
    "binance": BinanceAdapter(),
}


def get_adapter(venue: str):
    adapter = ADAPTERS.get(venue.lower())
    if adapter is None:
        raise ValueError(
            f"Unknown venue '{venue}'. Available: {list(ADAPTERS)}. "
            "Add an adapter module in core/exchanges/ and register it in "
            "core/exchanges/__init__.py's ADAPTERS dict."
        )
    return adapter
