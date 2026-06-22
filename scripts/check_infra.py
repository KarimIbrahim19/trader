"""
scripts/check_infra.py
──────────────────────────────────────────────────────────────────────
Stage 1 validation script.

Checks every infrastructure component independently before any live
trading code is wired together. Run this once after initial setup and
again after any environment change (new server, new API keys, etc.).

Usage:
    cd btc_trader
    python scripts/check_infra.py
    python scripts/check_infra.py --config config/settings.yaml
"""

import argparse
import logging
import socket
import sys
import time
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).parent.parent))


def _ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m  {msg}")


def _fail(msg: str) -> None:
    print(f"  \033[31m✗\033[0m  {msg}")


def _warn(msg: str) -> None:
    print(f"  \033[33m⚠\033[0m  {msg}")


def _section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")
    print("─" * 60)


# ── Individual checks ─────────────────────────────────────────────────
def check_config(config_path: str) -> bool:
    _section("1. Configuration")
    try:
        from core.config import load_settings
        settings = load_settings(Path(config_path).parent, Path(config_path).name)
        _ok(f"settings.yaml loaded  (mode={settings.mode})")
        _ok(f"Instrument   : {settings.instrument.nt_id}")
        _ok(f"Primary TF   : {settings.timeframes.primary}  HTF: {settings.timeframes.htf}")
        _ok(f"Strategy     : {settings.strategy.name}  htf_filter={settings.strategy.htf_filter}")
        _ok(f"Trade size   : {settings.risk.trade_size} BTC")
        _ok(f"Risk         : SL={settings.risk.sl_atr}×ATR  TP1={settings.risk.tp1_atr}×ATR  TP2={settings.risk.tp2_atr}×ATR")
        _ok(f"Max trades   : {settings.risk.max_open_trades}")
        _ok(f"Daily limit  : ${settings.risk.daily_loss_limit_usdt}")
        return True, settings
    except Exception as e:
        _fail(f"Config load failed: {e}")
        return False, None


def check_env_file(config_dir: Path) -> bool:
    _section("2. Environment / Secrets (.env)")
    env_file = config_dir / ".env"
    if not env_file.exists():
        _warn(".env file not found — secrets must be set as real environment variables")
        _warn(f"Copy config/.env.example → config/.env and fill in your keys")
        return True   # not a hard failure — keys may be real env vars

    # Load and check for placeholder values
    from dotenv import dotenv_values
    env = dotenv_values(env_file)

    placeholders = {"your_api_key_here", "your_api_secret_here",
                    "your_bot_token_here", "your_chat_id_here",
                    "your_testnet_key_here", "your_testnet_secret_here"}

    all_ok = True
    for key, value in env.items():
        if value in placeholders:
            _warn(f"{key} is still a placeholder — update config/.env")
            all_ok = False
        elif value:
            display = value[:4] + "****" + value[-4:] if len(value) > 8 else "****"
            _ok(f"{key} = {display}")
        else:
            _warn(f"{key} is empty")

    if all_ok:
        _ok(".env loaded with no placeholder values")
    return all_ok


def check_redis(settings) -> bool:
    _section("3. Redis")
    try:
        import redis
        host = settings.redis.host
        port = settings.redis.port
        r = redis.Redis(host=host, port=port,
                        socket_timeout=settings.redis.timeout_secs)
        t0 = time.time()
        pong = r.ping()
        latency = (time.time() - t0) * 1000
        if pong:
            _ok(f"Connected  {host}:{port}  latency={latency:.1f}ms")
            info = r.info("server")
            _ok(f"Redis version: {info.get('redis_version', 'unknown')}")
            return True
        else:
            _fail(f"Ping returned False from {host}:{port}")
            return False
    except ImportError:
        _fail("redis package not installed — run: pip install redis")
        return False
    except Exception as e:
        _fail(f"Cannot connect to Redis {settings.redis.host}:{settings.redis.port}: {e}")
        _warn("Install Redis: https://redis.io/docs/getting-started/installation/")
        _warn("Start Redis:  redis-server")
        return False


def check_binance_connectivity() -> bool:
    _section("4. Binance Futures API (network reachability)")
    endpoints = [
        ("fapi.binance.com", 443, "Futures REST API"),
        ("fstream.binance.com", 443, "Futures WebSocket"),
    ]
    all_ok = True
    for host, port, label in endpoints:
        try:
            t0 = time.time()
            sock = socket.create_connection((host, port), timeout=10)
            latency = (time.time() - t0) * 1000
            sock.close()
            _ok(f"{label}  ({host}:{port})  latency={latency:.0f}ms")
        except Exception as e:
            _fail(f"{label}  ({host}:{port})  unreachable: {e}")
            all_ok = False
    return all_ok


