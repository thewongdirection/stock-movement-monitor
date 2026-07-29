"""The four things this monitor looks for.

Ordered by how much they actually prove:

1. ``open_interest`` — contracts opened and held overnight. Proof a position was
   taken. Once a day, because the OCC settles it overnight.
2. ``insider`` — a named person bought or sold and filed a form saying so. The
   only stated direction in the set. Two-day reporting lag.
3. ``blocks`` — one print big enough that somebody authorised it. Size without
   direction. Needs a tick feed, so off by default.
4. ``volume`` — more trading than this hour usually sees. The most frequent and
   the least conclusive; it tells you where to look, not what to think.
"""

from .base import Baseline, Context, Signal, build_baseline, registry
from .blocks import BlockSignal
from .insider import InsiderSignal
from .open_interest import OpenInterestSignal
from .volume import VolumeSignal

__all__ = [
    "Baseline", "Context", "Signal", "build_baseline", "registry",
    "BlockSignal", "InsiderSignal", "OpenInterestSignal", "VolumeSignal",
]
