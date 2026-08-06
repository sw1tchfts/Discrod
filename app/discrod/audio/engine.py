"""Real-time audio engine.

Responsibilities:
  * Capture the microphone on an input stream into a ring buffer.
  * Mix microphone + active clip voices through per-channel DSP chains.
  * Sum channels to a master bus and write to the output device.

Routing for Discord: set the engine's **output device** to the render side of
a signed virtual cable (e.g. VB-CABLE's ``CABLE Input``; see
:mod:`discrod.audio.cables` for auto-detection).  Discord then selects the
cable's **capture** endpoint as its microphone, receiving mic + clips with all
processing applied.  Optionally a second output stream can feed local monitoring
(headphones) so you hear what you send.

The class is deliberately backend-thin: it talks to ``sounddevice`` (PortAudio)
but all mixing math is plain numpy so it can be unit-tested without hardware via
:meth:`render_block`.

Live-stream design notes:
  * The mic, the output, and the optional monitor run as free-running
    PortAudio streams on independent device clocks, decoupled by ring
    buffers.  Every ring crossing is consumed through a ``_ServoTap``:
    primed against callback jitter, rate-servo-corrected against sustained
    clock slew (virtual cables have sloppy software clocks), drift-bounded,
    and declicked at every gap edge.
  * ``render_block`` snapshots the voice list under ``_voices_lock`` and runs
    all DSP *outside* the lock, so a MIDI pad press never waits on a full
    render and the audio callback never blocks on the UI thread beyond the
    microsecond-scale list copy.
"""

from __future__ import annotations

import threading

import numpy as np

try:
    import sounddevice as sd
except Exception:  # pragma: no cover - headless/dev environments
    sd = None

from .channel import Channel, KIND_MIC, KIND_SOUNDBOARD
from .clip import Clip, Voice, MODE_ONESHOT, MODE_LOOP, MODE_TOGGLE
from .ringbuffer import RingBuffer


class _ServoTap:
    """Primed, servo-rate-corrected, declicked consumer of a RingBuffer.

    Bridges two free-running audio callbacks on independent clocks. Physical
    devices drift by ppm, but virtual cables (VB-CABLE, Steam Streaming
    Microphone) run software clocks that can slew their *real* rate well
    beyond that and deliver callbacks in bursts. No fixed-ratio consumer
    survives a sustained slew — the ring must eventually gap (click) or drop
    (click). Three defenses, applied to every ring crossing:

    * A priming cushion so occupancy never hovers at the threshold where
      every callback race is an underrun.
    * An occupancy servo: the consume ratio is continuously nudged (within
      1±span, via linear-interpolation resampling) to hold the ring near its
      prime level, turning sustained clock mismatch into an inaudibly small
      pitch offset instead of periodic gaps.
    * Declicked edges for whatever still gets through: underruns fade out
      across the remaining frames, and the first block after a prime or a
      drift-drop fades in.
    """

    def __init__(self, channels: int, prime_frames: int, max_fill: int,
                 fade: int, gain: float, alpha: float, span: float):
        self.channels = channels
        self.prime_frames = prime_frames
        self.max_fill = max_fill
        self.fade = fade
        self.gain = gain
        self.alpha = alpha
        self.span = span
        self.underruns = 0
        self.drops = 0
        self.reset()

    def reset(self) -> None:
        self.primed = False
        self.fade_in = True
        self.ratio = 1.0
        self.phase = 0.0
        self.carry = np.zeros((2, self.channels), dtype=np.float32)

    def reset_counters(self) -> None:
        self.underruns = 0
        self.drops = 0

    def read(self, ring: RingBuffer, frames: int) -> np.ndarray:
        ch = self.channels
        avail = ring.available
        if not self.primed:
            if avail < self.prime_frames:
                return np.zeros((frames, ch), dtype=np.float32)
            self.primed = True
            self.fade_in = True
            self.ratio = 1.0
            self.phase = 0.0
            self.carry = np.zeros((2, ch), dtype=np.float32)
        if avail > self.max_fill:
            ring.drop(avail - self.prime_frames)
            avail = self.prime_frames
            self.fade_in = True  # dropped frames = discontinuity
            self.drops += 1

        # Servo: occupancy error -> smoothed, clamped consume ratio.
        err = (avail - self.prime_frames) / float(self.prime_frames)
        err = max(-1.0, min(1.0, err))
        target = 1.0 + err * self.gain
        ratio = self.ratio + self.alpha * (target - self.ratio)
        ratio = max(1.0 - self.span, min(1.0 + self.span, ratio))
        self.ratio = ratio

        # Fractional-phase linear resampler. Positions are measured in input
        # frames relative to the older carry sample; the phase lives in
        # (ratio-1, 1+ratio) across blocks, hence the two-frame carry.
        pos = self.phase + ratio * np.arange(frames, dtype=np.float64)
        idx = np.floor(pos).astype(np.int64)
        n_read = max(int(idx[-1]) + 1, 0)
        if avail < n_read:
            # Underrun: emit the remainder as a fade-out, then silence, and
            # re-enter priming for one clean gap instead of edge-y crackle.
            self.primed = False
            self.underruns += 1
            out = np.zeros((frames, ch), dtype=np.float32)
            if avail > 0:
                tail = ring.read(avail)
                ramp = np.linspace(1.0, 0.0, avail, dtype=np.float32)
                out[:avail] = tail * ramp[:, None]
            self.fade_in = True
            return out
        xs = (ring.read(n_read) if n_read
              else np.zeros((0, ch), dtype=np.float32))
        arr = np.vstack([self.carry, xs])
        rows = idx + 1  # carry rows sit at input positions -1 and 0
        frac = (pos - idx).astype(np.float32)[:, None]
        block = arr[rows] * (1.0 - frac) + arr[rows + 1] * frac
        self.carry = arr[-2:].copy()
        self.phase = self.phase + ratio * frames - n_read
        if self.fade_in:
            n = min(self.fade, frames)
            ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
            block[:n] = block[:n] * ramp[:, None]
            self.fade_in = False
        return block.astype(np.float32, copy=False)


