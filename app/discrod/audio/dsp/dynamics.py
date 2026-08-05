"""Dynamics processors: a noise gate and a compressor.

Both use a peak detector with separate attack/release ballistics on a per-block
basis, computing one gain coefficient per sample from the linked (max across
channels) detector signal.  This keeps the stereo image stable.

Implementation note: everything that can be vectorized (detector, dB
conversions, gain application) runs as numpy array ops; only the inherently
sequential envelope recursion runs as a loop, and that loop works on plain
Python floats — numpy scalar ops per sample were measured to blow the
real-time budget inside the audio callback.
"""

from __future__ import annotations

import numpy as np

from .base import Processor
from .gain import db_to_linear


def _coeff(time_ms: float, sample_rate: int) -> float:
    """One-pole smoothing coefficient for the given time constant."""
    time_ms = max(time_ms, 0.01)
    return float(np.exp(-1.0 / (sample_rate * time_ms / 1000.0)))


class Gate(Processor):
    """Downward expander / noise gate.

    Below ``threshold_db`` the signal is attenuated toward silence (controlled by
    ``range_db``).  Useful on the mic channel to suppress background noise
    between phrases.
    """

    name = "Gate"

    def __init__(self, sample_rate: int, threshold_db: float = -45.0,
                 range_db: float = -60.0, attack_ms: float = 1.0,
                 hold_ms: float = 50.0, release_ms: float = 120.0,
                 enabled: bool = False):
        super().__init__(sample_rate, enabled=enabled)
        self.threshold_db = float(threshold_db)
        self.range_db = float(range_db)
        self.attack_ms = float(attack_ms)
        self.hold_ms = float(hold_ms)
        self.release_ms = float(release_ms)
        self._env = 0.0      # smoothed gain (1 = open, 0 = closed)
        self._hold_count = 0

    def reset(self) -> None:
        self._env = 0.0
        self._hold_count = 0

    def process(self, block: np.ndarray) -> None:
        if not self.enabled:
            return
        thr = db_to_linear(self.threshold_db)
        floor = db_to_linear(self.range_db)
        atk = _coeff(self.attack_ms, self.sample_rate)
        rel = _coeff(self.release_ms, self.sample_rate)
        one_m_atk = 1.0 - atk
        one_m_rel = 1.0 - rel
        hold_samples = int(self.sample_rate * self.hold_ms / 1000.0)
        detector = np.max(np.abs(block), axis=1).tolist()
        gains = np.empty(len(detector), dtype=np.float32)
        env = self._env
        hold = self._hold_count
        for n, level in enumerate(detector):
            target = 1.0 if level >= thr else floor
            if target >= env:
                hold = hold_samples
                env = atk * env + one_m_atk * target
            else:
                if hold > 0:
                    hold -= 1
                else:
                    env = rel * env + one_m_rel * target
            gains[n] = env
        block *= gains[:, None]
        self._env = env
        self._hold_count = hold

    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update(threshold_db=self.threshold_db, range_db=self.range_db,
                 attack_ms=self.attack_ms, hold_ms=self.hold_ms,
                 release_ms=self.release_ms)
        return d

    def load_dict(self, data: dict) -> None:
        super().load_dict(data)
        self.threshold_db = float(data.get("threshold_db", self.threshold_db))
        self.range_db = float(data.get("range_db", self.range_db))
        self.attack_ms = float(data.get("attack_ms", self.attack_ms))
        self.hold_ms = float(data.get("hold_ms", self.hold_ms))
        self.release_ms = float(data.get("release_ms", self.release_ms))


class Compressor(Processor):
    """Feed-forward peak compressor with soft-ish knee and make-up gain.

    The level detector is clamped to ``threshold_db - DETECTOR_FLOOR_DB`` so the
    envelope cannot free-fall toward -180 dB during speech pauses; without the
    clamp the envelope needs tens of milliseconds to climb back above threshold
    and every phrase onset passes uncompressed.
    """

    name = "Compressor"

    #: How far below threshold the detector may sink (dB).  Bounds the
    #: re-engagement time after silence to a few milliseconds.
    DETECTOR_FLOOR_DB = 20.0

    def __init__(self, sample_rate: int, threshold_db: float = -18.0,
                 ratio: float = 3.0, attack_ms: float = 10.0,
                 release_ms: float = 120.0, makeup_db: float = 0.0,
                 enabled: bool = False):
        super().__init__(sample_rate, enabled=enabled)
        self.threshold_db = float(threshold_db)
        self.ratio = float(ratio)
        self.attack_ms = float(attack_ms)
        self.release_ms = float(release_ms)
        self.makeup_db = float(makeup_db)
        self._env_db = self.threshold_db - self.DETECTOR_FLOOR_DB

    def reset(self) -> None:
        self._env_db = self.threshold_db - self.DETECTOR_FLOOR_DB

    def process(self, block: np.ndarray) -> None:
        if not self.enabled:
            return
        atk = _coeff(self.attack_ms, self.sample_rate)
        rel = _coeff(self.release_ms, self.sample_rate)
        one_m_atk = 1.0 - atk
        one_m_rel = 1.0 - rel
        makeup = db_to_linear(self.makeup_db)
        thr = self.threshold_db
        floor_db = thr - self.DETECTOR_FLOOR_DB
        ratio = max(self.ratio, 1.0)
        slope = 1.0 - 1.0 / ratio
        detector = np.max(np.abs(block), axis=1)
        levels_db = 20.0 * np.log10(detector + 1e-9)
        np.maximum(levels_db, floor_db, out=levels_db)
        levels = levels_db.tolist()
        gains_db = np.empty(len(levels), dtype=np.float64)
        # Clamp from below only: the envelope must track levels above 0 dBFS
        # (summed voices with positive pad gain), otherwise it re-attacks at
        # every block boundary — an audible block-rate sawtooth.
        env_db = max(self._env_db, floor_db)
        for n, level_db in enumerate(levels):
            # Smooth the detector with attack/release ballistics.
            if level_db > env_db:
                env_db = atk * env_db + one_m_atk * level_db
            else:
                env_db = rel * env_db + one_m_rel * level_db
            over = env_db - thr
            gains_db[n] = -over * slope if over > 0 else 0.0
        gains = np.power(10.0, gains_db / 20.0) * makeup
        block *= gains[:, None].astype(np.float32)
        self._env_db = env_db

    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update(threshold_db=self.threshold_db, ratio=self.ratio,
                 attack_ms=self.attack_ms, release_ms=self.release_ms,
                 makeup_db=self.makeup_db)
        return d

    def load_dict(self, data: dict) -> None:
        super().load_dict(data)
        self.threshold_db = float(data.get("threshold_db", self.threshold_db))
        self.ratio = float(data.get("ratio", self.ratio))
        self.attack_ms = float(data.get("attack_ms", self.attack_ms))
        self.release_ms = float(data.get("release_ms", self.release_ms))
        self.makeup_db = float(data.get("makeup_db", self.makeup_db))
