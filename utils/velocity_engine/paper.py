"""Paper-mode forward measurement for velocity signals (no lookahead)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


_HORIZONS = (1.0, 3.0, 5.0, 10.0)


@dataclass
class PaperForwardSample:
    decision_ts: float
    yes_mid: float
    no_mid: float
    decision: str
    preferred_side: Optional[str]
    action_kind: str
    forwards: Dict[str, Optional[float]] = field(default_factory=dict)


class VelocityPaperJournal:
    """Record mids at decision time; fill forward mids when wall time elapses."""

    def __init__(self, *, max_pending: int = 200) -> None:
        self._pending: List[PaperForwardSample] = []
        self._completed: List[PaperForwardSample] = []
        self._max_pending = max_pending

    def record(
        self,
        *,
        yes_mid: float,
        no_mid: float,
        decision: str,
        preferred_side: Optional[str],
        action_kind: str,
    ) -> None:
        if yes_mid <= 0 and no_mid <= 0:
            return
        self._pending.append(
            PaperForwardSample(
                decision_ts=time.time(),
                yes_mid=float(yes_mid),
                no_mid=float(no_mid),
                decision=str(decision),
                preferred_side=preferred_side,
                action_kind=str(action_kind),
                forwards={f"{int(h)}s": None for h in _HORIZONS},
            )
        )
        if len(self._pending) > self._max_pending:
            self._pending = self._pending[-self._max_pending :]

    def advance(self, *, yes_mid: float, no_mid: float) -> None:
        if yes_mid <= 0 and no_mid <= 0:
            return
        now = time.time()
        still: List[PaperForwardSample] = []
        for sample in self._pending:
            elapsed = now - sample.decision_ts
            for h in _HORIZONS:
                key = f"{int(h)}s"
                if sample.forwards.get(key) is None and elapsed >= h:
                    sample.forwards[key] = float(yes_mid if sample.preferred_side == "YES" else no_mid)
            if all(v is not None for v in sample.forwards.values()):
                self._completed.append(sample)
            else:
                still.append(sample)
        self._pending = still
        if len(self._completed) > 500:
            self._completed = self._completed[-500:]

    def summary(self) -> Dict[str, object]:
        return {
            "pending_samples": len(self._pending),
            "completed_samples": len(self._completed),
            "horizons_seconds": list(_HORIZONS),
        }