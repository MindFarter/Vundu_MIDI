"""Builds the Flask application and wires all the parts together."""

from __future__ import annotations

import logging
import platform
import socket
import sys
import time
from pathlib import Path

from flask import Flask, render_template

from . import APP_NAME, __version__
from .api import create_api
from .config import Config
from .engine import AudioEngine
from .midi_input import MidiManager, NetworkMidi
from .plugin_host import MINIHOST_AVAILABLE, MINIHOST_ERROR, MINIHOST_VERSION, PluginLibrary
from .presets import PresetStore

log = logging.getLogger("vundu")

ROOT = Path(__file__).resolve().parent.parent


class VunduApp:
    """Holds every running service; the API talks to this object."""

    def __init__(self, data_dir: Path, overrides: dict | None = None):
        self.root = ROOT
        self.data_dir = data_dir
        self.config = Config(data_dir / "config.json")
        for k, v in (overrides or {}).items():
            if v is not None:
                self.config[k] = v
        self.started = time.time()
        self.plugins = PluginLibrary(data_dir, self.config.get("plugin_dirs"))
        self.presets = PresetStore(ROOT / "presets" / "factory", data_dir / "presets")
        self.recordings_dir = data_dir / "recordings"
        self.engine = AudioEngine(self.config, self.plugins, self.recordings_dir)
        self.midi = MidiManager(self.engine.handle_midi, self.config.get("midi_auto_connect", True),
                                self.config.get("midi_port"))
        self.net_midi = NetworkMidi(self.midi, int(self.config.get("udp_midi_port") or 0))
        self.remotes: dict[str, dict] = {}
        self.engine.on_program_change = self._program_change

    # ------------------------------------------------------------------
    def start(self):
        self.engine.start()
        startup = self.config.get("startup_preset")
        if startup:
            try:
                self.load_preset(startup)
            except Exception as exc:
                log.warning("startup preset %s not loaded: %s", startup, exc)
        self.midi.start()
        if self.config.get("midi_port"):
            try:
                self.midi.connect(self.config["midi_port"])
            except Exception:
                pass
        self.net_midi.start()

    def shutdown(self):
        if self.engine.recorder.active:
            self.engine.recorder.stop()
        self.midi.stop()
        self.net_midi.stop()
        self.engine.stop()
        self.save_config()

    def save_config(self):
        self.config["midi_port"] = self.midi.preferred
        self.config["midi_map"] = self.engine.midi_map
        try:
            self.config.save()
        except Exception as exc:
            log.warning("could not save config: %s", exc)

    def load_preset(self, preset_id: str) -> dict:
        preset = self.presets.get(preset_id)
        errors = self.engine.apply_preset(preset)
        return {"preset": {"id": preset["id"], "name": preset["name"]}, "errors": errors}

    def _program_change(self, program: int):
        p = self.presets.by_index(program)
        self.load_preset(p["id"])

    def system_info(self) -> dict:
        return {"app": APP_NAME, "version": __version__, "python": sys.version.split()[0],
                "platform": platform.platform(), "machine": platform.machine(),
                "hostname": socket.gethostname(), "lan_ip": lan_ip(),
                "uptime_s": round(time.time() - self.started),
                "minihost": {"available": MINIHOST_AVAILABLE, "version": MINIHOST_VERSION,
                             "error": MINIHOST_ERROR,
                             "project": "https://github.com/shakfu/minihost"}}


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def create_app(data_dir: Path, overrides: dict | None = None, start: bool = True):
    vapp = VunduApp(data_dir, overrides)
    app = Flask(__name__, template_folder=str(ROOT / "web" / "templates"),
                static_folder=str(ROOT / "web" / "static"), static_url_path="/static")
    app.config["JSON_SORT_KEYS"] = False
    app.extensions["vundu"] = vapp
    app.register_blueprint(create_api(vapp), url_prefix="/api/v1")

    @app.route("/")
    def index():
        return render_template("index.html", version=__version__, app_name=APP_NAME)

    if start:
        vapp.start()
    return app, vapp
