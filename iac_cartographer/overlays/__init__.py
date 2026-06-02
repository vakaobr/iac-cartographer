"""Live-state overlays.

A `LiveStateOverlay` layers external workspace state (TFC / HCP / Terrakube)
on top of the static inventory. Read-only — we never trigger runs from
iac-cartographer.

The protocol lives in `live_state.py` alongside the first implementation
(`TFCOverlay`). Sibling backends (Terrakube — see issue #99) will land as
additional classes in the same module, sharing the protocol so the
renderer / orchestrator / diagnose paths stay backend-agnostic.
"""

from iac_cartographer.overlays.live_state import (
    LiveStateOverlay,
    StaleAlertCollector,
    TFCOverlay,
    build_overlay,
)

__all__ = ["LiveStateOverlay", "StaleAlertCollector", "TFCOverlay", "build_overlay"]
