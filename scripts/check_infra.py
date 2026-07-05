"""
scripts/check_infra.py
────────────────────────────────────────────────────────────────────────
Infrastructure validation script.

Checks every infrastructure component before live trading.
Run after initial setup and after any environment change.

Usage:
    cd live_trader
    python scripts/check_infra.py
    python scripts/check_infra.py --config config/settings.yaml
"""

import argparse
import logging
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _ok(msg: str)   -> None: print(f"  \033[32m✓\033[0m  {msg}")
def _fail(msg: str) -> None: print(f"  \033[31m✗\033[0m  {msg}")
def _warn(msg: str) -> None: print(f"  \033[33m⚠\033[0m  {msg}")
def _section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")
    print("─" * 60)


# ── 1. Configuration ──────────────────────────────────────────────────────
def check_config(config_path: str):
    _section("1. Configuration")
    try:
        from core.config import load_settings
        settings = load_settings(Path(config_path).parent, Path(config_path).name)
        _ok(f"settings.yaml loaded  (mode={settings.mode})")
        _ok(f"Instrument : {settings.instrument.nt_id}")

        if not settings.strategies:
            _fail("No strategies defined in settings.yaml")
            return False, None

        for name, s in settings.strategies.items():
            status = "\033[32mENABLED\033[0m" if s.enabled else "\033[90mdisabled\033[0m"
            print(f"\n  Strategy [{status}] \033[1m{name.upper()}\033[0m")
            _ok(f"  bars: primary={s.primary_bar}  htf={s.htf_bar}  htf_filter={s.htf_filter}")
            if s.enabled:
                _ok(
                    f"  risk: size={s.trade_size} BTC  "
                    f"SL={s.sl_atr}×  TP1={s.tp1_atr}×  TP2={s.tp2_atr}×  "
                    f"BE={s.breakeven_sl}  trailing={s.trailing_tp2}"
                )
                _ok(
                    f"  limits: max_open={s.max_open_trades}  "
                    f"daily_loss=${s.daily_loss_limit_usdt}"
                )

        enabled = settings.enabled_strategies
        if not enabled:
            _warn("No strategies are currently enabled — system will start but not trade.")
        else:
            _ok(f"{len(enabled)} strategy/strategies enabled: {', '.join(enabled.keys())}")

        return True, settings
    except Exception as e:
        _fail(f"Config load failed: {e}")
        return False, None


# ── 2. Environment / Secrets ──────────────────────────────────────────────
def check_env_file(config_dir: Path) -> bool:
    _section("2. Environment / Secrets (.env)")
    env_file = config_dir / ".env"
    if not env_file.exists():
        _warn(".env file not found — secrets must be set as real environment variables")
        _warn("Copy config/.env.example → config/.env and fill in your keys")
        return True

    from dotenv import dotenv_values
    env = dotenv_values(env_file)
    placeholders = {
        "your_api_key_here", "your_api_secret_here",
        "your_bot_token_here", "your_chat_id_here",
        "your_testnet_key_here", "your_testnet_secret_here",
    }
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


# ── 3. Redis ──────────────────────────────────────────────────────────────
def check_redis(settings) -> bool:
    _section("3. Redis")
    try:
        import redis
        host = settings.redis.host
        port = settings.redis.port
        r = redis.Redis(host=host, port=port, socket_timeout=settings.redis.timeout_secs)
        t0 = time.time()
        r.ping()
        latency = (time.time() - t0) * 1000
        _ok(f"Connected  {host}:{port}  latency={latency:.1f}ms")
        info = r.info("server")
        _ok(f"Redis version: {info.get('redis_version', 'unknown')}")
        return True
    except ImportError:
        _fail("redis package not installed — run: pip install redis")
        return False
    except Exception as e:
        _fail(f"Cannot connect to Redis: {e}")
        _warn("Start Redis: redis-server")
        return False


# ── 4. Binance connectivity ───────────────────────────────────────────────
def check_binance_connectivity() -> bool:
    _section("4. Binance Futures API (network reachability)")
    endpoints = [
        ("fapi.binance.com",    443, "Futures REST API"),
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


# ── 5. Binance API keys ───────────────────────────────────────────────────
def check_binance_api_keys(settings) -> bool:
    _section("5. Binance API key validity")
    if settings.is_dry_run:
        _warn("mode=dry_run — skipping API key validation (keys not needed)")
        return True

    key    = settings.active_api_key
    secret = settings.active_api_secret
    if not key or not secret:
        _fail("API keys not set in config/.env (see .env.example)")
        return False

    try:
        import hashlib, hmac, json as _json, urllib.request, urllib.parse
        base = "https://testnet.binancefuture.com" if settings.is_paper else "https://fapi.binance.com"
        label = "testnet" if settings.is_paper else "live"
        ts        = int(time.time() * 1000)
        params    = f"timestamp={ts}"
        signature = hmac.new(secret.encode(), params.encode(), hashlib.sha256).hexdigest()
        url = f"{base}/fapi/v2/account?{params}&signature={signature}"
        req = urllib.request.Request(url, headers={"X-MBX-APIKEY": key})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read())
        balance = next(
            (float(a["walletBalance"]) for a in data.get("assets", []) if a["asset"] == "USDT"),
            None,
        )
        _ok(f"API key valid ({label})")
        if balance is not None:
            _ok(f"USDT wallet balance: {balance:,.2f}")
        return True
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _fail("API key rejected (401) — check key and secret")
        elif e.code == 403:
            _fail("API key lacks Futures permission")
        else:
            _fail(f"HTTP {e.code}: {e.reason}")
        return False
    except Exception as e:
        _fail(f"API key check failed: {e}")
        return False


