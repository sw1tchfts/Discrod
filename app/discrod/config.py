"""Application configuration: persistence of devices, channels and pad mappings.

Config is stored as JSON in the user's config directory:
  * Windows: %APPDATA%/Discrod/config.json
  * macOS:   ~/Library/Application Support/Discrod/config.json
  * Linux:   ~/.config/Discrod/config.json
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field


APP_NAME = "Discrod"


def config_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


@dataclass
class AppConfig:
    input_device: object = None       # PortAudio device index or name
    output_device: object = None      # should point at the virtual cable render
    monitor_device: object = None     # optional local monitoring output
    midi_port: str | None = None
    sample_rate: int = 48000
    block_size: int = 256
    engine: dict = field(default_factory=dict)   # AudioEngine.to_dict()
    bank: dict = field(default_factory=dict)      # PadBank.to_dict()

    def to_dict(self) -> dict:
        return {
            "input_device": self.input_device,
            "output_device": self.output_device,
            "monitor_device": self.monitor_device,
            "midi_port": self.midi_port,
            "sample_rate": self.sample_rate,
            "block_size": self.block_size,
            "engine": self.engine,
            "bank": self.bank,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AppConfig":
        return cls(
            input_device=data.get("input_device"),
            output_device=data.get("output_device"),
            monitor_device=data.get("monitor_device"),
            midi_port=data.get("midi_port"),
            sample_rate=int(data.get("sample_rate", 48000)),
            block_size=int(data.get("block_size", 256)),
            engine=data.get("engine", {}),
            bank=data.get("bank", {}),
        )


def load_config(path: str | None = None) -> AppConfig:
    path = path or config_path()
    if not os.path.exists(path):
        return AppConfig()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return AppConfig.from_dict(json.load(fh))
    except (json.JSONDecodeError, OSError):
        return AppConfig()


def save_config(cfg: AppConfig, path: str | None = None) -> None:
    path = path or config_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg.to_dict(), fh, indent=2)
    os.replace(tmp, path)
