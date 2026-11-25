"""Tests for Vundu_MIDI. Run with: pytest"""

import socket
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vundu import dsp  # noqa: E402
from vundu.midi_input import MidiParser  # noqa: E402
from vundu.synth import PATCHES, Synth  # noqa: E402

SR = 48000


@pytest.mark.parametrize("effect_type", list(dsp.BUILTIN_EFFECTS))
def test_effects_are_finite_and_shaped(effect_type):
    fx = dsp.create_effect(effect_type, SR)
    x = (np.random.randn(2, 256) * 0.2).astype(np.float32)
    for _ in range(20):
        y = fx.process(x)
    assert y.shape == (2, 256)
    assert np.isfinite(y).all()
    assert np.abs(y).max() < 10


@pytest.mark.parametrize("effect_type", list(dsp.BUILTIN_EFFECTS))
def test_effect_params_clamp(effect_type):
    fx = dsp.create_effect(effect_type, SR)
    for p in fx.PARAMS:
        fx.set_param(p["id"], p["max"] + 1000)
        assert fx.params[p["id"]] == p["max"]


def test_bypass_is_transparent():
    fx = dsp.create_effect("distortion", SR)
    fx.enabled = False
    x = np.random.randn(2, 128).astype(np.float32)
    assert np.array_equal(fx.process(x), x)


def test_compressor_reduces_gain():
    c = dsp.Compressor(SR)
    c.set_params({"threshold": -30, "ratio": 10, "attack": 0.1, "makeup": 0})
    x = np.full((2, 256), 0.9, np.float32)
    for _ in range(50):
        y = c.process(x)
    assert np.abs(y).max() < 0.3
    assert c.meter_gr_db < -10


def test_reverb_tail_decays():
    r = dsp.Reverb(SR)
    r.set_params({"mix": 1, "predelay": 0, "size": 0.5})
    imp = np.zeros((2, 256), np.float32)
    imp[:, 0] = 1
    levels = [np.abs(r.process(imp if i == 0 else np.zeros_like(imp))).max() for i in range(500)]
    assert max(levels[:40]) > 0.01
    assert levels[-1] < max(levels[:40]) / 100


@pytest.mark.parametrize("patch", list(PATCHES))
def test_synth_patches_play_and_release(patch):
    s = Synth(SR, patch)
    s.note_on(60, 100)
    y = np.hstack([s.render(256) for _ in range(20)])
    assert np.abs(y).max() > 0.005
    s.note_off(60)
    for _ in range(2000):
        s.render(256)
    assert not s.voices


def test_sustain_pedal_holds_notes():
    s = Synth(SR, "organ")
    s.set_sustain(True)
    s.note_on(60, 100)
    s.note_off(60)
    s.render(256)
    assert 60 in s.active_notes
    s.set_sustain(False)
    for _ in range(200):
        s.render(256)
    assert 60 not in s.active_notes


def test_midi_parser_running_status_and_sysex():
    p = MidiParser()
    msgs = p.feed(bytes([0x90, 60, 100, 64, 90, 0xF0, 1, 2, 3, 0xF7, 0xF8, 0xB0, 7, 127, 0xC0, 5]))
    assert msgs == [(0x90, 60, 100), (0x90, 64, 90), (0xB0, 7, 127), (0xC0, 5, 0)]


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from vundu.server import create_app
    app, vapp = create_app(tmp_path_factory.mktemp("data"), {"no_audio": True, "udp_midi_port": 0})
    yield app.test_client(), vapp
    vapp.shutdown()


def test_status_and_index(client):
    c, _ = client
    r = c.get("/api/v1/status").get_json()
    assert r["ok"] and r["version"] == "0.0.2"
    idx = c.get("/api/v1").get_json()
    assert any("/api/v1/chain" in e for e in idx["endpoints"])


def test_all_factory_presets_load(client):
    c, _ = client
    presets = c.get("/api/v1/presets").get_json()["presets"]
    assert len(presets) >= 12
    for p in presets:
        r = c.post("/api/v1/presets/load", json={"id": p["id"]}).get_json()
        assert r["ok"] and not r["errors"], (p["id"], r)