def check_binance_api_keys(settings) -> bool:
    _section("5. Binance API key validity")

    if settings.is_dry_run:
        _warn("mode=dry_run — skipping API key validation (keys not needed yet)")
        return True

    key    = settings.active_api_key
    secret = settings.active_api_secret

    if not key or not secret:
        _fail("API keys not set in config/.env (see .env.example)")
        return False

    try:
        import hmac
        import hashlib
        import urllib.request
        import urllib.parse
        import json as _json

        if settings.is_paper:
            base = "https://testnet.binancefuture.com"
            label = "testnet"
        else:
            base = "https://fapi.binance.com"
            label = "live"

        ts        = int(time.time() * 1000)
        params    = f"timestamp={ts}"
        signature = hmac.new(
            secret.encode(), params.encode(), hashlib.sha256
        ).hexdigest()
        url = f"{base}/fapi/v2/account?{params}&signature={signature}"

        req = urllib.request.Request(url, headers={"X-MBX-APIKEY": key})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read())

        balance_usdt = next(
            (float(a["walletBalance"])
             for a in data.get("assets", [])
             if a["asset"] == "USDT"),
            None
        )
        _ok(f"API key valid ({label})")
        if balance_usdt is not None:
            _ok(f"USDT wallet balance: {balance_usdt:,.2f}")
        return True

    except urllib.error.HTTPError as e:
        if e.code == 401:
            _fail("API key rejected (401 Unauthorized) — check key and secret")
        elif e.code == 403:
            _fail("API key lacks Futures permission — enable in Binance API settings")
        else:
            _fail(f"HTTP {e.code}: {e.reason}")
        return False
    except Exception as e:
        _fail(f"API key check failed: {e}")
        return False


def check_telegram(settings) -> bool:
    _section("6. Telegram bot")

    if not settings.telegram.enabled:
        _warn("telegram.enabled=false in settings.yaml — skipping check")
        _warn("Set enabled: true and add TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to .env")
        return True

    token   = settings.telegram.bot_token
    chat_id = settings.telegram.chat_id

    if not token or not chat_id:
        _fail("Telegram enabled but BOT_TOKEN or CHAT_ID missing in .env")
        return False

    try:
        import urllib.request
        import json as _json

        # Verify bot token
        url  = f"https://api.telegram.org/bot{token}/getMe"
        req  = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read())

        if not data.get("ok"):
            _fail(f"Bot token invalid: {data}")
            return False

        bot_name = data["result"].get("username", "unknown")
        _ok(f"Bot token valid  @{bot_name}")

        # Send a test message
        msg_url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = _json.dumps({
            "chat_id": chat_id,
            "text": "✅ BTC Trader infrastructure check passed — bot is connected.",
            "parse_mode": "HTML",
        }).encode()
        req2 = urllib.request.Request(
            msg_url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req2, timeout=10) as resp2:
            data2 = _json.loads(resp2.read())

        if data2.get("ok"):
            _ok(f"Test message sent to chat_id={chat_id}")
        else:
            _fail(f"Message send failed: {data2}")
            return False

        return True

    except Exception as e:
        _fail(f"Telegram check failed: {e}")
        return False


def check_signal_modules() -> bool:
    _section("7. Signal modules (pure Python)")
    modules = [
        ("core.market_structure", "MarketStructure"),
        ("core.htf_bias",         "HTFBias"),
        ("core.fvg_zones",        "FVGZones"),
        ("core.atr",              "ATR"),
    ]
    all_ok = True
    for module, cls in modules:
        try:
            import importlib
            mod = importlib.import_module(module)
            getattr(mod, cls)
            _ok(f"{module}.{cls}")
        except ImportError:
            _warn(f"{module} not found — copy from ~/data/ into btc_trader/core/")
            all_ok = False
        except AttributeError:
            _fail(f"{cls} class missing from {module}")
            all_ok = False
    return all_ok


def check_nautilus() -> bool:
    _section("8. NautilusTrader")
    try:
        import nautilus_trader
        _ok(f"nautilus_trader version: {nautilus_trader.__version__}")
        from nautilus_trader.live.node import TradingNode
        from nautilus_trader.config import TradingNodeConfig
        _ok("TradingNode importable")
        return True
    except ImportError as e:
        _fail(f"NautilusTrader not installed: {e}")
        _warn("Install: pip install nautilus_trader")
        return False


# ── Main ──────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="BTC Trader — Stage 1 infrastructure check"
    )
    parser.add_argument(
        "--config", default="config/settings.yaml",
        help="Path to settings.yaml (default: config/settings.yaml)",
    )
    args = parser.parse_args()

    print("\n" + "═" * 60)
    print("  BTC Trader — Infrastructure Check")
    print("═" * 60)

    config_path = Path(args.config)
    config_ok, settings = check_config(str(config_path))

    if not config_ok or settings is None:
        print("\n\033[31mABORTED: fix configuration errors before continuing.\033[0m\n")
        sys.exit(1)

    env_ok      = check_env_file(config_path.parent)
    redis_ok    = check_redis(settings)
    network_ok  = check_binance_connectivity()
    api_ok      = check_binance_api_keys(settings)
    tg_ok       = check_telegram(settings)
    modules_ok  = check_signal_modules()
    nt_ok       = check_nautilus()

    # ── Summary ────────────────────────────────────────────────────────
    results = {
        "Config":          config_ok,
        "Environment":     env_ok,
        "Redis":           redis_ok,
        "Network":         network_ok,
        "Binance API":     api_ok,
        "Telegram":        tg_ok,
        "Signal modules":  modules_ok,
        "NautilusTrader":  nt_ok,
    }

    print("\n" + "═" * 60)
    print("  SUMMARY")
    print("═" * 60)
    all_passed = True
    for name, passed in results.items():
        icon   = "\033[32m✓\033[0m" if passed else "\033[31m✗\033[0m"
        status = "PASS" if passed else "FAIL"
        print(f"  {icon}  {name:<20} {status}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        print("  \033[32mAll checks passed — ready for Stage 2 (data feed).\033[0m")
    else:
        print("  \033[31mSome checks failed — fix the issues above before proceeding.\033[0m")
    print()

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
