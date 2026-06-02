"""Multi-band parametric equalizer built from cascaded biquads.

Defaults to a 3-band EQ (low shelf, mid peak, high shelf) but any number of
bands can be configured.  Disabled bands are skipped.
"""

from __future__ import annotations

import numpy as np

from .base import Processor
from .biquad import Biquad, LOW_SHELF, PEAK, HIGH_SHELF


DEFAULT_BANDS = [
    {"type": LOW_SHELF, "freq": 120.0, "gain_db": 0.0, "q": 0.707},
    {"type": PEAK, "freq": 1000.0, "gain_db": 0.0, "q": 1.0},
    {"type": HIGH_SHELF, "freq": 8000.0, "gain_db": 0.0, "q": 0.707},
]


class Equalizer(Processor):
    name = "EQ"

    def __init__(self, sample_rate: int, channels: int = 2, bands=None,
                 enabled: bool = False):
        super().__init__(sample_rate, enabled=enabled)
        self._channels = channels
        specs = bands if bands is not None else DEFAULT_BANDS
        self.bands = [
            Biquad(sample_rate, b["type"], b["freq"], b["gain_db"], b["q"],
                   channels)
            for b in specs
        ]

    def set_sample_rate(self, sample_rate: int) -> None:
        super().set_sample_rate(sample_rate)
        for band in self.bands:
            band.configure(sample_rate=sample_rate)

    def set_channels(self, channels: int) -> None:
        self._channels = channels
        for band in self.bands:
            band.set_channels(channels)

    def reset(self) -> None:
        for band in self.bands:
            band.reset()

    def set_band_gain(self, index: int, gain_db: float) -> None:
        self.bands[index].configure(gain_db=gain_db)

    def set_band_freq(self, index: int, freq: float) -> None:
        self.bands[index].configure(freq=freq)

    def set_band_q(self, index: int, q: float) -> None:
        self.bands[index].configure(q=q)

    def process(self, block: np.ndarray) -> None:
        if not self.enabled:
            return
        for band in self.bands:
            # Skip flat peak/shelf bands as a cheap optimization.
            if band.ftype in (LOW_SHELF, PEAK, HIGH_SHELF) and band.gain_db == 0.0:
                continue
            band.process(block)

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["bands"] = [
            {"type": b.ftype, "freq": b.freq, "gain_db": b.gain_db, "q": b.q}
            for b in self.bands
        ]
        return d

    def load_dict(self, data: dict) -> None:
        super().load_dict(data)
        specs = data.get("bands")
        if specs:
            self.bands = [
                Biquad(self.sample_rate, b["type"], b["freq"], b["gain_db"],
                       b["q"], self._channels)
                for b in specs
            ]
