"""Plugin hosting through Minihost (https://github.com/shakfu/minihost).

Minihost does the real work here: loading VST3 / LV2 / CLAP (and
AudioUnit on macOS) plugins, scanning, parameters and state. Vundu_MIDI
wraps a loaded plugin so it behaves like one of its built-in effects
inside the console's effect chain.

If Minihost is not installed, Vundu_MIDI still runs with its built-in
effects and sounds; the Plugins menu then explains how to install it.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import numpy as np

log = logging.getLogger("vundu.plugins")

try:  # Minihost is optional at runtime
    import minihost  # type: ignore
    from minihost import plugincache  # type: ignore
    MINIHOST_AVAILABLE = True
    MINIHOST_VERSION = getattr(minihost, "__version__", "unknown")
    MINIHOST_ERROR = ""
except Exception as exc:  # pragma: no cover - depends on the machine
    minihost = None
    plugincache = None
    MINIHOST_AVAILABLE = False
    MINIHOST_VERSION = None
    MINIHOST_ERROR = str(exc)


def default_plugin_dirs() -> list[str]:
    home = Path.home()
    if sys.platform == "darwin":
        dirs = ["/Library/Audio/Plug-Ins/VST3", "/Library/Audio/Plug-Ins/Components",
                "/Library/Audio/Plug-Ins/CLAP", "/Library/Audio/Plug-Ins/LV2",
                home / "Library/Audio/Plug-Ins/VST3", home / "Library/Audio/Plug-Ins/Components",
                home / "Library/Audio/Plug-Ins/CLAP", home / "Library/Audio/Plug-Ins/LV2"]
    elif sys.platform.startswith("win"):
        cpf = os.environ.get("COMMONPROGRAMFILES", r"C:\Program Files\Common Files")
        dirs = [Path(cpf) / "VST3", Path(cpf) / "CLAP", Path(cpf) / "LV2",
                Path(os.environ.get("LOCALAPPDATA", home)) / "Programs/Common/VST3"]
    else:  # Linux, including Raspberry Pi OS
        dirs = [home / ".vst3", home / ".lv2", home / ".clap",
                "/usr/lib/vst3", "/usr/local/lib/vst3", "/usr/lib/lv2", "/usr/local/lib/lv2",
                "/usr/lib/clap", "/usr/local/lib/clap",
                "/usr/lib/aarch64-linux-gnu/lv2", "/usr/lib/x86_64-linux-gnu/lv2",
                "/usr/lib/aarch64-linux-gnu/vst3", "/usr/lib/x86_64-linux-gnu/vst3"]
    return [str(d) for d in dirs]


class PluginEffect:
    """A Minihost plugin dressed up as a chain slot."""

    kind = "plugin"

    def __init__(self, info: dict, sr: float, block: int, instrument: bool = False):
        if not MINIHOST_AVAILABLE:
            raise RuntimeError("Minihost is not installed")
        self.info = info
        self.path = info["path"]
        self.sr = sr
        self.block = block
        self.instrument = instrument
        self.slot_id = uuid.uuid4().hex[:8]
        self.enabled = True
        self.meter_gr_db = 0.0
        self.TYPE = "plugin"
        self.NAME = info.get("name") or Path(self.path).stem
        self.COLOR = "#f783ac" if instrument else "#ff922b"
        ins = 0 if instrument and not info.get("num_inputs") else 2
        try:
            self.plugin = minihost.Plugin(self.path, sample_rate=sr, max_block_size=max(block, 4096),
                                          in_channels=ins, out_channels=2)
        except Exception:
            self.plugin = minihost.Plugin(self.path, sample_rate=sr, max_block_size=max(block, 4096))
        self.in_ch = self.plugin.num_input_channels
        self.out_ch = max(1, self.plugin.num_output_channels)
        self.accepts_midi = bool(self.plugin.accepts_midi)
        self._param_cache = None

    # --- params -----------------------------------------------------------
    def params_list(self, limit: int | None = None) -> list[dict]:
        out = []
        n = self.plugin.num_params
        for i in range(n if limit is None else min(n, limit)):
            try:
                inf = self.plugin.get_param_info(i)
            except Exception:
                continue
            val = self.plugin.get_param(i)
            out.append({"id": str(i), "name": inf.get("name") or f"P{i}", "min": 0.0, "max": 1.0,
                        "default": inf.get("default_value", 0.0), "unit": inf.get("label", ""),
                        "value": val, "text": inf.get("current_value_str", ""),
                        **({"step": 1} if inf.get("is_boolean") else {})})
        return out

    @property
    def params(self) -> dict:
        return {p["id"]: p["value"] for p in self.params_list()}

    def set_param(self, pid, value):
        self.plugin.set_param(int(pid), float(min(max(float(value), 0.0), 1.0)))

    def set_params(self, params: dict):
        for k, v in (params or {}).items():
            try:
                self.set_param(k, v)
            except Exception:
                pass

    # --- audio ------------------------------------------------------------
    def process(self, x: np.ndarray, midi: list | None = None) -> np.ndarray:
        if not self.enabled:
            return x
        n = x.shape[1]
        if self.in_ch == 0:
            inp = np.zeros((0, n), dtype=np.float32)
        elif self.in_ch == 1:
            inp = np.ascontiguousarray(x.mean(axis=0, keepdims=True), dtype=np.float32)
        else:
            inp = np.zeros((self.in_ch, n), dtype=np.float32)
            inp[:2] = x[:2]
        out = np.zeros((self.out_ch, n), dtype=np.float32)
        try:
            if self.accepts_midi and midi:
                self.plugin.process_midi(inp, out, midi)
            else:
                self.plugin.process(inp, out)
        except Exception as exc:  # never let a plugin kill the audio thread
            log.warning("plugin %s failed: %s", self.NAME, exc)
            self.enabled = False
            return x
        if self.out_ch == 1:
            return np.vstack([out[0], out[0]])
        return out[:2]

    def reset(self):
        try:
            self.plugin.reset()
        except Exception:
            pass

    def close(self):
        try:
            self.plugin.close()
        except Exception:
            pass

    # --- descriptions -----------------------------------------------------
    def describe(self, limit: int | None = 64) -> dict:
        return {"slot_id": self.slot_id, "type": "plugin", "kind": "plugin", "name": self.NAME,
                "category": "Plugin", "color": self.COLOR, "enabled": self.enabled,
                "path": self.path, "format": self.info.get("format"), "vendor": self.info.get("vendor"),
                "num_params": self.plugin.num_params, "gr_db": 0.0,
                "programs": [self.plugin.get_program_name(i) for i in range(min(self.plugin.num_programs, 128))],
                "params": self.params_list(limit)}

    def to_preset(self) -> dict:
        try:
            state = base64.b64encode(self.plugin.get_state()).decode()
        except Exception:
            state = None
        return {"type": "plugin", "path": self.path, "name": self.NAME, "enabled": self.enabled,
                "state": state}

    def load_state(self, b64: str | None):
        if b64:
            try:
                self.plugin.set_state(base64.b64decode(b64))
            except Exception as exc:
                log.warning("could not restore state for %s: %s", self.NAME, exc)


class PluginLibrary:
    """Scans for plugins with Minihost and keeps a catalogue on disk."""

    def __init__(self, data_dir: Path, extra_dirs: list[str] | None = None):
        self.file = data_dir / "plugins.json"
        self.extra_dirs = list(extra_dirs or [])
        self.plugins: list[dict] = []
        self.scan_state = {"running": False, "done": 0, "total": 0, "current": "", "finished_at": None,
                           "error": None}
        self._lock = threading.Lock()
        self._load()

    @property
    def available(self) -> bool:
        return MINIHOST_AVAILABLE

    def dirs(self) -> list[str]:
        return default_plugin_dirs() + [d for d in self.extra_dirs if d]

    def _load(self):
        try:
            self.plugins = json.loads(self.file.read_text())
        except Exception:
            self.plugins = []

    def _save(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(self.plugins, indent=2))

    def scan_async(self, refresh: bool = False) -> dict:
        if not MINIHOST_AVAILABLE:
            raise RuntimeError("Minihost is not installed: pip install minihost")
        with self._lock:
            if self.scan_state["running"]:
                return self.scan_state
            self.scan_state.update(running=True, done=0, total=0, current="", error=None)
        threading.Thread(target=self._scan, args=(refresh,), daemon=True, name="plugin-scan").start()
        return self.scan_state

    def _scan(self, refresh: bool):
        found: dict[str, dict] = {}
        try:
            for d in self.dirs():
                if not os.path.isdir(d):
                    continue
                self.scan_state["current"] = d

                def prog(done, total, path):
                    self.scan_state.update(done=done, total=total, current=path)

                for desc in plugincache.scan(d, refresh=refresh, on_progress=prog):
                    entry = self._normalise(desc)
                    found[entry["path"]] = entry
            self.plugins = sorted(found.values(), key=lambda p: (not p["is_instrument"], p["name"].lower()))
            self._save()
        except Exception as exc:  # pragma: no cover
            log.exception("scan failed")
            self.scan_state["error"] = str(exc)
        finally:
            self.scan_state.update(running=False, finished_at=time.time(), current="")

    @staticmethod
    def _normalise(desc: dict) -> dict:
        is_inst = bool(desc.get("accepts_midi")) and not desc.get("num_inputs")
        return {"id": uuid.uuid5(uuid.NAMESPACE_URL, desc.get("path", "")).hex[:12],
                "name": desc.get("name") or Path(desc.get("path", "?")).stem,
                "vendor": desc.get("vendor", ""), "version": desc.get("version", ""),
                "format": desc.get("format", ""), "path": desc.get("path", ""),
                "accepts_midi": bool(desc.get("accepts_midi")),
                "num_inputs": desc.get("num_inputs", 0), "num_outputs": desc.get("num_outputs", 2),
                "is_instrument": is_inst, "kind": "plugin"}

    def get(self, plugin_id_or_path: str) -> dict | None:
        for p in self.plugins:
            if p["id"] == plugin_id_or_path or p["path"] == plugin_id_or_path:
                return p
        if os.path.exists(plugin_id_or_path) and MINIHOST_AVAILABLE:
            try:
                desc = minihost.probe(plugin_id_or_path)
                desc["path"] = plugin_id_or_path
                return self._normalise(desc)
            except Exception:
                return None
        return None

    def instruments(self) -> list[dict]:
        return [p for p in self.plugins if p["is_instrument"]]

    def effects(self) -> list[dict]:
        return [p for p in self.plugins if not p["is_instrument"]]
