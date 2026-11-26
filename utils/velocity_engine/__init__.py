"""Order-book velocity, imbalance, and entry-selection engine (Polymarket-only)."""

from utils.velocity_engine.config import VelocityEngineConfig, load_velocity_config
from utils.velocity_engine.engine import OrderBookVelocityEngine, VelocitySnapshot
from utils.velocity_engine.models import (
    EntryClassification,
    VelocityContext,
    VelocityDecision,
    VelocitySignalState,
)

__all__ = [
    "EntryClassification",
    "OrderBookVelocityEngine",
    "VelocityContext",
    "VelocityDecision",
    "VelocityEngineConfig",
    "VelocitySignalState",
    "VelocitySnapshot",
    "load_velocity_config",
]