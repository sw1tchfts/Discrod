"""Real-time audio engine.

Responsibilities:
  * Capture the microphone on an input stream into a ring buffer.
  * Mix microphone + active clip voices through per-channel DSP chains.
  * Sum channels to a master bus and write to the output device.

Routing for Discord: set the engine's **output device** to the render side of
our virtual audio driver (``Discrod Virtual Cable``).  Discord then selects the
driver's **capture** endpoint as its microphone, receiving mic + clips with all
processing applied.  Optionally a second output stream can feed local monitoring
(headphones) so you hear what you send.

The class is deliberately backend-thin: it talks to ``sounddevice`` (PortAudio)
but all mixing math is plain numpy so it can be unit-tested without hardware via
:meth:`render_block`.
"""

from __future__ import annotations

import threading

import numpy as np

try:
    import sounddevice as sd
except Exception:  # pragma: no cover - headless/dev environments
    sd = None

from .channel import Channel, KIND_MIC, KIND_SOUNDBOARD
from .clip import Clip, Voice, MODE_ONESHOT, MODE_TOGGLE
from .ringbuffer import RingBuffer


class AudioEngine:
    def __init__(self, sample_rate: int = 48000, block_size: int = 256,
                 channels: int = 2):
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.channels = channels

        self.mic_ring = RingBuffer(sample_rate, channels)  # ~1s of slack

        # Default channels: a mic bus and a soundboard bus.
        self.mic_channel = Channel("Microphone", sample_rate, channels, KIND_MIC)
        self.soundboard_channel = Channel("Soundboard", sample_rate, channels,
                                          KIND_SOUNDBOARD)
        self.channels_list: list[Channel] = [self.mic_channel, self.soundboard_channel]

        self.master_gain_db = 0.0
        self.master_meter = 0.0

        self._voices: list[Voice] = []
        self._voices_lock = threading.Lock()
        # Toggle-mode bookkeeping: maps a trigger key -> live voice.
        self._toggle_voices: dict[object, Voice] = {}

        self._input_stream = None
        self._output_stream = None
        self.input_device = None
        self.output_device = None
        self.running = False

        # Reusable scratch buffers (allocated per device start).
        self._scratch = np.zeros((block_size, channels), dtype=np.float32)
        self._master = np.zeros((block_size, channels), dtype=np.float32)

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

    # --- clip triggering ----------------------------------------------------
    def trigger_clip(self, clip: Clip, channel_name: str, mode: str = MODE_ONESHOT,
                     gain: float = 1.0, key: object = None) -> None:
        """Start (or toggle) playback of ``clip`` on ``channel_name``."""
        with self._voices_lock:
            if mode == MODE_TOGGLE and key is not None:
                live = self._toggle_voices.get(key)
                if live is not None and live.active:
                    live.active = False
                    self._toggle_voices.pop(key, None)
                    return
            voice = Voice(clip, channel_name, mode=mode, gain=gain, key=key)
            self._voices.append(voice)
            if mode == MODE_TOGGLE and key is not None:
                self._toggle_voices[key] = voice

    def release_clip(self, key: object) -> None:
        """Note-off handler: stops gate-mode voices tied to ``key``."""
        with self._voices_lock:
            for v in self._voices:
                if v.key == key:
                    v.note_off()

    def stop_all(self) -> None:
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
            mic_block = self.mic_ring.read(frames)

        any_solo = any(ch.solo for ch in self.channels_list)

        with self._voices_lock:
            voices = self._voices
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
            # Drop finished voices.
            self._voices = [v for v in voices if v.active]
            self._toggle_voices = {k: v for k, v in self._toggle_voices.items()
                                   if v.active}

        g = 10.0 ** (self.master_gain_db / 20.0)
        if g != 1.0:
            out *= g
        np.clip(out, -1.0, 1.0, out=out)
        if out.size:
            self.master_meter = float(np.max(np.abs(out)))
        return out

    # --- PortAudio callbacks ------------------------------------------------
    def _input_callback(self, indata, frames, time_info, status):  # noqa: D401
        self.mic_ring.write(np.asarray(indata, dtype=np.float32))

    def _output_callback(self, outdata, frames, time_info, status):
        block = self.render_block(frames)
        outdata[:] = block

    # --- lifecycle ----------------------------------------------------------
    def start(self, input_device=None, output_device=None) -> None:
        if sd is None:
            raise RuntimeError("sounddevice (PortAudio) is not available")
        if self.running:
            self.stop()
        self.input_device = input_device if input_device is not None else self.input_device
        self.output_device = output_device if output_device is not None else self.output_device
        self.mic_ring.clear()

        if self.input_device is not None:
            self._input_stream = sd.InputStream(
                device=self.input_device, samplerate=self.sample_rate,
                blocksize=self.block_size, channels=self.channels,
                dtype="float32", callback=self._input_callback,
                latency="low",
            )
            self._input_stream.start()

        self._output_stream = sd.OutputStream(
            device=self.output_device, samplerate=self.sample_rate,
            blocksize=self.block_size, channels=self.channels,
            dtype="float32", callback=self._output_callback,
            latency="low",
        )
        self._output_stream.start()
        self.running = True

    def stop(self) -> None:
        self.running = False
        for stream in (self._input_stream, self._output_stream):
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
        self._input_stream = None
        self._output_stream = None

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
    """Return (input_devices, output_devices) as ``[(index, name), ...]``."""
    if sd is None:
        return [], []
    inputs, outputs = [], []
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            inputs.append((idx, dev["name"]))
        if dev["max_output_channels"] > 0:
            outputs.append((idx, dev["name"]))
    return inputs, outputs
