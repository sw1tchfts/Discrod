"""Audio clip loading and polyphonic voice playback.

A :class:`Clip` holds decoded float32 samples (always stored as stereo at the
engine sample rate).  A :class:`Voice` is one active playback of a clip and is
mixed additively into a channel buffer by the engine.
"""

from __future__ import annotations

import os

import numpy as np

try:
    import soundfile as sf
except Exception:  # pragma: no cover - import guarded for headless envs
    sf = None

# Playback modes for a mapped pad.
MODE_ONESHOT = "oneshot"   # play to end (re-trigger restarts)
MODE_GATE = "gate"         # play while key held, stop on note-off
MODE_LOOP = "loop"         # loop until re-triggered or stopped
MODE_TOGGLE = "toggle"     # first press starts, second stops


class Clip:
    def __init__(self, path: str, samples: np.ndarray, sample_rate: int):
        self.path = path
        self.name = os.path.splitext(os.path.basename(path))[0]
        self.samples = samples  # (frames, 2) float32
        self.sample_rate = sample_rate

    @property
    def frames(self) -> int:
        return self.samples.shape[0]

    @classmethod
    def load(cls, path: str, target_sr: int, channels: int = 2) -> "Clip":
        if sf is None:
            raise RuntimeError("soundfile is required to load audio clips")
        data, sr = sf.read(path, dtype="float32", always_2d=True)
        data = _to_channels(data, channels)
        if sr != target_sr:
            data = _resample_linear(data, sr, target_sr)
        return cls(path, np.ascontiguousarray(data), target_sr)


class Voice:
    """A single playing instance of a clip."""

    __slots__ = ("clip", "channel_name", "mode", "gain", "pos", "active", "held",
                 "key")

    def __init__(self, clip: Clip, channel_name: str, mode: str = MODE_ONESHOT,
                 gain: float = 1.0, key: object = None):
        self.clip = clip
        self.channel_name = channel_name
        self.mode = mode
        self.gain = gain
        self.pos = 0
        self.active = True
        self.held = True  # for gate mode
        self.key = key

    def note_off(self) -> None:
        if self.mode == MODE_GATE:
            self.active = False
        self.held = False

    def render_into(self, buffer: np.ndarray) -> None:
        """Additively mix this voice's next block into ``buffer`` (frames, 2)."""
        if not self.active:
            return
        frames = buffer.shape[0]
        src = self.clip.samples
        n = src.shape[0]
        written = 0
        while written < frames and self.active:
            remaining = n - self.pos
            take = min(frames - written, remaining)
            seg = src[self.pos:self.pos + take]
            if self.gain != 1.0:
                buffer[written:written + take] += seg * self.gain
            else:
                buffer[written:written + take] += seg
            self.pos += take
            written += take
            if self.pos >= n:
                if self.mode == MODE_LOOP and self.held:
                    self.pos = 0
                else:
                    self.active = False


def _to_channels(data: np.ndarray, channels: int) -> np.ndarray:
    if data.shape[1] == channels:
        return data
    if data.shape[1] == 1 and channels == 2:
        return np.repeat(data, 2, axis=1)
    if data.shape[1] == 2 and channels == 1:
        return data.mean(axis=1, keepdims=True)
    # Fallback: take/duplicate first channel.
    return np.repeat(data[:, :1], channels, axis=1)


def _resample_linear(data: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """Lightweight linear resampler (dependency-free).

    Good enough for soundboard clips; swap for ``scipy.signal.resample_poly``
    when scipy is available for higher quality.
    """
    if sr_in == sr_out:
        return data
    duration = data.shape[0] / sr_in
    out_frames = int(round(duration * sr_out))
    if out_frames <= 0:
        return np.zeros((0, data.shape[1]), dtype=np.float32)
    x_old = np.linspace(0.0, duration, num=data.shape[0], endpoint=False)
    x_new = np.linspace(0.0, duration, num=out_frames, endpoint=False)
    out = np.empty((out_frames, data.shape[1]), dtype=np.float32)
    for c in range(data.shape[1]):
        out[:, c] = np.interp(x_new, x_old, data[:, c]).astype(np.float32)
    return out
