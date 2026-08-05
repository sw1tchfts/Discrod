"""Pad mappings: bind a MIDI note to an audio clip + target channel + mode.

The :class:`PadBank` holds all mappings and is serialized into the app config.
:class:`Controller` wires MIDI events to the audio engine using the bank.
"""

from __future__ import annotations

import threading

from dataclasses import dataclass, field, asdict

from .audio.clip import Clip, MODE_ONESHOT
from .midi.input import NOTE_ON, NOTE_OFF


@dataclass
class PadMapping:
    note: int
    clip_path: str
    channel: str = "Soundboard"
    mode: str = MODE_ONESHOT
    gain_db: float = 0.0
    label: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PadMapping":
        return cls(
            note=int(data["note"]),
            clip_path=data["clip_path"],
            channel=data.get("channel", "Soundboard"),
            mode=data.get("mode", MODE_ONESHOT),
            gain_db=float(data.get("gain_db", 0.0)),
            label=data.get("label", ""),
        )


@dataclass
class PadBank:
    mappings: dict[int, PadMapping] = field(default_factory=dict)

    def set(self, mapping: PadMapping) -> None:
        self.mappings[mapping.note] = mapping

    def remove(self, note: int) -> None:
        self.mappings.pop(note, None)

    def get(self, note: int) -> PadMapping | None:
        return self.mappings.get(note)

    def to_dict(self) -> dict:
        return {"mappings": [m.to_dict() for m in self.mappings.values()]}

    def load_dict(self, data: dict) -> None:
        self.mappings.clear()
        for m in data.get("mappings", []):
            mapping = PadMapping.from_dict(m)
            self.mappings[mapping.note] = mapping


class Controller:
    """Connects MIDI note events to engine clip triggers via a PadBank.

    Clips are loaded lazily and cached by path so repeated triggers are cheap.
    """

    def __init__(self, engine, bank: PadBank):
        self.engine = engine
        self.bank = bank
        self._clip_cache: dict[str, Clip] = {}
        self._cache_lock = threading.Lock()
        # Track which voices were started by which note for gate note-off.
        self._note_keys: dict[int, object] = {}
        # Optional UI hook: called as fn(note, event) for visual feedback.
        self.on_event = None
        # Optional UI hook: called as fn(note_or_None, message) when a clip
        # fails to load.  May fire from a background preload thread.
        self.on_error = None

    def _get_clip(self, path: str) -> Clip:
        # Decode OUTSIDE the lock: holding it across Clip.load would make a
        # cache-hit pad press (GUI thread) stall behind the preload thread's
        # in-flight decode — the exact stall preloading exists to remove.
        for _ in range(3):
            with self._cache_lock:
                clip = self._clip_cache.get(path)
                sample_rate = self.engine.sample_rate
                channels = self.engine.channels
            if clip is not None and clip.sample_rate == sample_rate:
                return clip
            clip = Clip.load(path, sample_rate, channels)
            with self._cache_lock:
                cached = self._clip_cache.get(path)
                if cached is not None and cached.sample_rate == sample_rate:
                    return cached      # concurrent decode won; use one instance
                if self.engine.sample_rate == sample_rate:
                    self._clip_cache[path] = clip
                    return clip
            # Engine sample rate changed mid-decode: loop and decode again.
        return clip

    def invalidate_cache(self) -> None:
        with self._cache_lock:
            self._clip_cache.clear()

    def preload_async(self) -> threading.Thread:
        """Decode every mapped clip on a background thread.

        Without this the first press of each pad decodes (and possibly
        resamples) the file synchronously on the trigger path — a multi-minute
        music clip stalls the GUI and misses the soundboard moment.  Load
        failures are reported through :attr:`on_error` instead of leaving a
        silently dead pad.
        """
        mappings = list(self.bank.mappings.values())

        def work():
            for m in mappings:
                try:
                    self._get_clip(m.clip_path)
                except Exception as exc:
                    if self.on_error:
                        self.on_error(m.note,
                                      f"Pad {m.note}: cannot load "
                                      f"{m.clip_path}: {exc}")
        thread = threading.Thread(target=work, daemon=True,
                                  name="discrod-clip-preload")
        thread.start()
        return thread

    def handle_note(self, event: str, note: int, velocity: int, channel: int) -> None:
        mapping = self.bank.get(note)
        if self.on_event:
            self.on_event(note, event)
        if mapping is None:
            return
        if event == NOTE_ON:
            try:
                clip = self._get_clip(mapping.clip_path)
            except Exception as exc:
                if self.on_error:
                    self.on_error(note,
                                  f"Pad {note}: cannot load "
                                  f"{mapping.clip_path}: {exc}")
                return
            gain = 10.0 ** (mapping.gain_db / 20.0)
            key = ("note", note)
            self.engine.trigger_clip(clip, mapping.channel, mode=mapping.mode,
                                     gain=gain, key=key)
            self._note_keys[note] = key
        elif event == NOTE_OFF:
            key = self._note_keys.get(note)
            if key is not None:
                self.engine.release_clip(key)
