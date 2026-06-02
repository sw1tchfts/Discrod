"""Digital signal processing building blocks for channel strips."""

from .base import Processor
from .gain import Gain, db_to_linear, linear_to_db
from .eq import Equalizer
from .dynamics import Gate, Compressor

#: Registry used to rebuild a processor chain from serialized config.
PROCESSOR_TYPES = {
    cls.__name__: cls for cls in (Gain, Equalizer, Gate, Compressor)
}

__all__ = [
    "Processor", "Gain", "Equalizer", "Gate", "Compressor",
    "db_to_linear", "linear_to_db", "PROCESSOR_TYPES",
]
