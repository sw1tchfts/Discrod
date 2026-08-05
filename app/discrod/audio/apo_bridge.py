"""Shared-memory bridge to the Discrod capture APO (the driver-free transport).

In *APO mode* the app does not open any audio device.  Instead:

  * The capture APO (``apo/``) runs inside Windows' ``audiodg.exe`` on the real
    microphone endpoint, applies the mic DSP chain, and mixes soundboard audio
    that this bridge feeds it — so Discord, selecting the *real* mic, receives
    processed mic + soundboard with nothing installed as a device.
  * This module is the app side of that contract: it maps the bridge file,
    continuously publishes the mic-channel parameters, and streams the rendered
    soundboard bus into a lock-free ring the APO consumes.

The binary layout is defined once in ``apo/src/bridge_protocol.h``; the offsets
and sizes below mirror it exactly and are covered by
``tests/test_apo_bridge.py`` (a drift in either file fails a static check).

Only ever run one instance: the app is the sole writer of the ring-format
fields, ``write_pos``, and the parameter block; the APO owns ``read_pos`` and
the ``apo_*`` feedback fields.
"""

from __future__ import annotations

import mmap
import os
import struct
import threading

import numpy as np

# --- protocol constants (keep in lockstep with apo/src/bridge_protocol.h) ----

BRIDGE_MAGIC = 0x4F504144      # 'DAPO'
BRIDGE_VERSION = 1

HEADER_OFFSET = 0
PARAMS_OFFSET = 256
RING_OFFSET = 4096

RING_CAPACITY_FRAMES = 16384
RING_MAX_CHANNELS = 2
RING_PRIME_FRAMES = 1024

BRIDGE_FILE_SIZE = RING_OFFSET + RING_CAPACITY_FRAMES * RING_MAX_CHANNELS * 4

EQ_BANDS = 3

# EQ band type codes (must match biquad.py string types via this mapping).
_BAND_CODES = {
    "peak": 0,
    "lowshelf": 1,
    "highshelf": 2,
    "lowpass": 3,
    "highpass": 4,
}

# Header field byte offsets within the mapping.
_OFF_MAGIC = 0
_OFF_VERSION = 4
_OFF_APO_SR = 8
_OFF_APO_CH = 12
_OFF_APO_HEARTBEAT = 16
_OFF_APO_METER = 20
_OFF_RING_SR = 24
_OFF_RING_CH = 28
_OFF_RING_CAP = 32
_OFF_RING_EPOCH = 36
_OFF_WRITE_POS = 40
_OFF_READ_POS = 44
_OFF_PARAM_SEQ = 48

# BridgeParams packing: mirrors struct BridgeParams (all little-endian, 4-byte
# fields).  u = uint32, f = float32.
_PARAMS_FORMAT = "<" + (
    "f"          # mic_gain_db
    "I fffff"    # gate: enabled + threshold/range/attack/hold/release
    "I fffff"    # comp: enabled + threshold/ratio/attack/release/makeup
    "I"          # eq_enabled
    + "Ifff" * EQ_BANDS  # eq bands: type, freq, gain_db, q
    + "II"       # soundboard_enabled, mic_mute
)
PARAMS_SIZE = struct.calcsize(_PARAMS_FORMAT)


def bridge_path() -> str:
    """Path to the shared bridge file (matches the APO's OpenBridge)."""
    if os.name == "nt":
        base = os.environ.get("ProgramData", r"C:\ProgramData")
    else:
        # Dev/test on non-Windows: keep it under a writable dir so the parity
        # and round-trip tests can exercise the exact same code.
        base = os.environ.get("DISCROD_BRIDGE_DIR",
                              os.path.join(os.path.expanduser("~"), ".discrod"))
    folder = os.path.join(base, "Discrod")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "apo_bridge.bin")


def eq_band_code(ftype: str) -> int:
    return _BAND_CODES.get(ftype, 0)


