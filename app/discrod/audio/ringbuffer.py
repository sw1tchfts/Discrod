"""A simple thread-safe ring buffer for moving mic frames between the input and
output audio callbacks.

The microphone is captured on one PortAudio stream and consumed on the output
stream's callback.  A lock-guarded numpy ring decouples the two.  A lock in the
callback is acceptable for a prototype; the contention window is tiny and the
buffer is sized to absorb jitter.  A future revision can replace this with a
lock-free SPSC ring.
"""

from __future__ import annotations

import threading

import numpy as np


class RingBuffer:
    def __init__(self, capacity_frames: int, channels: int):
        self.capacity = capacity_frames
        self.channels = channels
        self._buf = np.zeros((capacity_frames, channels), dtype=np.float32)
        self._write = 0
        self._read = 0
        self._count = 0
        self._lock = threading.Lock()

    def write(self, data: np.ndarray) -> None:
        """Write frames, dropping oldest data on overflow."""
        frames = data.shape[0]
        if frames == 0:
            return
        with self._lock:
            if frames >= self.capacity:
                data = data[-self.capacity:]
                frames = data.shape[0]
            end = self._write + frames
            if end <= self.capacity:
                self._buf[self._write:end] = data
            else:
                first = self.capacity - self._write
                self._buf[self._write:] = data[:first]
                self._buf[:frames - first] = data[first:]
            self._write = (self._write + frames) % self.capacity
            self._count += frames
            if self._count > self.capacity:
                # Overflow: advance read pointer, drop oldest.
                drop = self._count - self.capacity
                self._read = (self._read + drop) % self.capacity
                self._count = self.capacity

    @property
    def available(self) -> int:
        with self._lock:
            return self._count

    def drop(self, frames: int) -> None:
        """Discard the oldest ``frames`` frames (used to bound drift latency)."""
        with self._lock:
            drop = min(frames, self._count)
            self._read = (self._read + drop) % self.capacity
            self._count -= drop

    def read(self, frames: int) -> np.ndarray:
        """Read ``frames`` frames; zero-fills underflow."""
        out = np.zeros((frames, self.channels), dtype=np.float32)
        with self._lock:
            avail = min(frames, self._count)
            if avail:
                end = self._read + avail
                if end <= self.capacity:
                    out[:avail] = self._buf[self._read:end]
                else:
                    first = self.capacity - self._read
                    out[:first] = self._buf[self._read:]
                    out[first:avail] = self._buf[:avail - first]
                self._read = (self._read + avail) % self.capacity
                self._count -= avail
        return out

    def clear(self) -> None:
        with self._lock:
            self._read = self._write = self._count = 0
