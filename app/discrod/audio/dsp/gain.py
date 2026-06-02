"""Simple gain / trim processor (always applied, even when 'enabled' is off).

Unlike the other processors, gain is a fundamental mixer control so the channel
applies it unconditionally.  It is kept as a Processor so it can live in the
chain and be serialized uniformly.
"""

from __future__ import annotations

import numpy as np

from .base import Processor


def db_to_linear(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def linear_to_db(linear: float) -> float:
    linear = max(linear, 1e-9)
    return float(20.0 * np.log10(linear))


class Gain(Processor):
    name = "Gain"

    def __init__(self, sample_rate: int, gain_db: float = 0.0):
        super().__init__(sample_rate, enabled=True)
        self.gain_db = float(gain_db)

    def process(self, block: np.ndarray) -> None:
        g = db_to_linear(self.gain_db)
        if g != 1.0:
            block *= g

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["gain_db"] = self.gain_db
        return d

    def load_dict(self, data: dict) -> None:
        super().load_dict(data)
        self.gain_db = float(data.get("gain_db", self.gain_db))
