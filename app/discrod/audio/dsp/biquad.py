"""Biquad filter primitives (RBJ Audio EQ cookbook) used by the equalizer.

Each :class:`Biquad` is a single second-order section processed per channel with
a Transposed Direct Form II structure.  Coefficients are recomputed whenever a
parameter or the sample rate changes.
"""

from __future__ import annotations

import numpy as np

PEAK = "peak"
LOW_SHELF = "lowshelf"
HIGH_SHELF = "highshelf"
LOW_PASS = "lowpass"
HIGH_PASS = "highpass"


class Biquad:
    def __init__(self, sample_rate: int, ftype: str = PEAK,
                 freq: float = 1000.0, gain_db: float = 0.0, q: float = 0.707,
                 channels: int = 2):
        self.sample_rate = int(sample_rate)
        self.ftype = ftype
        self.freq = float(freq)
        self.gain_db = float(gain_db)
        self.q = float(q)
        # Transposed DF-II state: two memory taps per channel.
        self._z1 = np.zeros(channels, dtype=np.float64)
        self._z2 = np.zeros(channels, dtype=np.float64)
        self._channels = channels
        self._update()

    def reset(self) -> None:
        self._z1[:] = 0.0
        self._z2[:] = 0.0

    def set_channels(self, channels: int) -> None:
        if channels != self._channels:
            self._channels = channels
            self._z1 = np.zeros(channels, dtype=np.float64)
            self._z2 = np.zeros(channels, dtype=np.float64)

    def configure(self, ftype=None, freq=None, gain_db=None, q=None,
                  sample_rate=None) -> None:
        if ftype is not None:
            self.ftype = ftype
        if freq is not None:
            self.freq = float(freq)
        if gain_db is not None:
            self.gain_db = float(gain_db)
        if q is not None:
            self.q = float(q)
        if sample_rate is not None:
            self.sample_rate = int(sample_rate)
        self._update()

    def _update(self) -> None:
        a = 10.0 ** (self.gain_db / 40.0)
        w0 = 2.0 * np.pi * max(self.freq, 1.0) / self.sample_rate
        cos_w0 = np.cos(w0)
        sin_w0 = np.sin(w0)
        alpha = sin_w0 / (2.0 * max(self.q, 1e-4))

        if self.ftype == PEAK:
            b0 = 1 + alpha * a
            b1 = -2 * cos_w0
            b2 = 1 - alpha * a
            a0 = 1 + alpha / a
            a1 = -2 * cos_w0
            a2 = 1 - alpha / a
        elif self.ftype == LOW_SHELF:
            ap1, am1 = a + 1, a - 1
            ta = 2 * np.sqrt(a) * alpha
            b0 = a * (ap1 - am1 * cos_w0 + ta)
            b1 = 2 * a * (am1 - ap1 * cos_w0)
            b2 = a * (ap1 - am1 * cos_w0 - ta)
            a0 = ap1 + am1 * cos_w0 + ta
            a1 = -2 * (am1 + ap1 * cos_w0)
            a2 = ap1 + am1 * cos_w0 - ta
        elif self.ftype == HIGH_SHELF:
            ap1, am1 = a + 1, a - 1
            ta = 2 * np.sqrt(a) * alpha
            b0 = a * (ap1 + am1 * cos_w0 + ta)
            b1 = -2 * a * (am1 + ap1 * cos_w0)
            b2 = a * (ap1 + am1 * cos_w0 - ta)
            a0 = ap1 - am1 * cos_w0 + ta
            a1 = 2 * (am1 - ap1 * cos_w0)
            a2 = ap1 - am1 * cos_w0 - ta
        elif self.ftype == LOW_PASS:
            b0 = (1 - cos_w0) / 2
            b1 = 1 - cos_w0
            b2 = (1 - cos_w0) / 2
            a0 = 1 + alpha
            a1 = -2 * cos_w0
            a2 = 1 - alpha
        elif self.ftype == HIGH_PASS:
            b0 = (1 + cos_w0) / 2
            b1 = -(1 + cos_w0)
            b2 = (1 + cos_w0) / 2
            a0 = 1 + alpha
            a1 = -2 * cos_w0
            a2 = 1 - alpha
        else:
            raise ValueError(f"unknown filter type {self.ftype!r}")

        self._b0 = b0 / a0
        self._b1 = b1 / a0
        self._b2 = b2 / a0
        self._a1 = a1 / a0
        self._a2 = a2 / a0

    def process(self, block: np.ndarray) -> None:
        """In-place per-channel processing of ``(frames, channels)``."""
        b0, b1, b2, a1, a2 = self._b0, self._b1, self._b2, self._a1, self._a2
        z1, z2 = self._z1, self._z2
        frames = block.shape[0]
        # Per-sample recursion; vectorized across channels.
        for n in range(frames):
            x = block[n].astype(np.float64)
            y = b0 * x + z1
            z1[:] = b1 * x - a1 * y + z2
            z2[:] = b2 * x - a2 * y
            block[n] = y
