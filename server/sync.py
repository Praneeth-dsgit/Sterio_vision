from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .cameras import Frame


@dataclass
class SyncedPair:
    left: Frame
    right: Frame
    skew_ms: float

    @property
    def timestamp(self) -> float:
        return (self.left.timestamp + self.right.timestamp) * 0.5


def pair_frames(left: Optional[Frame], right: Optional[Frame], max_skew_ms: float) -> Optional[SyncedPair]:
    if left is None or right is None:
        return None
    skew_ms = abs(left.timestamp - right.timestamp) * 1000.0
    if skew_ms > max_skew_ms:
        return None
    return SyncedPair(left=left, right=right, skew_ms=skew_ms)
