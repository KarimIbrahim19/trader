"""
core/logging_setup.py
──────────────────────────────────────────────────────────────────────
Configures the Python logging system for the live trading process.

Two handlers are set up:
  • Console  — coloured, human-readable, INFO level by default
  • File     — rotating, DEBUG level, written to logs/ directory

Usage:
    from core.logging_setup import setup_logging
    from core.config import load_settings

    settings = load_settings()
    setup_logging(settings.logging)
    logger = logging.getLogger(__name__)
    logger.info("Ready")
"""

import logging
import logging.handlers
import sys
from pathlib import Path

from core.config import LoggingSettings


# ── Colour codes for console output ───────────────────────────────────
_RESET  = "\033[0m"
_GREY   = "\033[90m"
_GREEN  = "\033[32m"
_YELLOW = "\033[33m"
_RED    = "\033[31m"
_BOLD   = "\033[1m"

_LEVEL_COLOURS = {
    "DEBUG":    _GREY,
    "INFO":     _GREEN,
    "WARNING":  _YELLOW,
    "ERROR":    _RED,
    "CRITICAL": _BOLD + _RED,
}


class _ColouredFormatter(logging.Formatter):
    """Formatter that adds ANSI colour codes based on log level."""

    FMT = "%(asctime)s  %(levelname)-8s  %(name)-30s  %(message)s"
    DATEFMT = "%Y-%m-%d %H:%M:%S"

    def format(self, record: logging.LogRecord) -> str:
        colour = _LEVEL_COLOURS.get(record.levelname, _RESET)
        record.levelname = f"{colour}{record.levelname}{_RESET}"
        return super().format(record)

    def __init__(self) -> None:
        super().__init__(fmt=self.FMT, datefmt=self.DATEFMT)


class _PlainFormatter(logging.Formatter):
    """Plain formatter for the log file (no colour codes)."""

    FMT    = "%(asctime)s  %(levelname)-8s  %(name)-30s  %(message)s"
    DATEFMT = "%Y-%m-%d %H:%M:%S"

    def __init__(self) -> None:
        super().__init__(fmt=self.FMT, datefmt=self.DATEFMT)


def setup_logging(cfg: LoggingSettings, project_root: Path | None = None) -> None:
    """
    Initialise all logging handlers.

    Call this once at process startup, before any other module logs anything.
    """
    if project_root is None:
        project_root = Path(__file__).parent.parent

    log_dir  = project_root / cfg.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / cfg.log_file_name

    # ── Root logger ────────────────────────────────────────────────────
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)           # handlers control their own level
    root.handlers.clear()                  # avoid duplicate handlers on reload

    # ── Console handler ────────────────────────────────────────────────
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, cfg.level.upper(), logging.INFO))
    console.setFormatter(_ColouredFormatter())
    root.addHandler(console)

    # ── Rotating file handler ──────────────────────────────────────────
    file_handler = logging.handlers.RotatingFileHandler(
        filename    = log_file,
        maxBytes    = cfg.rotate_mb * 1024 * 1024,
        backupCount = cfg.keep_backups,
        encoding    = "utf-8",
    )
    file_handler.setLevel(getattr(logging, cfg.level_file.upper(), logging.DEBUG))
    file_handler.setFormatter(_PlainFormatter())
    root.addHandler(file_handler)

    # ── Quieten noisy third-party loggers ─────────────────────────────
    for noisy in ("urllib3", "asyncio", "websockets", "hpack", "httpcore", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    logger.info("Logging initialised  (console=%s  file=%s  path=%s)",
                cfg.level, cfg.level_file, log_file)
