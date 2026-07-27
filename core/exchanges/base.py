"""
core/exchanges/base.py
────────────────────────────────────────────────────────────────────────
Common interface every exchange adapter implements.

Adding a new venue (Bybit, OKX, ...) means:
  1. Write core/exchanges/<name>.py implementing everything below.
  2. Register it in core/exchanges/__init__.py's ADAPTERS dict.
No changes required in core/node_builder.py, core/config.py, main.py,
scripts/check_infra.py, or any strategy code -- they all go through
get_adapter() and this interface.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional, Protocol, Tuple


class ExchangeAdapter(Protocol):
    """One adapter instance per exchange family (e.g. Binance)."""

    # NT venue string used as the key in TradingNodeConfig.data_clients /
    # .exec_clients (e.g. "BINANCE"). Must be unique across adapters.
    venue_name: str

    def build_data_client_cfg(self, creds: Tuple[str, str], is_paper: bool) -> Any:
        """Return the NT DataClientConfig for this venue."""
        ...

    def build_exec_client_cfg(
        self,
        creds: Tuple[str, str],
        is_paper: bool,
        symbol_settings: dict,
        position_mode: str = "netting",
    ) -> Any:
        """
        Return the NT ExecClientConfig for this venue.

        `symbol_settings` maps symbol -> core.config.SymbolSettings for
        every symbol traded on this venue, so leverage/margin type can
        be applied per symbol in one client (multiple strategies on the
        same symbol already share this).

        `position_mode` is "netting" (default) or "hedge" -- an
        account-wide setting on Binance, not per-symbol. Adapters that
        support hedge mode should adjust client config accordingly
        (e.g. Binance requires reduce-only orders to be disabled).
        """
        ...

    def verify_position_mode(
        self, creds: Tuple[str, str], is_paper: bool, expected_mode: str,
    ) -> dict:
        """
        Query the exchange's actual account-wide position mode and
        compare it to `expected_mode` ("netting" or "hedge"). Never
        changes it automatically -- switching modes on a live account
        typically requires flattening all positions and canceling all
        open orders first, so this is a read-only safety check run at
        startup. Return {"ok": bool, "detail": str}.
        """
        ...

    def data_client_factory(self) -> type:
        """NT data client factory class for node.add_data_client_factory()."""
        ...

    def exec_client_factory(self) -> type:
        """NT exec client factory class for node.add_exec_client_factory()."""
        ...

    def fetch_exchange_filters(
        self, symbol: str, is_paper: bool,
    ) -> Optional[Tuple[Decimal, float]]:
        """
        Query the exchange's live filter/min-notional info for `symbol`.
        Return (min_qty, min_notional), or None (not raise) if the fetch
        fails -- the caller falls back to the config-file values.
        """
        ...

    def connectivity_endpoints(self) -> list:
        """(host, port, label) tuples for scripts/check_infra.py's network check."""
        ...

    def validate_api_key(self, creds: Tuple[str, str], is_paper: bool) -> dict:
        """
        Make one authenticated request to prove the API key/secret work.
        Return {"ok": bool, "detail": str}.
        """
        ...
