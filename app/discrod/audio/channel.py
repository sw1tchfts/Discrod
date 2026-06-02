"""A mixer channel: a labelled bus with gain/mute/solo and a DSP chain.

Each channel owns an ordered list of processors (gate -> compressor -> EQ ->
gain by convention).  The engine writes the channel's summed source audio into a
scratch buffer, calls :meth:`process`, then adds the result to the master bus.
This is what lets every audio source be manipulated independently.
"""

from __future__ import annotations

import numpy as np

from .dsp import Gain, Equalizer, Gate, Compressor, Processor, PROCESSOR_TYPES

# Channel kinds.
KIND_MIC = "mic"
KIND_SOUNDBOARD = "soundboard"


class Channel:
    def __init__(self, name: str, sample_rate: int, channels: int = 2,
                 kind: str = KIND_SOUNDBOARD):
        self.name = name
        self.kind = kind
        self.sample_rate = sample_rate
        self.channels = channels
        self.mute = False
        self.solo = False

        # Default chain order: gate -> compressor -> eq -> gain (gain last so it
        # is a clean output trim).  All but gain are disabled by default.
        self.gate = Gate(sample_rate)
        self.compressor = Compressor(sample_rate)
        self.eq = Equalizer(sample_rate, channels=channels)
        self.gain = Gain(sample_rate, gain_db=0.0)
        self.chain: list[Processor] = [self.gate, self.compressor, self.eq, self.gain]

        # Peak meter value (linear) for the UI, updated each block.
        self.meter = 0.0

    @property
    def gain_db(self) -> float:
        return self.gain.gain_db

    @gain_db.setter
    def gain_db(self, value: float) -> None:
        self.gain.gain_db = float(value)

    def set_sample_rate(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        for proc in self.chain:
            proc.set_sample_rate(sample_rate)

    def set_channels(self, channels: int) -> None:
        self.channels = channels
        self.eq.set_channels(channels)

    def reset(self) -> None:
        for proc in self.chain:
            proc.reset()

    def process(self, block: np.ndarray) -> None:
        """Apply the chain in place and update the meter.  Honors mute."""
        if self.mute:
            block[:] = 0.0
            self.meter = 0.0
            return
        for proc in self.chain:
            proc.process(block)
        # Cheap peak meter with simple decay handled UI-side.
        if block.size:
            self.meter = float(np.max(np.abs(block)))

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "mute": self.mute,
            "solo": self.solo,
            "chain": [p.to_dict() for p in self.chain],
        }

    def load_dict(self, data: dict) -> None:
        self.name = data.get("name", self.name)
        self.kind = data.get("kind", self.kind)
        self.mute = bool(data.get("mute", False))
        self.solo = bool(data.get("solo", False))
        for proc, pdata in zip(self.chain, data.get("chain", [])):
            # Match by type to stay robust to reordering.
            if pdata.get("type") == proc.__class__.__name__:
                proc.load_dict(pdata)
