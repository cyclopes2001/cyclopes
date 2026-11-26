"""Central configuration for the order-book velocity engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence, Tuple


_DEFAULT_WINDOWS: Tuple[float, ...] = (1.0, 3.0, 5.0, 10.0)
_DEFAULT_LEVEL_WEIGHTS: Tuple[float, ...] = (1.0, 0.8, 0.6, 0.4, 0.2)


@dataclass(frozen=True)
class VelocityEngineConfig:
    engine_enabled: bool = True
    entry_enabled: bool = True
    initial_entry_override_enabled: bool = False
    initial_entry_max_price: float = 0.50
    paper_mode: bool = True
    windows_seconds: Tuple[float, ...] = _DEFAULT_WINDOWS
    min_confidence: float = 75.0
    signal_persistence_ms: float = 1000.0
    max_book_age_ms: float = 1000.0
    min_observations: int = 3
    min_depth: float = 5.0
    max_spread: float = 0.08
    min_price_movement_ticks: int = 1
    tick_size: float = 0.01
    reversal_confirmation_enabled: bool = True
    require_ofi_confirmation: bool = True
    require_obi_confirmation: bool = True
    require_depth_validation: bool = True
    obi_level_weights: Tuple[float, ...] = _DEFAULT_LEVEL_WEIGHTS
    velocity_fast_threshold: float = 0.002
    velocity_slow_threshold: float = 0.0008
    acceleration_threshold: float = 0.0005
    ofi_norm_cap: float = 50.0
    rising_fast_block_chase: bool = True


def _as_bool(val: Any, default: bool) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _as_float(val: Any, default: float) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _as_int(val: Any, default: int) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _windows(val: Any) -> Tuple[float, ...]:
    if isinstance(val, (list, tuple)) and val:
        try:
            return tuple(float(x) for x in val)
        except (TypeError, ValueError):
            pass
    return _DEFAULT_WINDOWS


def load_velocity_config(wc: Any) -> VelocityEngineConfig:
    """Build engine config from ``WorkerConfig`` or dict-like worker settings."""
    g = wc
    get = getattr(g, "__getitem__", None)
    if get is None:

        def _get(key: str, default: Any = None) -> Any:
            return getattr(g, key, default)

    else:

        def _get(key: str, default: Any = None) -> Any:
            try:
                return get(key)
            except (KeyError, TypeError):
                return getattr(g, key, default)

    return VelocityEngineConfig(
        engine_enabled=_as_bool(_get("velocity_engine_enabled", True), True),
        entry_enabled=_as_bool(_get("velocity_entry_enabled", True), True),
        initial_entry_override_enabled=_as_bool(
            _get("velocity_initial_entry_override_enabled", False), False
        ),
        initial_entry_max_price=_as_float(
            _get("velocity_initial_entry_max_price", 0.50), 0.50,
        ),
        paper_mode=_as_bool(_get("velocity_paper_mode", True), True),
        windows_seconds=_windows(_get("velocity_windows_seconds")),
        min_confidence=_as_float(_get("velocity_min_confidence", 75.0), 75.0),
        signal_persistence_ms=_as_float(_get("velocity_signal_persistence_ms", 1000.0), 1000.0),
        max_book_age_ms=_as_float(_get("velocity_max_book_age_ms", 1000.0), 1000.0),
        min_observations=_as_int(_get("velocity_min_observations", 3), 3),
        min_depth=_as_float(_get("velocity_min_depth", 5.0), 5.0),
        max_spread=_as_float(_get("velocity_max_spread", 0.08), 0.08),
        min_price_movement_ticks=_as_int(_get("velocity_min_price_movement_ticks", 1), 1),
        tick_size=_as_float(_get("velocity_tick_size", 0.01), 0.01),
        reversal_confirmation_enabled=_as_bool(
            _get("velocity_reversal_confirmation_enabled", True), True
        ),
        require_ofi_confirmation=_as_bool(_get("velocity_require_ofi_confirmation", True), True),
        require_obi_confirmation=_as_bool(_get("velocity_require_obi_confirmation", True), True),
        require_depth_validation=_as_bool(_get("velocity_require_depth_validation", True), True),
        velocity_fast_threshold=_as_float(_get("velocity_fast_threshold", 0.002), 0.002),
        velocity_slow_threshold=_as_float(_get("velocity_slow_threshold", 0.0008), 0.0008),
        acceleration_threshold=_as_float(_get("velocity_acceleration_threshold", 0.0005), 0.0005),
        ofi_norm_cap=_as_float(_get("velocity_ofi_norm_cap", 50.0), 50.0),
        rising_fast_block_chase=_as_bool(_get("velocity_rising_fast_block_chase", True), True),
    )