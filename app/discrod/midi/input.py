"""MIDI input handling.

Wraps ``mido`` (with the ``python-rtmidi`` backend) to open an input port and
dispatch note-on / note-off events to a callback.  Runs the receive loop on a
background thread so the UI stays responsive.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

try:
    import mido
except Exception:  # pragma: no cover - headless/dev environments
    mido = None


# Callback signature: (event, note, velocity, channel)
NoteCallback = Callable[[str, int, int, int], None]

NOTE_ON = "note_on"
NOTE_OFF = "note_off"


def list_ports() -> list[str]:
    if mido is None:
        return []
    try:
        return list(mido.get_input_names())
    except Exception:
        return []


def note_name(note: int) -> str:
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    return f"{names[note % 12]}{note // 12 - 1}"


class MidiInput:
    def __init__(self, callback: NoteCallback):
        self._callback = callback
        self._port = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.port_name: Optional[str] = None
        # When set, the next note-on is reported to the learn callback instead of
        # being dispatched normally (used for MIDI-learn binding in the UI).
        self.learn_callback: Optional[Callable[[int], None]] = None

    def open(self, port_name: str) -> None:
        if mido is None:
            raise RuntimeError("mido / python-rtmidi is not available")
        self.close()
        self._port = mido.open_input(port_name)
        self.port_name = port_name
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        port = self._port
        if port is None:
            return
        for msg in port:
            if self._stop.is_set():
                break
            self._handle(msg)

    def _handle(self, msg) -> None:
        if msg.type == "note_on" and msg.velocity > 0:
            if self.learn_callback is not None:
                cb, self.learn_callback = self.learn_callback, None
                cb(msg.note)
                return
            self._callback(NOTE_ON, msg.note, msg.velocity, msg.channel)
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            self._callback(NOTE_OFF, msg.note, 0, msg.channel)

    def arm_learn(self, callback: Callable[[int], None]) -> None:
        """Capture the next note-on and report its note number to ``callback``."""
        self.learn_callback = callback

    def cancel_learn(self) -> None:
        """Disarm a pending MIDI-learn.  A stale armed learn would otherwise
        swallow the first pad press whenever the user next plays."""
        self.learn_callback = None

    def close(self) -> None:
        self._stop.set()
        self.learn_callback = None
        if self._port is not None:
            try:
                self._port.close()
            except Exception:
                pass
        self._port = None
        self._thread = None
        self.port_name = None
