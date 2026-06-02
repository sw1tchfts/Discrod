"""Pad mappings: bind a MIDI note to an audio clip + target channel + mode.

The :class:`PadBank` holds all mappings and is serialized into the app config.
:class:`Controller` wires MIDI events to the audio engine using the bank.
"""

from __future__ import annotations

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
        # Track which voices were started by which note for gate note-off.
        self._note_keys: dict[int, object] = {}
        # Optional UI hook: called as fn(note, event) for visual feedback.
        self.on_event = None

    def _get_clip(self, path: str) -> Clip:
        clip = self._clip_cache.get(path)
        if clip is None:
            clip = Clip.load(path, self.engine.sample_rate, self.engine.channels)
            self._clip_cache[path] = clip
        return clip

    def invalidate_cache(self) -> None:
        self._clip_cache.clear()

    def handle_note(self, event: str, note: int, velocity: int, channel: int) -> None:
        mapping = self.bank.get(note)
        if self.on_event:
            self.on_event(note, event)
        if mapping is None:
            return
        if event == NOTE_ON:
            try:
                clip = self._get_clip(mapping.clip_path)
            except Exception:
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