class ApoBridge:
    """Writer side of the APO shared-memory bridge.

    Create it, call :meth:`open` to map/initialize the file, then either drive
    it manually (``publish_params`` + ``write_soundboard``) or hand it to
    :meth:`start_stream` which paces a background render thread.
    """

    def __init__(self, sample_rate: int = 48000, ring_channels: int = 2,
                 path: str | None = None):
        self.sample_rate = int(sample_rate)
        self.ring_channels = max(1, min(RING_MAX_CHANNELS, int(ring_channels)))
        self.path = path or bridge_path()
        self._mm: mmap.mmap | None = None
        self._file = None
        self._write_pos = 0
        self._ring = None            # numpy view over the ring region
        self._param_seq = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # --- lifecycle ----------------------------------------------------------
    def open(self) -> None:
        """Create (or reopen) the bridge file, zero it, then stamp magic last.

        Magic is written only after the rest of the header/params are valid so
        an APO that maps the file mid-initialization sees either "not ready"
        (magic absent) or a fully-formed bridge, never a torn one.
        """
        self.close()
        # Create/truncate to the exact size.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
        os.ftruncate(fd, BRIDGE_FILE_SIZE)
        self._file = fd
        self._mm = mmap.mmap(fd, BRIDGE_FILE_SIZE)
        self._mm[:] = b"\x00" * BRIDGE_FILE_SIZE

        self._ring = np.frombuffer(
            self._mm, dtype=np.float32, count=RING_CAPACITY_FRAMES * RING_MAX_CHANNELS,
            offset=RING_OFFSET,
        ).reshape(RING_CAPACITY_FRAMES, RING_MAX_CHANNELS)

        self._write_pos = 0
        self._param_seq = 0
        self._put_u32(_OFF_VERSION, BRIDGE_VERSION)
        self._put_u32(_OFF_RING_SR, self.sample_rate)
        self._put_u32(_OFF_RING_CH, self.ring_channels)
        self._put_u32(_OFF_RING_CAP, RING_CAPACITY_FRAMES)
        self._put_u32(_OFF_RING_EPOCH, 1)
        self._put_u32(_OFF_WRITE_POS, 0)
        self._put_u32(_OFF_READ_POS, 0)
        self._put_u32(_OFF_PARAM_SEQ, 0)
        # Magic last: publishes readiness.
        self._put_u32(_OFF_MAGIC, BRIDGE_MAGIC)

    def close(self) -> None:
        self.stop_stream()
        if self._mm is not None:
            try:
                self._put_u32(_OFF_MAGIC, 0)  # mark not-ready before unmapping
                self._mm.flush()
            except (ValueError, OSError):
                pass
            # Drop the numpy view first: it exports a pointer into the mapping,
            # and mmap.close() raises BufferError while any such view is alive.
            self._ring = None
            self._mm.close()
            self._mm = None
        if self._file is not None:
            try:
                os.close(self._file)
            except OSError:
                pass
            self._file = None

    def __enter__(self) -> "ApoBridge":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- header helpers -----------------------------------------------------
    def _put_u32(self, off: int, value: int) -> None:
        struct.pack_into("<I", self._mm, off, value & 0xFFFFFFFF)

    def _get_u32(self, off: int) -> int:
        return struct.unpack_from("<I", self._mm, off)[0]

    def _get_f32(self, off: int) -> float:
        return struct.unpack_from("<f", self._mm, off)[0]

    # --- APO -> app feedback -------------------------------------------------
    @property
    def apo_loaded(self) -> bool:
        """True when an APO has attached and published its stream format."""
        return self._mm is not None and self._get_u32(_OFF_APO_SR) != 0

    @property
    def apo_sample_rate(self) -> int:
        return self._get_u32(_OFF_APO_SR) if self._mm is not None else 0

    @property
    def apo_heartbeat(self) -> int:
        return self._get_u32(_OFF_APO_HEARTBEAT) if self._mm is not None else 0

    @property
    def apo_meter(self) -> float:
        return self._get_f32(_OFF_APO_METER) if self._mm is not None else 0.0

    @property
    def read_pos(self) -> int:
        return self._get_u32(_OFF_READ_POS) if self._mm is not None else 0

    @property
    def write_pos(self) -> int:
        return self._write_pos

    def ring_fill(self) -> int:
        """Frames written but not yet consumed by the APO (u32 wrap-safe)."""
        return (self._write_pos - self.read_pos) & 0xFFFFFFFF

    # --- app -> APO: parameters ---------------------------------------------
    def publish_params(self, engine) -> None:
        """Snapshot the engine's mic channel + soundboard state into the params
        block under the seqlock.  Safe to call from the UI thread."""
        mic = engine.mic_channel
        gate = mic.gate
        comp = mic.compressor
        eq = mic.eq
        bands = list(eq.bands)[:EQ_BANDS]
        while len(bands) < EQ_BANDS:
            bands.append(None)

        values = [
            float(mic.gain_db),
            1 if gate.enabled else 0,
            float(gate.threshold_db), float(gate.range_db),
            float(gate.attack_ms), float(gate.hold_ms), float(gate.release_ms),
            1 if comp.enabled else 0,
            float(comp.threshold_db), float(comp.ratio),
            float(comp.attack_ms), float(comp.release_ms), float(comp.makeup_db),
            1 if eq.enabled else 0,
        ]
        for b in bands:
            if b is None:
                values += [0, 1000.0, 0.0, 1.0]
            else:
                values += [eq_band_code(b.ftype), float(b.freq),
                           float(b.gain_db), float(b.q)]
        # Mix soundboard into the APO output unless the soundboard bus is muted;
        # replace the mic signal with silence when the mic channel is muted.
        values.append(0 if engine.soundboard_channel.mute else 1)  # soundboard_enabled
        values.append(1 if mic.mute else 0)                        # mic_mute

        blob = struct.pack(_PARAMS_FORMAT, *values)
        # Seqlock: odd during the write, even when consistent.
        self._param_seq += 1
        self._put_u32(_OFF_PARAM_SEQ, self._param_seq)   # -> odd
        self._mm[PARAMS_OFFSET:PARAMS_OFFSET + PARAMS_SIZE] = blob
        self._param_seq += 1
        self._put_u32(_OFF_PARAM_SEQ, self._param_seq)   # -> even

    # --- app -> APO: soundboard audio ---------------------------------------
    def write_soundboard(self, block: np.ndarray) -> int:
        """Append one block of soundboard audio to the ring.

        ``block`` is ``(frames, channels)`` float32.  Returns frames written.
        The audio is copied into the ring *before* ``write_pos`` advances so the
        APO (which reads with acquire ordering on ``write_pos``) never sees data
        it hasn't been told about — the standard SPSC publish on x86-64.
        """
        if self._mm is None or block.size == 0:
            return 0
        frames = block.shape[0]
        data = block
        if data.shape[1] != self.ring_channels:
            data = _fit_channels(data, self.ring_channels)
        data = np.ascontiguousarray(data, dtype=np.float32)

        start = self._write_pos % RING_CAPACITY_FRAMES
        first = min(frames, RING_CAPACITY_FRAMES - start)
        self._ring[start:start + first, :self.ring_channels] = data[:first]
        if first < frames:
            rest = frames - first
            self._ring[0:rest, :self.ring_channels] = data[first:first + rest]

        self._write_pos = (self._write_pos + frames) & 0xFFFFFFFF
        self._put_u32(_OFF_WRITE_POS, self._write_pos)
        return frames

    # --- paced streaming thread ---------------------------------------------
    def start_stream(self, engine, block_size: int = 256,
                     target_fill: int | None = None) -> None:
        """Spawn a thread that renders the soundboard bus and feeds the ring.

        Pacing is closed-loop on the ring fill rather than a free-running
        wall-clock timer: each tick tops the ring up to ``target_fill`` frames,
        so the APO's real consumption sets the rate and Python scheduler jitter
        cannot make the app run away from (or starve) the endpoint clock.
        """
        if self._thread is not None:
            return
        if target_fill is None:
            target_fill = RING_PRIME_FRAMES + 2 * block_size
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._stream_loop, args=(engine, block_size, target_fill),
            name="apo-bridge", daemon=True)
        self._thread.start()

    def stop_stream(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.0)
        self._thread = None

    def _stream_loop(self, engine, block_size: int, target_fill: int) -> None:
        interval = block_size / float(self.sample_rate)
        while not self._stop.is_set():
            self.publish_params(engine)
            # Produce until the ring holds target_fill frames of slack.
            guard = 0
            while self.ring_fill() < target_fill and guard < 64:
                block = engine.render_soundboard(block_size)
                self.write_soundboard(block)
                guard += 1
            self._stop.wait(interval)


def _fit_channels(data: np.ndarray, channels: int) -> np.ndarray:
    if data.shape[1] == channels:
        return data
    if data.shape[1] == 1:
        return np.repeat(data, channels, axis=1)
    if channels == 1:
        return data.mean(axis=1, keepdims=True)
    return np.repeat(data[:, :1], channels, axis=1)
