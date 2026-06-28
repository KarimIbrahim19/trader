"""
strategies/__init__.py
────────────────────────────────────────────────────────────────────────
Registry mapping strategy names → settings class + strategy class.
Adding a new strategy = create its module + add one entry here.
No changes required in core/config.py or main.py.
"""

from strategies.fvg_strategy import FvgSettings, FvgStrategy
from strategies.ms_strategy import MsSettings, MsStrategy

REGISTRY: dict[str, dict] = {
    "ms":  {"settings": MsSettings,  "strategy": MsStrategy},
    "fvg": {"settings": FvgSettings, "strategy": FvgStrategy},
}