def test_chain_crud(client):
    c, _ = client
    c.post("/api/v1/chain/clear")
    slot = c.post("/api/v1/chain", json={"type": "delay", "params": {"time": 500}}).get_json()["slot"]
    assert slot["name"] == "Tape Delay"
    sid = slot["slot_id"]
    r = c.patch(f"/api/v1/chain/{sid}", json={"params": {"mix": 0.5}, "enabled": False}).get_json()
    assert r["slot"]["enabled"] is False
    assert next(p for p in r["slot"]["params"] if p["id"] == "mix")["value"] == 0.5
    c.post("/api/v1/chain", json={"type": "reverb", "position": 0})
    chain = c.post(f"/api/v1/chain/{sid}/move", json={"position": 0}).get_json()["chain"]
    assert chain[0]["slot_id"] == sid
    assert c.delete(f"/api/v1/chain/{sid}").get_json()["ok"]
    assert c.post("/api/v1/chain", json={"type": "nope"}).status_code == 404


def test_midi_send_plays_notes(client):
    c, vapp = client
    c.post("/api/v1/instrument", json={"instrument": "keyboard", "sound": "builtin:grand_piano"})
    r = c.post("/api/v1/midi/send", json={"messages": [{"type": "note_on", "note": 64}, {"status": 0x90, "data1": 67, "data2": 90}]})
    assert r.get_json()["sent"][0].startswith("Note On")
    time.sleep(0.2)
    assert 64 in c.get("/api/v1/meters").get_json()["notes"]
    c.post("/api/v1/panic")


def test_midi_learn_and_map(client):
    c, vapp = client
    c.post("/api/v1/midi/learn", json={"target": "master.pan"})
    vapp.engine.handle_midi(0xB0, 21, 0)
    m = c.get("/api/v1/midi/map").get_json()
    assert m["map"]["1:21"] == "master.pan" and m["learn"] is None
    assert vapp.engine.master.settings["pan"] == -1.0


def test_presets_save_and_delete(client):
    c, _ = client
    r = c.post("/api/v1/presets", json={"name": "Test Sound", "category": "Tests"}).get_json()
    pid = r["preset"]["id"]
    assert pid == "user:test_sound"
    assert c.delete("/api/v1/presets/factory:metal_lead").status_code == 403
    assert c.delete(f"/api/v1/presets/{pid}").get_json()["ok"]


def test_recording_writes_wav(client):
    import wave
    c, vapp = client
    c.post("/api/v1/record/start", json={"name": "unit"})
    time.sleep(0.4)
    info = c.post("/api/v1/record/stop").get_json()["recording"]
    path = vapp.recordings_dir / info["file"]
    with wave.open(str(path)) as w:
        assert w.getnchannels() == 2 and w.getsampwidth() == 3 and w.getnframes() > 0
    assert c.delete(f"/api/v1/recordings/{info['file']}").get_json()["ok"]


def test_master_and_meters(client):
    c, _ = client
    r = c.patch("/api/v1/master", json={"volume": -12, "comp": {"enabled": True, "params": {"ratio": 4}}}).get_json()
    assert r["master"]["volume"] == -12 and r["master"]["comp"]["enabled"]
    assert len(c.get("/api/v1/spectrum?bands=24").get_json()["bands"]) == 24
    assert "overview" in c.get("/api/v1/waveform").get_json()


def test_udp_midi(tmp_path):
    from vundu.midi_input import MidiManager, NetworkMidi
    got = []
    m = MidiManager(lambda s, a, b, src: got.append((s, a, b, src)), auto_connect=False)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    net = NetworkMidi(m, port, "127.0.0.1")
    net.start()
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.sendto(bytes([0x90, 60, 100]), ("127.0.0.1", port))
    for _ in range(50):
        if got:
            break
        time.sleep(0.02)
    net.stop()
    assert got and got[0][:3] == (0x90, 60, 100) and got[0][3].startswith("udp:")


# --------------------------------------------------------------------------- #
# v0.0.2: tape deck, mixer, 20 effects
# --------------------------------------------------------------------------- #