class AudioEngine:
    #: Mic ring slack (in blocks) before the output starts consuming. The
    #: consumer is the virtual cable's callback, whose pacing is at the cable
    #: driver's mercy (Steam Streaming Microphone in particular is bursty),
    #: so this needs real depth — ~43 ms at 256/48k, still fine for live
    #: speech on top of Discord's own buffering.
    MIC_PRIME_BLOCKS = 8
    #: Monitor ring slack (in blocks). Deeper still: monitoring tolerates
    #: latency (~64 ms) that the live path can't.
    MONITOR_PRIME_BLOCKS = 12
    #: Occupancy (in multiples of the prime level) beyond which the backlog is
    #: dropped to re-bound latency after clock drift.
    MAX_FILL_FACTOR = 3
    #: Declick ramp length (frames) applied entering/leaving transport gaps.
    DECLICK_FADE = 128
    #: Occupancy-servo tuning shared by both ring consumers. GAIN maps a
    #: rail-to-rail occupancy error onto a ratio offset, ALPHA is the
    #: one-pole smoothing per block, RANGE clamps the ratio to 1±RANGE.
    #: ±5% covers even a virtual cable's software-clock slew; real
    #: corrections sit far below the audibility threshold.
    SERVO_GAIN = 0.05
    SERVO_ALPHA = 0.02
    SERVO_RANGE = 0.05

    def __init__(self, sample_rate: int = 48000, block_size: int = 256,
                 channels: int = 2):
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.channels = channels

        # Both ring crossings (mic capture -> cable callback, cable callback
        # -> monitor callback) go through servo taps: see _ServoTap.
        self.mic_ring = RingBuffer(sample_rate, channels)  # ~1s of slack
        self._mic_tap = _ServoTap(
            channels, self.MIC_PRIME_BLOCKS * block_size,
            self.MIC_PRIME_BLOCKS * block_size * self.MAX_FILL_FACTOR,
            self.DECLICK_FADE, self.SERVO_GAIN, self.SERVO_ALPHA,
            self.SERVO_RANGE)

        # Local monitoring: a second output (headphones) fed the soundboard
        # mix at exactly the level it enters the virtual mic, optionally
        # including the processed mic (monitor_mic) to audition the FX chain.
        self.monitor_ring = RingBuffer(sample_rate, channels)
        self._monitor_tap = _ServoTap(
            channels, self.MONITOR_PRIME_BLOCKS * block_size,
            self.MONITOR_PRIME_BLOCKS * block_size * self.MAX_FILL_FACTOR,
            self.DECLICK_FADE, self.SERVO_GAIN, self.SERVO_ALPHA,
            self.SERVO_RANGE)
        self.monitor_enabled = False
        self.monitor_mic = False

        # Default channels: a mic bus and a soundboard bus.
        self.mic_channel = Channel("Microphone", sample_rate, channels, KIND_MIC)
        self.soundboard_channel = Channel("Soundboard", sample_rate, channels,
                                          KIND_SOUNDBOARD)
        self.channels_list: list[Channel] = [self.mic_channel, self.soundboard_channel]

        self.master_gain_db = 0.0
        self.master_meter = 0.0

        self._voices: list[Voice] = []
        self._voices_lock = threading.Lock()
        # Toggle/loop bookkeeping: maps a trigger key -> live voice.
        self._toggle_voices: dict[object, Voice] = {}

        self._input_stream = None
        self._output_stream = None
        self._monitor_stream = None
        self.input_device = None
        self.output_device = None
        self.monitor_device = None
        self.running = False
        # Invoked (from the thread calling start()) when device negotiation
        # changes the engine sample rate; clip caches must be invalidated.
        self.on_sample_rate_changed = None

    # --- channel management -------------------------------------------------
    def get_channel(self, name: str) -> Channel | None:
        for ch in self.channels_list:
            if ch.name == name:
                return ch
        return None

    def add_channel(self, name: str) -> Channel:
        ch = Channel(name, self.sample_rate, self.channels, KIND_SOUNDBOARD)
        self.channels_list.append(ch)
        return ch

    def set_sample_rate(self, sample_rate: int) -> None:
        sample_rate = int(sample_rate)
        if sample_rate == self.sample_rate:
            return
        self.sample_rate = sample_rate
        for ch in self.channels_list:
            ch.set_sample_rate(sample_rate)
        self.mic_ring = RingBuffer(sample_rate, self.channels)
        self._mic_tap.reset()
        self.monitor_ring = RingBuffer(sample_rate, self.channels)
        self._monitor_tap.reset()
        if self.on_sample_rate_changed is not None:
            self.on_sample_rate_changed(sample_rate)

    # --- clip triggering ----------------------------------------------------
    def trigger_clip(self, clip: Clip, channel_name: str, mode: str = MODE_ONESHOT,
                     gain: float = 1.0, key: object = None) -> None:
        """Start (or toggle/restart) playback of ``clip`` on ``channel_name``.

        * toggle & loop: a second press with the same ``key`` stops the live
          voice (with a fade) instead of stacking another copy.
        * oneshot & gate: re-triggering fades the previous voice for that key
          and restarts from the beginning, per the documented pad semantics.
        A ``channel_name`` that doesn't resolve falls back to the soundboard
        bus, so a stale mapping still plays instead of leaking a silent voice.
        """
        if self.get_channel(channel_name) is None:
            channel_name = self.soundboard_channel.name
        with self._voices_lock:
            if key is not None:
                if mode in (MODE_TOGGLE, MODE_LOOP):
                    live = self._toggle_voices.get(key)
                    if live is not None and live.active:
                        live.stop()
                        self._toggle_voices.pop(key, None)
                        return
                else:
                    for v in self._voices:
                        if v.active and v.key == key:
                            v.stop()
            voice = Voice(clip, channel_name, mode=mode, gain=gain, key=key)
            self._voices.append(voice)
            if mode in (MODE_TOGGLE, MODE_LOOP) and key is not None:
                self._toggle_voices[key] = voice

    def release_clip(self, key: object) -> None:
        """Note-off handler: stops gate-mode voices tied to ``key``."""
        with self._voices_lock:
            for v in self._voices:
                if v.key == key:
                    v.note_off()

    def stop_all(self) -> None:
        with self._voices_lock:
            for v in self._voices:
                v.stop()
            self._toggle_voices.clear()

    def _clear_voices(self) -> None:
        """Hard reset: drop queued/lingering voices without rendering them.

        Pads pressed while the engine is stopped would otherwise pile up and
        all blast simultaneously on the first callback after Start.
        """
        with self._voices_lock:
            self._voices.clear()
            self._toggle_voices.clear()

    # --- core mixing (hardware-independent, unit-testable) ------------------
    def render_block(self, frames: int, mic_block: np.ndarray | None = None,
                     out: np.ndarray | None = None) -> np.ndarray:
        """Mix one block and return ``(frames, channels)``.

        ``mic_block`` overrides the mic ring (used by tests).  In live operation
        the mic comes from the ring buffer.
        """
        if out is None or out.shape[0] != frames:
            out = np.zeros((frames, self.channels), dtype=np.float32)
        else:
            out[:] = 0.0

        if mic_block is None:
            mic_block = self._read_mic(frames)

        any_solo = any(ch.solo for ch in self.channels_list)

        # Monitor mix: the same processed channel blocks that feed the master,
        # restricted to the soundboard buses (plus the mic when monitor_mic),
        # so the headphone level tracks the virtual-mic level exactly.
        monitor = None
        if self.monitor_enabled:
            monitor = np.zeros((frames, self.channels), dtype=np.float32)

        # Snapshot under the lock, render outside it: the DSP work below is the
        # expensive part and must not serialize against trigger_clip on the
        # UI/MIDI path (priority inversion into the audio callback).
        with self._voices_lock:
            voices = list(self._voices)

        for ch in self.channels_list:
            scratch = np.zeros((frames, self.channels), dtype=np.float32)
            if ch.kind == KIND_MIC:
                scratch += mic_block
            # Mix voices routed to this channel.
            for v in voices:
                if v.active and v.channel_name == ch.name:
                    v.render_into(scratch)
            ch.process(scratch)
            if (not any_solo) or ch.solo:
                if not ch.mute:
                    out += scratch
                    if monitor is not None and (ch.kind != KIND_MIC
                                                or self.monitor_mic):
                        monitor += scratch

        # Drop finished voices.
        with self._voices_lock:
            self._voices = [v for v in self._voices if v.active]
            self._toggle_voices = {k: v for k, v in self._toggle_voices.items()
                                   if v.active}

        g = 10.0 ** (self.master_gain_db / 20.0)
        if g != 1.0:
            out *= g
        np.clip(out, -1.0, 1.0, out=out)
        if out.size:
            self.master_meter = float(np.max(np.abs(out)))
        if monitor is not None:
            # Mirror the master bus exactly (gain + clip) before handing the
            # block to the monitor stream's ring.
            if g != 1.0:
                monitor *= g
            np.clip(monitor, -1.0, 1.0, out=monitor)
            self.monitor_ring.write(monitor)
        return out

    def _read_mic(self, frames: int) -> np.ndarray:
        """Consume mic frames through the mic servo tap (see _ServoTap)."""
        return self._mic_tap.read(self.mic_ring, frames)

    def _read_monitor(self, frames: int) -> np.ndarray:
        """Consume monitor frames through its servo tap (see _ServoTap)."""
        return self._monitor_tap.read(self.monitor_ring, frames)

    # Glitch diagnostics, surfaced in the UI status bar.
    @property
    def mic_underruns(self) -> int:
        return self._mic_tap.underruns

    @property
    def mic_drops(self) -> int:
        return self._mic_tap.drops

    @property
    def monitor_underruns(self) -> int:
        return self._monitor_tap.underruns

    @property
    def monitor_drops(self) -> int:
        return self._monitor_tap.drops

    # --- PortAudio callbacks ------------------------------------------------
    def _input_callback(self, indata, frames, time_info, status):  # noqa: D401
        data = np.asarray(indata, dtype=np.float32)
        if data.ndim == 1:
            data = data[:, None]
        if data.shape[1] < self.channels:
            # Mono (or narrower) capture device: duplicate to the bus width.
            data = np.repeat(data[:, :1], self.channels, axis=1)
        elif data.shape[1] > self.channels:
            data = data[:, :self.channels]
        self.mic_ring.write(data)

    def _output_callback(self, outdata, frames, time_info, status):
        block = self.render_block(frames)
        outdata[:] = block

    def _monitor_callback(self, outdata, frames, time_info, status):
        outdata[:] = self._read_monitor(frames)

    # --- lifecycle ----------------------------------------------------------
    def start(self, input_device=None, output_device=None,
              monitor_device=None) -> None:
        if sd is None:
            raise RuntimeError("sounddevice (PortAudio) is not available")
        if self.running:
            self.stop()
        # Assign unconditionally: None means "no input" / "default output" /
        # "no monitor" for THIS start, never "keep whatever device a previous
        # start used".
        self.input_device = input_device
        self.output_device = output_device
        self.monitor_device = monitor_device
        self._clear_voices()

        self.set_sample_rate(self._negotiate_sample_rate())
        in_channels = self._negotiated_input_channels()
        self.mic_ring.clear()
        self._mic_tap.reset()
        self._mic_tap.reset_counters()
        self.monitor_ring.clear()
        self._monitor_tap.reset()
        self._monitor_tap.reset_counters()

        # Construct all streams before starting any, and unwind on any
        # failure — a half-started engine must not leak a live input stream
        # that keeps writing the ring behind our back.
        input_stream = None
        output_stream = None
        monitor_stream = None
        try:
            if self.input_device is not None:
                input_stream = sd.InputStream(
                    device=self.input_device, samplerate=self.sample_rate,
                    blocksize=self.block_size, channels=in_channels,
                    dtype="float32", callback=self._input_callback,
                    latency="low",
                )
            output_stream = sd.OutputStream(
                device=self.output_device, samplerate=self.sample_rate,
                blocksize=self.block_size, channels=self.channels,
                dtype="float32", callback=self._output_callback,
                latency="low",
            )
            if self.monitor_device is not None:
                # latency="high": monitoring tolerates buffering the live
                # cable path can't, and the extra host slack absorbs the
                # cable-callback burstiness that clicks at "low".
                monitor_stream = sd.OutputStream(
                    device=self.monitor_device, samplerate=self.sample_rate,
                    blocksize=self.block_size, channels=self.channels,
                    dtype="float32", callback=self._monitor_callback,
                    latency="high",
                )
            if input_stream is not None:
                input_stream.start()
            # Flip monitor_enabled before the output stream runs so the very
            # first rendered block reaches the monitor ring.
            self.monitor_enabled = monitor_stream is not None
            output_stream.start()
            if monitor_stream is not None:
                monitor_stream.start()
        except Exception:
            self.monitor_enabled = False
            for stream in (input_stream, output_stream, monitor_stream):
                if stream is not None:
                    try:
                        stream.stop()
                        stream.close()
                    except Exception:
                        pass
            raise
        self._input_stream = input_stream
        self._output_stream = output_stream
        self._monitor_stream = monitor_stream
        self.running = True

    def _negotiate_sample_rate(self) -> int:
        """Pick a sample rate both selected devices accept.

        Prefers the engine's configured rate; falls back to the output
        device's native rate (the cable feeds Discord, so it wins).  If no
        probe succeeds the configured rate is returned and the stream open
        reports the real error.
        """
        rate = self.sample_rate
        candidates = [rate]
        try:
            info = sd.query_devices(self.output_device, "output")
            native = int(info["default_samplerate"])
            if native not in candidates:
                candidates.append(native)
        except Exception:
            pass
        for candidate in candidates:
            try:
                sd.check_output_settings(
                    device=self.output_device, samplerate=candidate,
                    channels=self.channels, dtype="float32")
                if self.input_device is not None:
                    sd.check_input_settings(
                        device=self.input_device, samplerate=candidate,
                        channels=self._negotiated_input_channels(),
                        dtype="float32")
                if self.monitor_device is not None:
                    sd.check_output_settings(
                        device=self.monitor_device, samplerate=candidate,
                        channels=self.channels, dtype="float32")
                return candidate
            except Exception:
                continue
        return rate

    def _negotiated_input_channels(self) -> int:
        """Channel count to open the capture device with (mono mics are common
        on Windows; opening them as stereo fails outright)."""
        if self.input_device is None:
            return self.channels
        try:
            info = sd.query_devices(self.input_device, "input")
            max_in = int(info["max_input_channels"])
        except Exception:
            return self.channels
        return max(1, min(self.channels, max_in))

    def stop(self) -> None:
        self.running = False
        self.monitor_enabled = False
        for stream in (self._input_stream, self._output_stream,
                       self._monitor_stream):
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
        self._input_stream = None
        self._output_stream = None
        self._monitor_stream = None

    # --- config -------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "sample_rate": self.sample_rate,
            "block_size": self.block_size,
            "master_gain_db": self.master_gain_db,
            "channels": [ch.to_dict() for ch in self.channels_list],
        }

    def load_dict(self, data: dict) -> None:
        self.master_gain_db = float(data.get("master_gain_db", 0.0))
        saved = {c["name"]: c for c in data.get("channels", [])}
        for ch in self.channels_list:
            if ch.name in saved:
                ch.load_dict(saved[ch.name])
        # Recreate any extra (user-added) channels not in defaults.
        existing = {ch.name for ch in self.channels_list}
        for name, cdata in saved.items():
            if name not in existing:
                ch = self.add_channel(name)
                ch.load_dict(cdata)


def list_devices():
    """Return (input_devices, output_devices) as
    ``[(index, name, hostapi_name), ...]``.

    The host API matters on Windows: PortAudio lists the same physical device
    once per API (MME/DirectSound/WASAPI/WDM-KS) under an identical name, so
    name alone cannot re-identify a selection across sessions.
    """
    if sd is None:
        return [], []
    try:
        hostapis = [api["name"] for api in sd.query_hostapis()]
    except Exception:
        hostapis = []
    inputs, outputs = [], []
    for idx, dev in enumerate(sd.query_devices()):
        api_index = dev.get("hostapi", -1)
        api = hostapis[api_index] if 0 <= api_index < len(hostapis) else ""
        if dev["max_input_channels"] > 0:
            inputs.append((idx, dev["name"], api))
        if dev["max_output_channels"] > 0:
            outputs.append((idx, dev["name"], api))
    return inputs, outputs
