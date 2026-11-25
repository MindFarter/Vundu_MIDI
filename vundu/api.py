"""REST API v1 for Vundu_MIDI.

Everything the console GUI does goes through this API, so the same
calls can be used by scripts, a phone, or the planned ESP32 MIDI
interface. See docs/API.md for the full reference.
"""

from __future__ import annotations

import json
import time

from flask import Blueprint, Response, jsonify, request, send_from_directory, stream_with_context

from . import __version__
from .dsp import BUILTIN_EFFECTS
from .deck import read_info
from .engine import INSTRUMENTS, MAX_SLOTS
from .midi_input import describe_message


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def ok(**kw):
    return jsonify({"ok": True, **kw})


def create_api(vapp) -> Blueprint:
    api = Blueprint("api_v1", __name__)
    eng = vapp.engine

    @api.errorhandler(ApiError)
    def _api_error(e):
        return jsonify({"ok": False, "error": str(e)}), e.status

    @api.errorhandler(KeyError)
    def _key_error(e):
        return jsonify({"ok": False, "error": f"not found: {e.args[0] if e.args else e}"}), 404

    @api.errorhandler(ValueError)
    def _value_error(e):
        return jsonify({"ok": False, "error": str(e)}), 400

    @api.errorhandler(RuntimeError)
    def _rt_error(e):
        return jsonify({"ok": False, "error": str(e)}), 409

    # ------------------------------------------------------------------
    # Index / status
    # ------------------------------------------------------------------
    @api.get("")
    @api.get("/")
    def index():
        rules = sorted({f"{', '.join(sorted(r.methods - {'HEAD', 'OPTIONS'}))} {r.rule}"
                        for r in vapp_app_rules()})
        return ok(name="Vundu_MIDI API", version="v1", app_version=__version__, endpoints=rules)

    def vapp_app_rules():
        from flask import current_app
        return [r for r in current_app.url_map.iter_rules() if r.rule.startswith("/api/v1")]

    @api.get("/status")
    def status():
        return ok(version=__version__, audio=eng.audio_status(), midi=vapp.midi.status(),
                  udp_midi=vapp.net_midi.status(), instrument=eng.instrument,
                  guitar_source=eng.guitar_source, sound=eng.sound, sound_name=eng.sound_name(),
                  preset=eng.current_preset, chain=[s["name"] for s in eng.chain_view()],
                  recording=eng.recorder.active, remotes=list(vapp.remotes.values()), rev=eng.state_rev)

    @api.get("/system")
    def system():
        return ok(**vapp.system_info())

    @api.get("/state")
    def state():
        """Everything the console needs to draw itself in one call."""
        return ok(version=__version__, audio=eng.audio_status(), midi=vapp.midi.status(),
                  instrument=eng.instrument, guitar_source=eng.guitar_source, sound=eng.sound,
                  sound_name=eng.sound_name(), preset=eng.current_preset, chain=eng.chain_view(),
                  master=eng.master.describe(), midi_map=eng.midi_map, learn=eng.learn_target,
                  rev=eng.state_rev, minihost=vapp.plugins.available, max_slots=MAX_SLOTS,
                  deck=eng.deck_view())

    # ------------------------------------------------------------------
    # Devices
    # ------------------------------------------------------------------
    @api.get("/devices/audio")
    def audio_devices():
        return ok(**eng.list_audio_devices(), current=eng.audio_status())

    @api.post("/devices/audio")
    def set_audio():
        b = _body()
        allowed = {k: b[k] for k in ("audio_output", "audio_input", "sample_rate", "block_size",
                                      "input_channel", "audio_input_enabled", "input_stereo") if k in b}
        if "sample_rate" in allowed and int(allowed["sample_rate"]) not in (44100, 48000, 88200, 96000):
            raise ApiError("sample_rate must be 44100, 48000, 88200 or 96000")
        if "block_size" in allowed and int(allowed["block_size"]) not in (64, 128, 256, 512, 1024, 2048):
            raise ApiError("block_size must be 64-2048 (power of two)")
        eng.reconfigure(**allowed)
        vapp.save_config()
        return ok(audio=eng.audio_status())

    @api.get("/devices/midi")
    def midi_devices():
        return ok(ports=vapp.midi.list_ports(), **vapp.midi.status())

    @api.post("/devices/midi/connect")
    def midi_connect():
        b = _body()
        port = b.get("port")
        if port is None:
            raise ApiError("port (index or name) required")
        name = vapp.midi.connect(port)
        vapp.save_config()
        return ok(port=name)

    @api.post("/devices/midi/disconnect")
    def midi_disconnect():
        vapp.midi.disconnect()
        return ok()

    @api.post("/devices/midi/auto")
    def midi_auto():
        vapp.midi.auto_connect = bool(_body().get("enabled", True))
        vapp.config["midi_auto_connect"] = vapp.midi.auto_connect
        vapp.save_config()
        return ok(auto_connect=vapp.midi.auto_connect)

    # ------------------------------------------------------------------
    # Instruments & sounds
    # ------------------------------------------------------------------
    @api.get("/instruments")
    def instruments():
        return ok(instruments=[{"id": k, **v} for k, v in INSTRUMENTS.items()], current=eng.instrument,
                  guitar_source=eng.guitar_source)

    @api.post("/instrument")
    def set_instrument():
        b = _body()
        eng.set_instrument(b.get("instrument"), b.get("guitar_source"), b.get("sound"))
        vapp.save_config()
        return ok(instrument=eng.instrument, guitar_source=eng.guitar_source, sound=eng.sound,
                  sound_name=eng.sound_name())

    @api.get("/sounds")
    def sounds():
        return ok(sounds=eng.sounds(), current=eng.sound)

    # ------------------------------------------------------------------
    # Effects catalogue + chain
    # ------------------------------------------------------------------
    @api.get("/effects")
    def effects():
        builtin = [cls.catalogue_entry() for cls in BUILTIN_EFFECTS.values()]
        plugins = vapp.plugins.effects()
        return ok(builtin=builtin, plugins=plugins, minihost=vapp.plugins.available)

    @api.get("/chain")
    def chain():
        return ok(chain=eng.chain_view(), max_slots=MAX_SLOTS)

    @api.post("/chain")
    def chain_add():
        b = _body()
        if not b.get("type"):
            raise ApiError("type required (e.g. 'reverb', or 'plugin' with 'plugin': <id or path>)")
        slot = eng.add_effect(b, b.get("position"))
        return ok(slot=slot.describe(), chain=eng.chain_view())

    @api.post("/chain/clear")
    def chain_clear():
        eng.clear_chain()
        return ok(chain=[])

    @api.get("/chain/<slot_id>")
    def chain_get(slot_id):
        i, slot = eng.find_slot(slot_id)
        d = slot.describe(None) if hasattr(slot, "plugin") else slot.describe()
        return ok(slot=dict(d, index=i))

    @api.route("/chain/<slot_id>", methods=["PATCH", "PUT"])
    def chain_patch(slot_id):
        b = _body()
        i, slot = eng.find_slot(slot_id)
        if "enabled" in b:
            slot.enabled = bool(b["enabled"])
        if "params" in b:
            slot.set_params(b["params"])
        if "program" in b and hasattr(slot, "plugin"):
            slot.plugin.program = int(b["program"])
        eng.bump()
        return ok(slot=dict(slot.describe(), index=i))

    @api.delete("/chain/<slot_id>")
    def chain_delete(slot_id):
        eng.remove_effect(slot_id)
        return ok(chain=eng.chain_view())

    @api.post("/chain/<slot_id>/move")
    def chain_move(slot_id):
        pos = _body().get("position")
        if pos is None:
            raise ApiError("position required")
        eng.move_effect(slot_id, int(pos))
        return ok(chain=eng.chain_view())

    # ------------------------------------------------------------------
    # Master bus
    # ------------------------------------------------------------------
    @api.get("/master")
    def master():
        return ok(master=eng.master.describe())

    @api.route("/master", methods=["PATCH", "PUT", "POST"])
    def master_patch():
        eng.master.update(_body())
        eng.bump()
        return ok(master=eng.master.describe())

    @api.post("/control")
    def control():
        """Set any mappable control with a normalised 0..1 value:
        {"target": "master.volume", "value": 0.8}"""
        b = _body()
        eng.set_target(b["target"], float(b["value"]))
        return ok()

    # ------------------------------------------------------------------
    # Meters / scope / live stream
    # ------------------------------------------------------------------
    @api.get("/meters")
    def meters():
        return ok(**eng.meter_view())

    @api.post("/meters/reset_clip")
    def reset_clip():
        eng.meters["clip"] = False
        return ok()

    @api.get("/waveform")
    def waveform():
        return ok(**eng.waveform(int(request.args.get("points", 360))))

    @api.get("/spectrum")
    def spectrum():
        return ok(bands=eng.spectrum(int(request.args.get("bands", 48))))

    @api.get("/stream")
    def stream():
        """Server-Sent Events: meters + waveform + spectrum at ~20 fps."""
        fps = min(30, max(2, int(request.args.get("fps", 20))))

        def gen():
            period = 1.0 / fps
            while True:
                payload = {"meters": eng.meter_view(), "wave": eng.waveform(240),
                           "spectrum": eng.spectrum(40)}
                yield f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
                time.sleep(period)

        return Response(stream_with_context(gen()), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------
    # Presets
    # ------------------------------------------------------------------
    @api.get("/presets")
    def presets():
        return ok(presets=vapp.presets.summary(), current=eng.current_preset)

    @api.get("/presets/<preset_id>")
    def preset_get(preset_id):
        return ok(preset=vapp.presets.get(preset_id))

    @api.post("/presets/load")
    def preset_load():
        pid = _body().get("id")
        if not pid:
            raise ApiError("id required")
        return ok(**vapp.load_preset(pid))

    @api.post("/presets")
    def preset_save():
        b = _body()
        name = (b.get("name") or "").strip()
        if not name:
            raise ApiError("name required")
        p = vapp.presets.save(name, b.get("category", "User"), eng.snapshot(), b.get("description", ""))
        eng.current_preset = {"id": p["id"], "name": p["name"]}
        eng.bump()
        return ok(preset={"id": p["id"], "name": p["name"]})

    @api.delete("/presets/<preset_id>")
    def preset_delete(preset_id):
        try:
            vapp.presets.delete(preset_id)
        except PermissionError as e:
            raise ApiError(str(e), 403)
        return ok()

    @api.get("/snapshot")
    def snapshot():
        return ok(snapshot=eng.snapshot())

    # ------------------------------------------------------------------
    # Tape deck: record / pause / stop / play / rewind / fast forward
    # ------------------------------------------------------------------
    @api.get("/deck")
    def deck():
        return ok(deck=eng.deck_view())

    @api.route("/deck/meta", methods=["POST", "PATCH"])
    def deck_meta():
        b = _body()
        eng.deck_set_meta(b.get("artist"), b.get("title"), b.get("date"))
        vapp.save_config()
        return ok(deck=eng.deck_view())

    @api.post("/deck/record")
    def deck_record():
        b = _body()
        if b:
            eng.deck_set_meta(b.get("artist"), b.get("title"), b.get("date"))
        path = eng.deck_record()
        return ok(file=path.name, deck=eng.deck_view())

    @api.post("/deck/pause")
    def deck_pause():
        eng.deck_pause()
        return ok(deck=eng.deck_view())

    @api.post("/deck/stop")
    def deck_stop():
        info = eng.deck_stop()
        return ok(recording=info, deck=eng.deck_view())

    @api.post("/deck/play")
    def deck_play():
        f = _body().get("file")
        if f:
            eng.deck_load(f)
        eng.deck_play()
        return ok(deck=eng.deck_view())

    @api.post("/deck/load")
    def deck_load():
        f = _body().get("file")
        if not f:
            raise ApiError("file required")
        eng.deck_load(f)
        return ok(deck=eng.deck_view())

    @api.post("/deck/seek")
    def deck_seek():
        b = _body()
        if "position" not in b and "delta" not in b:
            raise ApiError("position (seconds) or delta (seconds, negative = rewind) required")
        eng.deck_seek(b.get("position"), b.get("delta"))
        return ok(deck=eng.deck_view())

    @api.post("/deck/rewind")
    def deck_rewind():
        eng.deck_seek(delta=-float(_body().get("seconds", 5)))
        return ok(deck=eng.deck_view())

    @api.post("/deck/forward")
    def deck_forward():
        eng.deck_seek(delta=float(_body().get("seconds", 5)))
        return ok(deck=eng.deck_view())

    # v0.0.1 compatible shortcuts
    @api.post("/record/start")
    def rec_start():
        b = _body()
        if b.get("name") and not b.get("title"):
            b["title"] = b["name"]
        if b:
            eng.deck_set_meta(b.get("artist"), b.get("title"), b.get("date"))
        path = eng.deck_record()
        return ok(file=path.name)

    @api.post("/record/stop")
    def rec_stop():
        return ok(recording=eng.deck_stop() if eng.recorder.active else None)

    @api.get("/recordings")
    def recordings():
        d = vapp.recordings_dir
        files = []
        if d.exists():
            for f in sorted(d.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True):
                if eng.recorder.active and eng.recorder.path and f.name == eng.recorder.path.name:
                    continue
                st = f.stat()
                info = read_info(f)
                files.append({"file": f.name, "bytes": st.st_size, "seconds": info["seconds"],
                              "artist": info["artist"], "title": info["title"], "date": info["date"],
                              "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                              "url": f"/api/v1/recordings/{f.name}"})
        return ok(recordings=files, active=eng.recorder.active, loaded=eng.player.file)

    @api.get("/recordings/<name>")
    def recording_get(name):
        if "/" in name or ".." in name:
            raise ApiError("bad name")
        return send_from_directory(vapp.recordings_dir, name, as_attachment=request.args.get("download") == "1")

    @api.delete("/recordings/<name>")
    def recording_delete(name):
        if "/" in name or ".." in name or not name.endswith(".wav"):
            raise ApiError("bad name")
        if eng.recorder.active and eng.recorder.path and eng.recorder.path.name == name:
            raise ApiError("this take is being recorded", 409)
        p = vapp.recordings_dir / name
        if not p.exists():
            raise KeyError(name)
        if eng.player.file == name:
            eng.player.unload()
            eng.bump()
        p.unlink()
        return ok()

    # ------------------------------------------------------------------
    # MIDI over the network (ESP32 / scripts) + monitor + mapping
    # ------------------------------------------------------------------
    def _msg_from(d: dict):
        if "status" in d:
            return int(d["status"]) & 0xFF, int(d.get("data1", 0)) & 0x7F, int(d.get("data2", 0)) & 0x7F
        t = d.get("type")
        ch = (int(d.get("channel", 1)) - 1) & 0x0F
        if t == "note_on":
            return 0x90 | ch, int(d["note"]) & 0x7F, int(d.get("velocity", 100)) & 0x7F
        if t == "note_off":
            return 0x80 | ch, int(d["note"]) & 0x7F, 0
        if t in ("cc", "control_change"):
            return 0xB0 | ch, int(d["control"]) & 0x7F, int(d["value"]) & 0x7F
        if t == "program_change":
            return 0xC0 | ch, int(d["program"]) & 0x7F, 0
        if t == "pitch_bend":
            v = int(d.get("value", 0)) + 8192
            v = min(max(v, 0), 16383)
            return 0xE0 | ch, v & 0x7F, v >> 7
        raise ApiError("message needs status/data1/data2 or a type: note_on, note_off, cc, "
                       "program_change, pitch_bend")

    @api.post("/midi/send")
    def midi_send():
        b = _body()
        msgs = b.get("messages") if isinstance(b.get("messages"), list) else [b]
        src = f"api:{request.remote_addr}"
        sent = []
        for m in msgs:
            st, d1, d2 = _msg_from(m)
            vapp.midi.dispatch(st, d1, d2, src)
            sent.append(describe_message(st, d1, d2))
        return ok(sent=sent)

    @api.post("/panic")
    def panic():
        eng.panic()
        return ok()

    @api.get("/midi/log")
    def midi_log():
        return ok(log=list(vapp.midi.log))

    @api.get("/midi/map")
    def midi_map():
        return ok(map=eng.midi_map, learn=eng.learn_target)

    @api.post("/midi/map")
    def midi_map_set():
        b = _body()
        key = f"{b.get('channel', 1)}:{int(b['cc'])}"
        eng.midi_map[key] = b["target"]
        vapp.save_config()
        eng.bump()
        return ok(map=eng.midi_map)

    @api.delete("/midi/map/<key>")
    def midi_map_del(key):
        eng.midi_map.pop(key)
        vapp.save_config()
        eng.bump()
        return ok(map=eng.midi_map)

    @api.post("/midi/learn")
    def midi_learn():
        eng.learn_target = _body().get("target") or None
        eng.bump()
        return ok(learn=eng.learn_target)

    # ------------------------------------------------------------------
    # Plugins (Minihost)
    # ------------------------------------------------------------------
    @api.get("/plugins")
    def plugins():
        return ok(available=vapp.plugins.available, plugins=vapp.plugins.plugins, dirs=vapp.plugins.dirs(),
                  scan=vapp.plugins.scan_state,
                  install_hint="pip install minihost  (Raspberry Pi: see README - builds from source)")

    @api.post("/plugins/scan")
    def plugins_scan():
        st = vapp.plugins.scan_async(bool(_body().get("refresh")))
        return ok(scan=st)

    @api.get("/plugins/scan")
    def plugins_scan_state():
        return ok(scan=vapp.plugins.scan_state, count=len(vapp.plugins.plugins))

    @api.post("/plugins/dirs")
    def plugins_dirs():
        dirs = _body().get("dirs") or []
        if not isinstance(dirs, list):
            raise ApiError("dirs must be a list of folders")
        vapp.plugins.extra_dirs = [str(d) for d in dirs]
        vapp.config["plugin_dirs"] = vapp.plugins.extra_dirs
        vapp.save_config()
        return ok(dirs=vapp.plugins.dirs())

    # ------------------------------------------------------------------
    # Remote controllers (ESP32 MIDI interface - groundwork)
    # ------------------------------------------------------------------
    @api.post("/remote/hello")
    def remote_hello():
        b = _body()
        rid = str(b.get("id") or request.remote_addr)
        vapp.remotes[rid] = {"id": rid, "name": b.get("name", "ESP32"), "ip": request.remote_addr,
                             "firmware": b.get("firmware", ""), "board": b.get("board", ""),
                             "last_seen": time.time()}
        return ok(server=__version__, udp_midi_port=vapp.net_midi.port, sample_rate=eng.sr,
                  preset=eng.current_preset)

    @api.get("/remote")
    def remotes():
        return ok(remotes=list(vapp.remotes.values()))

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    @api.get("/settings")
    def settings():
        return ok(settings=vapp.config.public())

    @api.patch("/settings")
    def settings_patch():
        b = _body()
        for k in ("startup_preset", "theme", "midi_auto_connect"):
            if k in b:
                vapp.config[k] = b[k]
        vapp.save_config()
        return ok(settings=vapp.config.public())

    return api
