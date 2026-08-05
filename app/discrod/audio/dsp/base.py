"""Base class for all audio processors.

A processor operates in-place on a float32 block shaped ``(frames, channels)``.
Processors are stateful (filters keep history, dynamics keep envelopes) and must
be cheap enough to run inside the real-time audio callback.

Parameters are plain attributes guarded by ``enabled``.  The UI thread mutates
them directly; under CPython the GIL makes the individual float/bool stores
atomic enough for a prototype.  When we move to a lock-free design these become
double-buffered parameter snapshots.
"""

from __future__ import annotations

import numpy as np


class Processor:
    #: Human readable name shown in the UI.
    name = "Processor"

    def __init__(self, sample_rate: int, enabled: bool = False):
        self.sample_rate = int(sample_rate)
        self.enabled = bool(enabled)

    def set_sample_rate(self, sample_rate: int) -> None:
        """Called when the engine's sample rate changes; recompute coefficients."""
        self.sample_rate = int(sample_rate)

    def reset(self) -> None:
        """Clear any internal state (filter memory, envelopes)."""

    def process(self, block: np.ndarray) -> None:
        """Process ``block`` in place.  ``block`` is float32 ``(frames, channels)``."""
        raise NotImplementedError

    # --- serialization -----------------------------------------------------
    def to_dict(self) -> dict:
        return {"type": self.__class__.__name__, "enabled": self.enabled}

    def load_dict(self, data: dict) -> None:
        self.enabled = bool(data.get("enabled", self.enabled))
