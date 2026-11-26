"""Velocity, acceleration, OBI, and rolling OFI calculations."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Sequence, Tuple

from utils.velocity_engine.snapshot import BookSnapshot, ofi_delta, weighted_depth, weighted_obi


def price_velocity(current: float, previous: float, elapsed: float) -> float:
    if elapsed <= 1e-9 or not math.isfinite(elapsed):
        return 0.0
    if not math.isfinite(current) or not math.isfinite(previous):
        return 0.0
    return (current - previous) / elapsed


def velocity_acceleration(current_vel: float, previous_vel: float, elapsed: float) -> float:
    if elapsed <= 1e-9:
        return 0.0
    return (current_vel - previous_vel) / elapsed


def normalize_ofi(raw: float, *, cap: float, depth_scale: float) -> float:
    scale = max(1.0, depth_scale, cap * 0.25)
    if cap > 0:
        scale = min(scale, cap)
    return max(-1.0, min(1.0, raw / scale))


@dataclass
class SideHistory:
    maxlen: int = 512
    snapshots: Deque[BookSnapshot] = field(default_factory=deque)
    ofi_events: Deque[Tuple[float, float]] = field(default_factory=deque)

    def append(self, snap: BookSnapshot) -> Optional[float]:
        ofi_inc: Optional[float] = None
        if self.snapshots:
            prev = self.snapshots[-1]
            ofi_inc = ofi_delta(prev, snap, tick=0.01)
            self.ofi_events.append((snap.ts_mono, ofi_inc))
        self.snapshots.append(snap)
        while len(self.snapshots) > self.maxlen:
            self.snapshots.popleft()
        self._trim_ofi(snap.ts_mono, 12.0)
        return ofi_inc

    def _trim_ofi(self, now: float, horizon: float) -> None:
        while self.ofi_events and now - self.ofi_events[0][0] > horizon:
            self.ofi_events.popleft()

    def observations_in(self, seconds: float, now: float) -> List[BookSnapshot]:
        cutoff = now - seconds
        return [s for s in self.snapshots if s.ts_mono >= cutoff]

    def ofi_in(self, seconds: float, now: float) -> float:
        cutoff = now - seconds
        return sum(v for t, v in self.ofi_events if t >= cutoff)

    def latest(self) -> Optional[BookSnapshot]:
        return self.snapshots[-1] if self.snapshots else None

    def clear(self) -> None:
        self.snapshots.clear()
        self.ofi_events.clear()


def window_velocity(snaps: List[BookSnapshot], *, field_name: str = "mid") -> float:
    if len(snaps) < 2:
        return 0.0
    first, last = snaps[0], snaps[-1]
    elapsed = last.ts_mono - first.ts_mono
    if elapsed <= 1e-9:
        return 0.0
    if field_name == "bid":
        return price_velocity(last.best_bid, first.best_bid, elapsed)
    if field_name == "ask":
        return price_velocity(last.best_ask, first.best_ask, elapsed)
    return price_velocity(last.mid, first.mid, elapsed)


def window_acceleration(
    history: SideHistory,
    window: float,
    now: float,
    *,
    field_name: str = "mid",
) -> Tuple[float, float]:
    snaps = history.observations_in(window, now)
    if len(snaps) < 3:
        return 0.0, window_velocity(snaps, field_name=field_name)
    mid = len(snaps) // 2
    first_half = snaps[: mid + 1]
    second_half = snaps[mid:]
    v1 = window_velocity(first_half, field_name=field_name)
    v2 = window_velocity(second_half, field_name=field_name)
    elapsed = second_half[-1].ts_mono - first_half[0].ts_mono
    if elapsed <= 1e-9:
        return 0.0, v2
    return velocity_acceleration(v2, v1, elapsed), v2


def aggregate_ofi(
    history: SideHistory,
    windows: Sequence[float],
    now: float,
    *,
    norm_cap: float,
    latest: Optional[BookSnapshot],
) -> Dict[float, float]:
    depth_scale = 0.0
    if latest:
        depth_scale = max(latest.bid_depth_5, latest.ask_depth_5, 1.0)
    out: Dict[float, float] = {}
    for w in windows:
        raw = history.ofi_in(w, now)
        out[w] = normalize_ofi(raw, cap=norm_cap, depth_scale=depth_scale)
    return out


def movement_sufficient(velocity: float, *, tick: float, min_ticks: int) -> bool:
    return abs(velocity) >= tick * min_ticks - 1e-12