# Changelog

## 2026-06-26 — Minor fixes from manager review

### Fixed

- `core/config.py` — Removed unused `field` from dataclass import (`field,` → `fields` only). Lint noise, no functional change.

- `core/config.py` — Added `build_config()` to `StrategySettingsBase` with a `NotImplementedError`. Previously calling `build_config()` on a subclass that forgot to implement it would raise a cryptic `AttributeError` at runtime. Now it raises a clear error pointing to `MsSettings` or `FvgSettings` as the pattern.
