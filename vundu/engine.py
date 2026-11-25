"""Real-time audio engine.

Signal flow (per block, planar stereo float32):

    MIDI (USB / UDP / API) --> sound source --+
                                              +--> effect chain --> master bus --> speakers / line out
    Audio input (guitar)  --> input trim  ----+                                   +-> recorder, meters, scope

The audio device is opened through `sounddevice` (PortAudio), which works
on Linux (ALSA / PipeWire / JACK), Raspberry Pi OS, macOS and Windows.
Plugins in the chain are hosted by Minihost. If no audio device can be
opened, the engine falls back to a silent clock so the console and API
still work (useful on headless test machines).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from datetime import date
from pathlib import Path

import numpy as np

from . import dsp
from .deck import Player, Recorder
from .plugin_host import MINIHOST_AVAILABLE, PluginEffect, PluginLibrary
from .synth import PATCHES, Synth, patch_catalogue

log = logging.getLogger("vundu.engine")

try:
    import sounddevice as sd  # type: ignore
    SD_ERROR = ""
except Exception as exc:  # PortAudio missing
    sd = None
    SD_ERROR = str(exc)

INSTRUMENTS = {
    "keyboard": {"name": "Keyboard", "icon": "keys",
                 "description": "MIDI keyboard / controller through the 5-pin DIN to USB cable. "
                                "Notes play the selected sound, then run through the effects."},
    "guitar": {"name": "Guitar", "icon": "guitar",
               "description": "Audio mode: guitar into the computer's line/mic input or a USB audio "
                              "interface, processed live. MIDI mode: a MIDI guitar / pickup on the DIN "
                              "cable plays the selected sound. MIDI CCs and program changes work in both."},
}

MASTER_RANGES = {"volume": (-60.0, 6.0), "input_gain": (-24.0, 24.0),
                 "instrument_level": (-60.0, 6.0), "fx_return": (-60.0, 12.0),
                 "playback_level": (-60.0, 6.0), "pan": (-1.0, 1.0)}

MAX_SLOTS = 20  # effects per chain / preset
MIXER_CHANNELS = ("inst", "input", "fx", "play", "master")


class AudioEngine:
    def __init__(self, config, plugins: PluginLibrary, record_dir: Path):
        self.config = config
        self.plugins = plugins
        self.sr = int(config.get("sample_rate", 48000))
        self.block = int(config.get("block_size", 256))
        self.backend = "none"
        self.backend_error = ""
        self.stream = None
        self._null_thread = None
        self._null_stop = threading.Event()

        self.instrument = config.get("instrument", "keyboard")
        self.guitar_source = config.get("guitar_source", "audio")
        self.sound = config.get("sound", "builtin:grand_piano")
        self.synth = Synth(self.sr)
        self.instrument_plugin: PluginEffect | None = None
        self.chain: list = []
        self.master = dsp.MasterBus(self.sr)
        self.recorder = Recorder(record_dir)
        self.player = Player(self.sr)
        self.deck_meta = {"artist": config.get("deck_artist", ""), "title": "", "date": ""}
        self.current_preset: dict = {"id": None, "name": "Init"}

        self.midi_events: deque = deque()
        self.midi_map: dict[str, str] = dict(config.get("midi_map") or {
            "1:7": "master.volume", "1:11": "master.instrument_level"})
        self.learn_target: str | None = None
        self.on_program_change = None  # set by the app (loads presets)
        self.state_rev = 0

        # meters / scope
        self.meters = {"in_peak": [-120.0, -120.0], "out_peak": [-120.0, -120.0],
                       "out_rms": [-120.0, -120.0], "gr": 0.0, "clip": False, "cpu": 0.0, "xruns": 0,
                       "ch": {c: -120.0 for c in MIXER_CHANNELS}}
        self.scope = np.zeros(4096, dtype=np.float32)
        self.overview: deque = deque(maxlen=720)
        self.last_note: int | None = None
        self.blocks = 0
        self.started_at = time.time()
        self.lock = threading.RLock()

        self._apply_sound(self.sound, quiet=True)

    # ==================================================================
    # Audio device
    # ==================================================================
    def list_audio_devices(self) -> dict:
        if sd is None:
            return {"backend": "none", "error": SD_ERROR, "outputs": [], "inputs": []}
        outs, ins = [], []
        try:
            hostapis = sd.query_hostapis()
            default_in, default_out = sd.default.device
            for i, d in enumerate(sd.query_devices()):
                api = hostapis[d["hostapi"]]["name"] if hostapis else ""
                item = {"index": i, "name": d["name"], "hostapi": api,
                        "default_samplerate": d.get("default_samplerate")}
                if d["max_output_channels"] > 0:
                    outs.append({**item, "channels": d["max_output_channels"], "is_default": i == default_out})
                if d["max_input_channels"] > 0:
                    ins.append({**item, "channels": d["max_input_channels"], "is_default": i == default_in})
        except Exception as exc:
            return {"backend": "sounddevice", "error": str(exc), "outputs": [], "inputs": []}
        return {"backend": "sounddevice", "error": "", "outputs": outs, "inputs": ins}

    def start(self):
        self.stop()
        if sd is not None and not self.config.get("no_audio"):
            out_dev = self.config.get("audio_output")
            in_dev = self.config.get("audio_input")
            want_input = self.config.get("audio_input_enabled", True)
            attempts = []
            if want_input:
                attempts.append("duplex")
            attempts.append("output")
            for mode in attempts:
                try:
                    if mode == "duplex":
                        self.stream = sd.Stream(samplerate=self.sr, blocksize=self.block,
                                                device=(in_dev, out_dev), channels=(2, 2),
                                                dtype="float32", latency="low", callback=self._cb_duplex)
                    else:
                        self.stream = sd.OutputStream(samplerate=self.sr, blocksize=self.block,
                                                      device=out_dev, channels=2, dtype="float32",
                                                      latency="low", callback=self._cb_output)
                    self.stream.start()
                    self.backend = f"sounddevice/{mode}"
                    self.backend_error = ""
                    log.info("audio started: %s @ %s Hz, block %s", self.backend, self.sr, self.block)
                    return
                except Exception as exc:
                    self.backend_error = str(exc)
                    log.warning("audio %s failed: %s", mode, exc)
                    self.stream = None
        else:
            self.backend_error = SD_ERROR or "audio disabled"
        # fallback: silent clock
        if "device -1" in self.backend_error or "No Default" in self.backend_error:
            self.backend_error = "no sound card / output device found"
        self.backend = "null"
        self._null_stop.clear()
        self._null_thread = threading.Thread(target=self._null_loop, daemon=True, name="null-audio")
        self._null_thread.start()
        log.warning("no audio device - running on a silent clock (%s)", self.backend_error)

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        if self._null_thread is not None:
            self._null_stop.set()
            self._null_thread.join(timeout=1)
            self._null_thread = None

    def reconfigure(self, **changes):
        """Change device / sample rate / block size and restart the stream."""
        snap = self.snapshot()
        for k, v in changes.items():
            if v is not None:
                self.config[k] = v
        new_sr = int(self.config.get("sample_rate", self.sr))
        self.block = int(self.config.get("block_size", self.block))
        if new_sr != self.sr:
            self.sr = new_sr
            self.synth = Synth(self.sr)
            self.master = dsp.MasterBus(self.sr)
            self.apply_preset(snap, keep_name=True)
            loaded = self.player.file
            self.player = Player(self.sr)
            if loaded:
                try:
                    self.player.load(self.recorder.folder / loaded)
                except Exception:
                    pass
        self.start()
        self.bump()

    def _cb_output(self, outdata, frames, time_info, status):
        if status:
            self.meters["xruns"] += 1
        outdata[:] = self.process_block(None, frames).T

    def _cb_duplex(self, indata, outdata, frames, time_info, status):
        if status:
            self.meters["xruns"] += 1
        outdata[:] = self.process_block(indata, frames).T

    def _null_loop(self):
        period = self.block / self.sr
        nxt = time.perf_counter()
        while not self._null_stop.is_set():
            self.process_block(None, self.block)
            nxt += period
            delay = nxt - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                nxt = time.perf_counter()

    # ==================================================================
    # DSP
    # ==================================================================
    def process_block(self, indata, frames: int) -> np.ndarray:
        t0 = time.perf_counter()
        try:
            out = self._render(indata, frames)
        except Exception:
            log.exception("render error")
            out = np.zeros((2, frames), dtype=np.float32)
        dt = time.perf_counter() - t0
        self.meters["cpu"] = 0.9 * self.meters["cpu"] + 0.1 * (dt / (frames / self.sr) * 100)
        return out

    def _drain_midi(self) -> list:
        events = []
        while self.midi_events:
            events.append(self.midi_events.popleft())
        return events

    def _render(self, indata, n: int) -> np.ndarray:
        events = self._drain_midi()
        midi_mode = self.instrument == "keyboard" or self.guitar_source == "midi"
        plugin_midi = []
        for status, d1, d2 in events:
            kind = status & 0xF0
            if self.instrument_plugin is not None and midi_mode:
                plugin_midi.append((0, status, d1, d2))
            if not midi_mode:
                continue
            if kind == 0x90 and d2 > 0:
                self.synth.note_on(d1, d2)
                self.last_note = d1
            elif kind == 0x80 or (kind == 0x90 and d2 == 0):
                self.synth.note_off(d1)
            elif kind == 0xB0 and d1 == 64:
                self.synth.set_sustain(d2 >= 64)
            elif kind == 0xB0 and d1 in (120, 123):
                self.synth.all_notes_off()
            elif kind == 0xE0:
                self.synth.pitch_bend((d2 << 7) | d1)

        s = self.master.settings
        pk = lambda a: dsp.lin_to_db(float(np.max(np.abs(a)))) if a.size else -120.0
        ch = self.meters["ch"]
        # --- sources (mixer channels "inst" and "input") ---------------
        if midi_mode:
            if self.instrument_plugin is not None:
                x = self.instrument_plugin.process(np.zeros((2, n), np.float32), plugin_midi)
            else:
                x = self.synth.render(n)
            x = x * (0.0 if s["inst_mute"] else dsp.db_to_lin(s["instrument_level"]))
            ch["inst"], ch["input"] = pk(x), -120.0
        else:
            if indata is not None:
                c = int(self.config.get("input_channel", 0))
                if self.config.get("input_stereo"):
                    x = np.ascontiguousarray(indata.T[:2], dtype=np.float32)
                else:
                    mono = indata[:, min(c, indata.shape[1] - 1)]
                    x = np.vstack([mono, mono]).astype(np.float32)
            else:
                x = np.zeros((2, n), dtype=np.float32)
            x = x * (0.0 if s["input_mute"] else dsp.db_to_lin(s["input_gain"]))
            ch["input"], ch["inst"] = pk(x), -120.0
        x = x.astype(np.float32, copy=False)
        in_pk = np.max(np.abs(x), axis=1) if x.size else np.zeros(2)

        # --- effects chain (mixer channel "fx": return level / mute = bypass all)
        if not s["fx_mute"] and self.chain:
            for slot in self.chain:
                x = slot.process(x)
            x = x * dsp.db_to_lin(s["fx_return"])
        ch["fx"] = pk(x)
        # --- master ----------------------------------------------------
        y = self.master.process(x)
        # --- tape deck playback (post-master, follows main fader + mute)
        pb = self.player.render(n)
        if pb is not None:
            gain = 0.0 if (s["playback_mute"] or s["mute"]) else \
                dsp.db_to_lin(s["playback_level"]) * dsp.db_to_lin(s["volume"])
            pb = pb * gain
            ch["play"] = pk(pb)
            y = np.clip(y + pb, -1.0, 1.0).astype(np.float32)
        else:
            ch["play"] = -120.0

        # --- meters, scope, recorder ----------------------------------
        pk = np.max(np.abs(y), axis=1)
        rms = np.sqrt(np.mean(y.astype(np.float64) ** 2, axis=1))
        m = self.meters
        m["in_peak"] = [dsp.lin_to_db(float(v)) for v in in_pk]
        m["out_peak"] = [dsp.lin_to_db(float(v)) for v in pk]
        m["out_rms"] = [dsp.lin_to_db(float(v)) for v in rms]
        m["gr"] = self.master.comp.meter_gr_db
        ch["master"] = m["out_peak"][0] if m["out_peak"][0] > m["out_peak"][1] else m["out_peak"][1]
        if pk.max() >= 0.999:
            m["clip"] = True
        mono = (y[0] + y[1]) * 0.5
        if n >= self.scope.size:
            self.scope[:] = mono[-self.scope.size:]
        else:
            self.scope = np.roll(self.scope, -n)
            self.scope[-n:] = mono
        self.overview.append((float(mono.min()), float(mono.max()),
                              self.recorder.active and not self.recorder.paused))
        self.recorder.push(y)
        self.blocks += 1
        return y

    # ==================================================================
    # MIDI in (called from MIDI / network threads)
    # ==================================================================
    def handle_midi(self, status: int, d1: int, d2: int, source: str = "usb"):
        kind = status & 0xF0
        ch = (status & 0x0F) + 1
        if kind == 0xB0 and d1 not in (64, 120, 123):
            key = f"{ch}:{d1}"
            if self.learn_target:
                self.midi_map = {k: v for k, v in self.midi_map.items() if v != self.learn_target}
                self.midi_map[key] = self.learn_target
                self.config["midi_map"] = self.midi_map
                self.learn_target = None
                self.bump()
            target = self.midi_map.get(key) or self.midi_map.get(f"*:{d1}")
            if target:
                try:
                    self.set_target(target, d2 / 127.0)
                except Exception as exc:
                    log.debug("map target %s failed: %s", target, exc)
        elif kind == 0xC0 and self.on_program_change:
            try:
                self.on_program_change(d1)
            except Exception:
                log.exception("program change failed")
        self.midi_events.append((status, d1, d2))

    def panic(self):
        self.synth.all_notes_off()
        for ch in range(16):
            self.midi_events.append((0xB0 | ch, 123, 0))
        for slot in self.chain:
            slot.reset()

    # ==================================================================
    # Control targets (MIDI map + API share this)
    # ==================================================================
    def set_target(self, target: str, norm: float):
        """Set a control from a normalised 0..1 value. Targets:
        master.<volume|input_gain|instrument_level|pan>, master.comp.<p>,
        master.eq.<p>, chain.<index>.<param|enabled>"""
        parts = target.split(".")
        norm = min(max(norm, 0.0), 1.0)
        if parts[0] == "master" and len(parts) == 2:
            lo, hi = MASTER_RANGES[parts[1]]
            self.master.update({parts[1]: lo + (hi - lo) * norm})
        elif parts[0] == "master" and parts[1] in ("comp", "eq"):
            fx = self.master.comp if parts[1] == "comp" else self.master.eq
            self._set_norm(fx, parts[2], norm)
        elif parts[0] == "chain":
            slot = self.chain[int(parts[1])]
            if parts[2] == "enabled":
                slot.enabled = norm >= 0.5
            elif isinstance(slot, PluginEffect):
                slot.set_param(parts[2], norm)
            else:
                self._set_norm(slot, parts[2], norm)
        else:
            raise KeyError(target)
        self.bump()

    @staticmethod
    def _set_norm(fx, pid, norm):
        spec = next(p for p in fx.PARAMS if p["id"] == pid)
        fx.set_param(pid, spec["min"] + (spec["max"] - spec["min"]) * norm)

    def bump(self):
        self.state_rev += 1

    # ==================================================================
    # Instrument / sound
    # ==================================================================
    def set_instrument(self, instrument: str | None = None, guitar_source: str | None = None,
                       sound: str | None = None):
        if instrument:
            if instrument not in INSTRUMENTS:
                raise KeyError(instrument)
            self.instrument = instrument
        if guitar_source:
            if guitar_source not in ("audio", "midi"):
                raise ValueError("guitar_source must be 'audio' or 'midi'")
            self.guitar_source = guitar_source
        if sound:
            self._apply_sound(sound)
        elif instrument == "guitar" and self.guitar_source == "midi" and self.sound == "builtin:grand_piano":
            self._apply_sound("builtin:clean_guitar")
        self.config.update(instrument=self.instrument, guitar_source=self.guitar_source, sound=self.sound)
        self.synth.all_notes_off()
        self.bump()

    def _apply_sound(self, sound: str, quiet: bool = False):
        kind, _, ident = sound.partition(":")
        if kind == "builtin":
            self.synth.set_patch(ident)
            old, self.instrument_plugin = self.instrument_plugin, None
            if old:
                old.close()
        elif kind == "plugin":
            info = self.plugins.get(ident)
            if not info:
                raise KeyError(f"plugin not found: {ident}")
            new = PluginEffect(info, self.sr, self.block, instrument=True)
            old, self.instrument_plugin = self.instrument_plugin, new
            if old:
                old.close()
        else:
            raise ValueError(f"bad sound id: {sound}")
        self.sound = sound

    def sounds(self) -> list[dict]:
        items = [{**p, "id": f"builtin:{p['id']}"} for p in patch_catalogue()]
        for p in self.plugins.instruments():
            items.append({"id": f"plugin:{p['id']}", "name": p["name"], "group": f"Plugins ({p['format']})",
                          "engine": "plugin", "kind": "plugin", "vendor": p["vendor"]})
        return items

    def sound_name(self) -> str:
        if self.instrument_plugin:
            return self.instrument_plugin.NAME
        return PATCHES[self.synth.patch_id]["name"]

    # ==================================================================
    # Chain
    # ==================================================================
    def make_slot(self, spec: dict):
        t = spec.get("type")
        if t == "plugin":
            info = self.plugins.get(spec.get("plugin") or spec.get("path") or spec.get("id", ""))
            if not info:
                raise KeyError(f"plugin not found: {spec.get('plugin') or spec.get('path')}")
            slot = PluginEffect(info, self.sr, self.block)
            slot.load_state(spec.get("state"))
        else:
            slot = dsp.create_effect(t, self.sr)
        if "params" in spec:
            slot.set_params(spec["params"])
        if "enabled" in spec:
            slot.enabled = bool(spec["enabled"])
        return slot

    def add_effect(self, spec: dict, position: int | None = None):
        slot = self.make_slot(spec)
        chain = list(self.chain)
        if position is None or position >= len(chain):
            chain.append(slot)
        else:
            chain.insert(max(0, int(position)), slot)
        if len(chain) > MAX_SLOTS:
            raise ValueError(f"the effect chain is full ({MAX_SLOTS} effects maximum)")
        self.chain = chain
        self.bump()
        return slot

    def find_slot(self, slot_id: str):
        for i, s in enumerate(self.chain):
            if s.slot_id == slot_id:
                return i, s
        raise KeyError(slot_id)

    def remove_effect(self, slot_id: str):
        i, slot = self.find_slot(slot_id)
        self.chain = [s for s in self.chain if s.slot_id != slot_id]
        if isinstance(slot, PluginEffect):
            slot.close()
        self.bump()

    def move_effect(self, slot_id: str, position: int):
        i, slot = self.find_slot(slot_id)
        chain = [s for s in self.chain if s.slot_id != slot_id]
        chain.insert(max(0, min(int(position), len(chain))), slot)
        self.chain = chain
        self.bump()

    def clear_chain(self):
        old, self.chain = self.chain, []
        for s in old:
            if isinstance(s, PluginEffect):
                s.close()
        self.bump()

    # ==================================================================
    # Presets
    # ==================================================================
    def snapshot(self) -> dict:
        return {"instrument": self.instrument, "guitar_source": self.guitar_source, "sound": self.sound,
                "chain": [s.to_preset() for s in self.chain], "master": self.master.to_preset()}

    def apply_preset(self, preset: dict, keep_name: bool = False):
        errors = []
        new_chain = []
        specs = preset.get("chain", [])
        if len(specs) > MAX_SLOTS:
            errors.append(f"only the first {MAX_SLOTS} of {len(specs)} effects were loaded")
            specs = specs[:MAX_SLOTS]
        for spec in specs:
            try:
                new_chain.append(self.make_slot(spec))
            except Exception as exc:
                errors.append(f"{spec.get('name') or spec.get('type')}: {exc}")
        try:
            self.set_instrument(preset.get("instrument"), preset.get("guitar_source"), preset.get("sound"))
        except Exception as exc:
            errors.append(f"sound: {exc}")
        old, self.chain = self.chain, new_chain
        for s in old:
            if isinstance(s, PluginEffect):
                s.close()
        if preset.get("master"):
            self.master.update(preset["master"])
        if not keep_name:
            self.current_preset = {"id": preset.get("id"), "name": preset.get("name", "Untitled")}
        self.bump()
        return errors

    # ==================================================================
    # Tape deck
    # ==================================================================
    def deck_state(self) -> str:
        if self.recorder.active:
            return "rec_paused" if self.recorder.paused else "recording"
        return self.player.state if self.player.file else "empty"

    def deck_view(self) -> dict:
        p = self.player
        rec = self.recorder.active
        return {"state": self.deck_state(),
                "position": round(self.recorder.seconds if rec else p.pos / p.sr, 2),
                "length": round(self.recorder.seconds if rec else p.length / p.sr, 2),
                "file": self.recorder.path.name if rec and self.recorder.path else p.file,
                "take": dict(self.recorder.meta) if rec else {k: p.info.get(k, "") for k in ("artist", "title", "date")},
                "meta": dict(self.deck_meta)}

    def deck_set_meta(self, artist=None, title=None, date_=None):
        if artist is not None:
            self.deck_meta["artist"] = str(artist)[:80]
            self.config["deck_artist"] = self.deck_meta["artist"]
        if title is not None:
            self.deck_meta["title"] = str(title)[:80]
        if date_ is not None:
            self.deck_meta["date"] = str(date_)[:20]

    def deck_record(self):
        if self.recorder.active:
            if self.recorder.paused:
                self.recorder.pause(False)
            return self.recorder.path
        self.player.stop()
        meta = dict(self.deck_meta)
        meta["date"] = meta["date"] or date.today().isoformat()
        meta["title"] = meta["title"] or "Untitled"
        path = self.recorder.start(self.sr, meta)
        self.bump()
        return path

    def deck_pause(self):
        if self.recorder.active:
            self.recorder.pause()
        else:
            self.player.pause()

    def deck_stop(self) -> dict | None:
        info = None
        if self.recorder.active:
            info = self.recorder.stop()
            try:  # load the new take, ready to play back
                self.player.load(self.recorder.folder / info["file"])
            except Exception as exc:
                log.warning("could not load take: %s", exc)
            self.bump()
        else:
            self.player.stop()
        return info

    def deck_load(self, name: str):
        path = self.recorder.folder / name
        if "/" in name or "\\" in name or ".." in name or not path.exists():
            raise KeyError(name)
        if self.recorder.active:
            raise RuntimeError("stop recording first")
        self.player.load(path)
        self.bump()

    def deck_play(self):
        if self.recorder.active:
            raise RuntimeError("stop recording first")
        self.player.play()

    def deck_seek(self, seconds=None, delta=None):
        if self.recorder.active:
            raise RuntimeError("can't wind the tape while recording")
        self.player.seek(seconds, delta)

    # ==================================================================
    # Views
    # ==================================================================
    def spectrum(self, bands: int = 48) -> list[float]:
        x = self.scope[-2048:] * np.hanning(2048)
        mag = np.abs(np.fft.rfft(x)) / 512.0
        freqs = np.fft.rfftfreq(2048, 1 / self.sr)
        edges = np.geomspace(30, min(18000, self.sr / 2 - 1), bands + 1)
        out = []
        for lo, hi in zip(edges[:-1], edges[1:]):
            sel = mag[(freqs >= lo) & (freqs < hi)]
            v = float(sel.max()) if sel.size else 0.0
            out.append(round(dsp.lin_to_db(v, -90), 1))
        return out

    def waveform(self, points: int = 360) -> dict:
        ov = list(self.overview)[-points:]
        sc = self.scope[-1024:][::4]
        return {"overview": [[round(a, 3), round(b, 3), int(r)] for a, b, r in ov],
                "scope": [round(float(v), 3) for v in sc]}

    def meter_view(self) -> dict:
        m = self.meters
        return {"in_peak": [round(v, 1) for v in m["in_peak"]],
                "out_peak": [round(v, 1) for v in m["out_peak"]],
                "out_rms": [round(v, 1) for v in m["out_rms"]],
                "gr": round(m["gr"], 2), "clip": m["clip"], "cpu": round(m["cpu"], 1),
                "xruns": m["xruns"],
                "slots_gr": {s.slot_id: round(getattr(s, "meter_gr_db", 0.0), 1) for s in self.chain},
                "notes": self.synth.active_notes, "last_note": self.last_note,
                "rec": {"active": self.recorder.active, "seconds": round(self.recorder.seconds, 1)},
                "ch": {k: round(v, 1) for k, v in m["ch"].items()},
                "deck": self.deck_view(),
                "rev": self.state_rev}

    def audio_status(self) -> dict:
        return {"backend": self.backend, "error": self.backend_error, "sample_rate": self.sr,
                "block_size": self.block, "latency_ms": round(self.block / self.sr * 1000, 2),
                "output": self.config.get("audio_output"), "input": self.config.get("audio_input"),
                "input_channel": self.config.get("input_channel", 0),
                "minihost": MINIHOST_AVAILABLE}

    def chain_view(self) -> list[dict]:
        return [dict(s.describe(), index=i) for i, s in enumerate(self.chain)]