# ── 6. Telegram ───────────────────────────────────────────────────────────
def check_telegram(settings) -> bool:
    _section("6. Telegram bot")
    if not settings.telegram.enabled:
        _warn("telegram.enabled=false — skipping check")
        return True
    token   = settings.telegram.bot_token
    chat_id = settings.telegram.chat_id
    if not token or not chat_id:
        _fail("Telegram enabled but BOT_TOKEN or CHAT_ID missing in .env")
        return False
    try:
        import json as _json, urllib.request
        url = f"https://api.telegram.org/bot{token}/getMe"
        with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as r:
            data = _json.loads(r.read())
        if not data.get("ok"):
            _fail(f"Bot token invalid: {data}")
            return False
        _ok(f"Bot token valid  @{data['result'].get('username', 'unknown')}")
        msg_url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = _json.dumps({
            "chat_id": chat_id,
            "text": "✅ BTC Trader — infrastructure check passed.",
        }).encode()
        req2 = urllib.request.Request(msg_url, data=payload,
                                       headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req2, timeout=10) as r2:
            data2 = _json.loads(r2.read())
        if data2.get("ok"):
            _ok(f"Test message sent to chat_id={chat_id}")
        else:
            _fail(f"Message send failed: {data2}")
            return False
        return True
    except Exception as e:
        _fail(f"Telegram check failed: {e}")
        return False


# ── 7. Signal modules ─────────────────────────────────────────────────────
def check_signal_modules() -> bool:
    _section("7. Signal modules (core/)")
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
            _warn(f"{module} not found — copy from ~/data/ into core/")
            all_ok = False
        except AttributeError:
            _fail(f"{cls} class missing from {module}")
            all_ok = False
    return all_ok


# ── 8. Strategy modules ───────────────────────────────────────────────────
def check_strategy_modules() -> bool:
    _section("8. Strategy modules")
    modules = [
        ("risk.trade_ledger",         "TradeLedger"),
        ("risk.position_manager",     "PositionManager"),
        ("persistence.state_store",   "StateStore"),
        ("strategies.base_smc_strategy", "BaseSmcStrategy"),
        ("strategies.ms_strategy",    "MsStrategy"),
        ("strategies.fvg_strategy",   "FvgStrategy"),
    ]
    all_ok = True
    for module, cls in modules:
        try:
            import importlib
            mod = importlib.import_module(module)
            getattr(mod, cls)
            _ok(f"{module}.{cls}")
        except ImportError as e:
            _fail(f"{module} import error: {e}")
            all_ok = False
        except AttributeError:
            _fail(f"{cls} class missing from {module}")
            all_ok = False
    return all_ok


# ── 9. NautilusTrader ────────────────────────────────────────────────────
def check_nautilus() -> bool:
    _section("9. NautilusTrader")
    try:
        import nautilus_trader
        _ok(f"nautilus_trader version: {nautilus_trader.__version__}")
        from nautilus_trader.live.node import TradingNode
        _ok("TradingNode importable")
        return True
    except ImportError as e:
        _fail(f"NautilusTrader not installed: {e}")
        return False


# ── State files ───────────────────────────────────────────────────────────
def check_state_files() -> bool:
    _section("10. Persistence state files")
    state_dir = Path("state")
    if not state_dir.exists():
        _ok("state/ directory does not exist yet — will be created on first run")
        return True
    state_files = list(state_dir.glob("*_state.json"))
    if not state_files:
        _ok("state/ exists, no saved trade state found (clean start)")
    else:
        _warn(f"Found {len(state_files)} saved state file(s):")
        for f in state_files:
            import json
            try:
                with open(f) as fp:
                    data = json.load(fp)
                n     = len(data.get("open_trades", []))
                saved = data.get("saved_at", "unknown")
                _warn(f"  {f.name}  —  {n} open trade(s)  saved at {saved}")
            except Exception:
                _warn(f"  {f.name}  —  could not parse")
    return True


# ── Main ──────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="BTC Trader — infrastructure check")
    parser.add_argument("--config", default="config/settings.yaml")
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
    strategy_ok = check_strategy_modules()
    nt_ok       = check_nautilus()
    state_ok    = check_state_files()

    results = {
        "Config":           config_ok,
        "Environment":      env_ok,
        "Redis":            redis_ok,
        "Network":          network_ok,
        "Binance API":      api_ok,
        "Telegram":         tg_ok,
        "Signal modules":   modules_ok,
        "Strategy modules":  strategy_ok,
        "NautilusTrader":   nt_ok,
        "State files":      state_ok,
    }

    print("\n" + "═" * 60)
    print("  SUMMARY")
    print("═" * 60)
    all_passed = True
    for name, passed in results.items():
        icon   = "\033[32m✓\033[0m" if passed else "\033[31m✗\033[0m"
        status = "PASS" if passed else "FAIL"
        print(f"  {icon}  {name:<22} {status}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        print("  \033[32mAll checks passed — ready to run.\033[0m")
        enabled = settings.enabled_strategies
        if enabled:
            print(f"  Active strategies: {', '.join(enabled.keys())}")
        print()
        print("  Start with:  python main.py")
    else:
        print("  \033[31mSome checks failed — fix the issues above before proceeding.\033[0m")
    print()

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
