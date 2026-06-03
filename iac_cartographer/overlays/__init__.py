"""Live-state overlays.

A `LiveStateOverlay` layers external workspace state (TFC / HCP / Terrakube)
on top of the static inventory. Read-only — we never trigger runs from
iac-cartographer.

The protocol lives in `live_state.py` alongside its implementations
(`TFCOverlay`, `TerrakubeOverlay`). All implementations share the same
protocol so the renderer / orchestrator / diagnose paths stay
backend-agnostic.
"""

from iac_cartographer.overlays.live_state import (
    LiveStateOverlay,
    StaleAlertCollector,
    TerrakubeOverlay,
    TFCOverlay,
    build_overlay,
)

__all__ = [
    "LiveStateOverlay",
    "StaleAlertCollector",
    "TFCOverlay",
    "TerrakubeOverlay",
    "build_overlay",
]
