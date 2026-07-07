"""
core/exchanges/binance.py
────────────────────────────────────────────────────────────────────────
Binance USDT-M Futures adapter. All Binance-specific NT wiring lives
here: client config construction, testnet URL routing, the v1.228
leverage-init monkey-patch (see docs/binance_leverage_init_bug.md), and
the HTTP calls used for exchange-filter lookups and API-key validation
(previously duplicated between strategies/base_smc_strategy.py and
scripts/check_infra.py).

This module used to be core/node_builder.py's entire contents when the
system only ever talked to one exchange. It was extracted unchanged in
behavior as part of the multi-exchange refactor -- see
docs/multi_exchange_architecture.md.

To add a new exchange: copy this file's shape, implement the
ExchangeAdapter interface (core/exchanges/base.py) for the new venue,
then add one line to core/exchanges/__init__.py.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Optional, Tuple

from nautilus_trader.adapters.binance.common.enums import BinanceAccountType
from nautilus_trader.adapters.binance.common.symbol import BinanceSymbol
from nautilus_trader.adapters.binance.config import (
    BinanceDataClientConfig,
    BinanceExecClientConfig,
)
from nautilus_trader.adapters.binance.factories import (
    BinanceLiveDataClientFactory,
    BinanceLiveExecClientFactory,
)
from nautilus_trader.adapters.binance.futures.enums import BinanceFuturesMarginType
from nautilus_trader.config import InstrumentProviderConfig

logger = logging.getLogger(__name__)

_TESTNET_HTTP       = "https://testnet.binancefuture.com"
_TESTNET_WS_MARKET  = "wss://fstream.binancefuture.com/market"
_TESTNET_WS_PRIVATE = "wss://fstream.binancefuture.com/private"
_TESTNET_WS_API     = "wss://testnet.binancefuture.com/ws-fapi/v1"

_LIVE_REST = "https://fapi.binance.com"


# ── Monkey-patch: Binance Futures leverage init crash (NT v1.228) ──────
#
# BinanceFuturesExecClient._update_account_state() calls
# query_futures_symbol_config() with no symbol filter, then iterates ALL
# returned symbols calling account.set_leverage(). If any symbol has
# leverage=0 (common on testnet for unused symbols), set_leverage() raises
# ValueError("leverage was not >= 1"). The except KeyError handler is
# dead code (_get_cached_instrument_id() never raises), so the
# ValueError propagates and kills _connect().
#
# Fixed upstream in NT v1.229.0 (PR #4289). Remove this block after
# upgrading (see docs/binance_leverage_init_bug.md and PROJECT_v2.md §9).
import nautilus_trader.adapters.binance.futures.execution as _futures_exec

_orig_update_account_state = _futures_exec.BinanceFuturesExecutionClient._update_account_state


async def _patched_update_account_state(self):
    try:
        await _orig_update_account_state(self)
    except ValueError as e:
        if "leverage was not >= 1" in str(e):
            logger.warning(
                "Ignored invalid leverage during connect (testnet symbol config quirk): %s", e,
            )
        else:
            raise


_futures_exec.BinanceFuturesExecutionClient._update_account_state = _patched_update_account_state


class BinanceAdapter:
    """USDT-M Futures adapter. See core/exchanges/base.py for the interface."""

    venue_name = "BINANCE"

    # ── NT client configs ────────────────────────────────────────────────
    def build_data_client_cfg(self, creds: Tuple[str, str], is_paper: bool):
        api_key, api_secret = creds
        return BinanceDataClientConfig(
            api_key             = api_key or "",
            api_secret          = api_secret or "",
            account_type        = BinanceAccountType.USDT_FUTURES,
            base_url_http       = _TESTNET_HTTP      if is_paper else None,
            base_url_ws         = _TESTNET_WS_MARKET if is_paper else None,
            instrument_provider = InstrumentProviderConfig(load_all=True),
        )

    def build_exec_client_cfg(self, creds: Tuple[str, str], is_paper: bool, symbol_settings: dict):
        api_key, api_secret = creds

        leverages: dict = {}
        margin_types: dict = {}
        for symbol, sym_cfg in symbol_settings.items():
            bsym = BinanceSymbol(symbol)
            leverages[bsym] = sym_cfg.leverage
            if sym_cfg.margin_type is not None:
                margin_types[bsym] = BinanceFuturesMarginType(sym_cfg.margin_type)

        return BinanceExecClientConfig(
            api_key             = api_key,
            api_secret          = api_secret,
            account_type        = BinanceAccountType.USDT_FUTURES,
            base_url_http       = _TESTNET_HTTP       if is_paper else None,
            base_url_ws         = _TESTNET_WS_API     if is_paper else None,  # WS API (session.logon) — not used in HMAC mode
            base_url_ws_stream  = _TESTNET_WS_PRIVATE if is_paper else None,  # user data stream (ACCOUNT_UPDATE, etc.)
            instrument_provider = InstrumentProviderConfig(load_all=True),
            futures_leverages    = leverages,
            futures_margin_types = margin_types or None,
        )

    def data_client_factory(self) -> type:
        return BinanceLiveDataClientFactory

    def exec_client_factory(self) -> type:
        return BinanceLiveExecClientFactory

    # ── HTTP helpers (used by BaseSmcStrategy + scripts/check_infra.py) ───
    def fetch_exchange_filters(
        self, symbol: str, is_paper: bool,
    ) -> Optional[Tuple[Decimal, float]]:
        base_url = _TESTNET_HTTP if is_paper else _LIVE_REST
        try:
            url = f"{base_url}/fapi/v1/exchangeInfo"
            with urllib.request.urlopen(url, timeout=3) as resp:
                data = json.loads(resp.read())
            sym_info = next(s for s in data["symbols"] if s["symbol"] == symbol)
            filters = {f["filterType"]: f for f in sym_info["filters"]}
            mls = filters.get("MARKET_LOT_SIZE", {})
            mn  = filters.get("MIN_NOTIONAL", {})
            min_qty      = Decimal(mls.get("minQty", "0.001"))
            min_notional = float(mn.get("notional", "50"))
            return min_qty, min_notional
        except Exception as e:
            logger.warning("Binance exchangeInfo fetch failed for %s: %s", symbol, e)
            return None

    def connectivity_endpoints(self) -> list:
        return [
            ("fapi.binance.com",    443, "Futures REST API"),
            ("fstream.binance.com", 443, "Futures WebSocket"),
        ]

    def validate_api_key(self, creds: Tuple[str, str], is_paper: bool) -> dict:
        key, secret = creds
        if not key or not secret:
            return {"ok": False, "detail": "API key/secret not set"}

        base  = _TESTNET_HTTP if is_paper else _LIVE_REST
        label = "testnet" if is_paper else "live"
        ts     = int(time.time() * 1000)
        params = f"timestamp={ts}"
        signature = hmac.new(secret.encode(), params.encode(), hashlib.sha256).hexdigest()
        url = f"{base}/fapi/v2/account?{params}&signature={signature}"
        req = urllib.request.Request(url, headers={"X-MBX-APIKEY": key})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            balance = next(
                (float(a["walletBalance"]) for a in data.get("assets", []) if a["asset"] == "USDT"),
                None,
            )
            detail = f"API key valid ({label})"
            if balance is not None:
                detail += f" — USDT wallet balance: {balance:,.2f}"
            return {"ok": True, "detail": detail}
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return {"ok": False, "detail": "API key rejected (401) — check key and secret"}
            if e.code == 403:
                return {"ok": False, "detail": "API key lacks Futures permission"}
            return {"ok": False, "detail": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"ok": False, "detail": f"API key check failed: {e}"}