def test_deck_record_pause_stop_tags(client):
    from vundu.deck import read_info
    c, vapp = client
    c.post("/api/v1/deck/meta", json={"artist": "Bertie", "title": "Test Take", "date": "2026-11-25"})
    assert c.post("/api/v1/deck/record").get_json()["deck"]["state"] == "recording"
    time.sleep(0.3)
    assert c.post("/api/v1/deck/pause").get_json()["deck"]["state"] == "rec_paused"
    frames = vapp.engine.recorder.frames
    time.sleep(0.3)
    assert vapp.engine.recorder.frames == frames  # nothing written while paused
    assert c.post("/api/v1/deck/record").get_json()["deck"]["state"] == "recording"  # resume
    time.sleep(0.2)
    r = c.post("/api/v1/deck/stop").get_json()
    rec = r["recording"]
    assert rec["file"] == "2026-11-25_Bertie_Test_Take.wav"
    assert r["deck"]["state"] == "stopped" and r["deck"]["file"] == rec["file"]
    info = read_info(vapp.recordings_dir / rec["file"])
    assert (info["artist"], info["title"], info["date"]) == ("Bertie", "Test Take", "2026-11-25")
    listed = c.get("/api/v1/recordings").get_json()["recordings"]
    assert any(f["title"] == "Test Take" and f["artist"] == "Bertie" for f in listed)


def test_deck_play_pause_seek(client):
    c, vapp = client
    name = c.get("/api/v1/recordings").get_json()["recordings"][0]["file"]
    assert c.post("/api/v1/deck/play", json={"file": name}).get_json()["deck"]["state"] == "playing"
    time.sleep(0.15)
    assert c.post("/api/v1/deck/pause").get_json()["deck"]["state"] == "paused"
    d = c.post("/api/v1/deck/forward", json={"seconds": 0.2}).get_json()["deck"]
    pos = d["position"]
    assert pos > 0
    assert c.post("/api/v1/deck/rewind", json={"seconds": 100}).get_json()["deck"]["position"] == 0
    assert c.post("/api/v1/deck/seek", json={"position": 999}).get_json()["deck"]["position"] == d["length"]
    assert c.post("/api/v1/deck/stop").get_json()["deck"]["position"] == 0


def test_deck_refuses_winding_while_recording(client):
    c, _ = client
    c.post("/api/v1/deck/record")
    assert c.post("/api/v1/deck/forward").status_code == 409
    c.post("/api/v1/deck/stop")


def test_wav_loader_formats(tmp_path):
    import wave
    from vundu.deck import load_wav
    for width, ch, sr in ((2, 1, 44100), (3, 2, 48000)):
        p = tmp_path / f"t{width}{ch}.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(ch)
            w.setsampwidth(width)
            w.setframerate(sr)
            w.writeframes(bytes(width * ch * (sr // 10)))
        x = load_wav(p, 48000)
        assert x.shape[0] == 2 and abs(x.shape[1] - 4800) <= 2


def test_twenty_effects_max(client):
    c, _ = client
    c.post("/api/v1/chain/clear")
    for i in range(20):
        assert c.post("/api/v1/chain", json={"type": "eq3"}).get_json()["ok"]
    r = c.post("/api/v1/chain", json={"type": "eq3"})
    assert r.status_code == 400 and "20" in r.get_json()["error"]
    saved = c.post("/api/v1/presets", json={"name": "Twenty FX"}).get_json()["preset"]["id"]
    assert c.post("/api/v1/presets/load", json={"id": saved}).get_json()["errors"] == []
    assert len(c.get("/api/v1/chain").get_json()["chain"]) == 20
    c.delete(f"/api/v1/presets/{saved}")
    c.post("/api/v1/chain/clear")


def test_mixer_channels(client):
    c, vapp = client
    m = c.patch("/api/v1/master", json={"fx_return": -6, "playback_level": -3, "fx_mute": True,
                                        "playback_mute": True}).get_json()["master"]
    assert m["fx_return"] == -6 and m["fx_mute"] and m["playback_mute"]
    assert set(c.get("/api/v1/meters").get_json()["ch"]) == {"inst", "input", "fx", "play", "master"}
    c.patch("/api/v1/master", json={"fx_mute": False, "playback_mute": False, "fx_return": 0})


def test_old_max_slots_setting_is_dropped(tmp_path):
    import json
    from vundu.config import Config
    (tmp_path / "config.json").write_text(json.dumps({"max_slots": 8, "port": 5080}))
    assert "max_slots" not in Config(tmp_path / "config.json")
