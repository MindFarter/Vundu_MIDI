"""Settings stored in data/config.json (created on first run)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

DEFAULTS = {
    "host": "0.0.0.0",
    "port": 5080,
    "udp_midi_port": 5005,
    "sample_rate": 48000,
    "block_size": 256,
    "audio_output": None,      # sounddevice index or name; None = system default
    "audio_input": None,
    "audio_input_enabled": True,
    "input_channel": 0,        # 0 = left / input 1, 1 = right / input 2
    "input_stereo": False,
    "midi_port": None,         # remembered MIDI interface name
    "midi_auto_connect": True,
    "instrument": "keyboard",
    "guitar_source": "audio",
    "sound": "builtin:grand_piano",
    "startup_preset": "factory:studio_grand",
    "plugin_dirs": [],
    "deck_artist": "",         # "Your name" on the tape deck, used in recordings
    "midi_map": None,
    "theme": "amber",
}


class Config(dict):
    def __init__(self, path: Path):
        super().__init__(DEFAULTS)
        self.path = path
        self._lock = threading.Lock()
        if path.exists():
            try:
                self.update(json.loads(path.read_text()))
            except Exception:
                pass
        self.pop("max_slots", None)  # v0.0.1 setting, replaced by a fixed limit of 20

    def save(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(dict(self), indent=2))
            tmp.replace(self.path)

    def public(self) -> dict:
        return dict(self)
