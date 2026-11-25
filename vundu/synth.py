"""Built-in polyphonic sound generator.

Used when the selected instrument is a MIDI keyboard (or a MIDI guitar)
and no external instrument plugin has been loaded through Minihost.
All synthesis is vectorised numpy per voice per block.
"""

from __future__ import annotations

import math

import numpy as np

from .dsp import Biquad

TWO_PI = 2.0 * math.pi

# Each patch: engine + parameters. Times in seconds.
PATCHES: dict[str, dict] = {
    "grand_piano": {"name": "Grand Piano", "group": "Keys", "engine": "additive",
                    "partials": [1.0, 0.55, 0.32, 0.22, 0.12, 0.08, 0.05, 0.03],
                    "decay": [3.2, 2.2, 1.6, 1.1, 0.9, 0.7, 0.5, 0.4], "inharm": 0.0004,
                    "a": 0.003, "d": 4.0, "s": 0.0, "r": 0.35, "gain": 0.22},
    "electric_piano": {"name": "Electric Piano", "group": "Keys", "engine": "fm",
                       "ratio": 1.0, "index": 2.2, "index_decay": 0.9, "tine": 0.18,
                       "a": 0.002, "d": 3.0, "s": 0.0, "r": 0.4, "gain": 0.35},
    "organ": {"name": "Tonewheel Organ", "group": "Keys", "engine": "additive",
              "partials": [0.8, 1.0, 0.6, 0.0, 0.45, 0.0, 0.0, 0.3],
              "ratios": [0.5, 1, 1.5, 2, 3, 4, 5, 6], "decay": None, "inharm": 0.0,
              "a": 0.008, "d": 0.1, "s": 1.0, "r": 0.06, "gain": 0.16, "click": 0.15},
    "strings": {"name": "String Ensemble", "group": "Pads", "engine": "subtractive",
                "wave": "saw", "detune": 0.12, "voices": 3, "cutoff": 2600, "keytrack": 0.3,
                "env_amt": 0.0, "q": 0.6, "a": 0.45, "d": 0.5, "s": 0.85, "r": 0.9, "gain": 0.42},
    "warm_pad": {"name": "Warm Pad", "group": "Pads", "engine": "subtractive",
                 "wave": "saw", "detune": 0.2, "voices": 3, "cutoff": 1100, "keytrack": 0.2,
                 "env_amt": 1200, "q": 0.9, "a": 0.9, "d": 1.2, "s": 0.7, "r": 1.6, "gain": 0.5},
    "synth_lead": {"name": "Analog Lead", "group": "Synth", "engine": "subtractive",
                   "wave": "square", "detune": 0.07, "voices": 2, "cutoff": 2400, "keytrack": 0.6,
                   "env_amt": 2500, "q": 2.2, "a": 0.005, "d": 0.25, "s": 0.7, "r": 0.2, "gain": 0.24},
    "synth_bass": {"name": "Synth Bass", "group": "Bass", "engine": "subtractive",
                   "wave": "saw", "detune": 0.03, "voices": 2, "cutoff": 500, "keytrack": 0.4,
                   "env_amt": 1800, "q": 3.0, "a": 0.002, "d": 0.28, "s": 0.4, "r": 0.12, "gain": 0.36},
    "clean_guitar": {"name": "Clean Electric Guitar", "group": "Guitar", "engine": "ks",
                     "brightness": 0.55, "sustain": 0.996, "pick": 0.5,
                     "a": 0.001, "d": 6.0, "s": 0.0, "r": 0.15, "gain": 0.5},
    "nylon_guitar": {"name": "Nylon Guitar", "group": "Guitar", "engine": "ks",
                     "brightness": 0.3, "sustain": 0.993, "pick": 0.3,
                     "a": 0.001, "d": 4.0, "s": 0.0, "r": 0.2, "gain": 0.55},
    "bells": {"name": "Crystal Bells", "group": "Mallets", "engine": "fm",
              "ratio": 3.5, "index": 3.0, "index_decay": 1.8, "tine": 0.0,
              "a": 0.001, "d": 3.5, "s": 0.0, "r": 1.0, "gain": 0.25},
}


class Envelope:
    def __init__(self, sr, a, d, s, r):
        self.sr, self.a, self.d, self.s, self.r = sr, max(a, 1e-4), max(d, 1e-3), s, max(r, 1e-3)
        self.stage = "a"
        self.level = 0.0

    def release(self):
        if self.stage != "off":
            self.stage = "r"

    def block(self, n: int) -> np.ndarray:
        out = np.zeros(n, dtype=np.float32)
        i = 0
        while i < n:
            rem = n - i
            if self.stage == "a":
                rate = 1.0 / (self.a * self.sr)
                k = min(rem, max(1, int(math.ceil((1.0 - self.level) / rate))))
                seg = self.level + rate * np.arange(1, k + 1)
                np.minimum(seg, 1.0, out=seg)
                out[i:i + k] = seg
                self.level = float(seg[-1])
                if self.level >= 1.0:
                    self.stage = "d"
                i += k
            elif self.stage == "d":
                coef = math.exp(-1.0 / (self.d * self.sr / 4.0))
                seg = self.s + (self.level - self.s) * coef ** np.arange(1, rem + 1)
                out[i:] = seg
                self.level = float(seg[-1])
                if self.s <= 0.0 and self.level < 1e-4:
                    self.stage = "off"
                i = n
            elif self.stage == "r":
                coef = math.exp(-1.0 / (self.r * self.sr / 4.0))
                seg = self.level * coef ** np.arange(1, rem + 1)
                out[i:] = seg
                self.level = float(seg[-1])
                if self.level < 1e-4:
                    self.stage = "off"
                i = n
            else:
                i = n
        return out


class Voice:
    def __init__(self, sr, patch, note, velocity, age_counter):
        self.sr = sr
        self.patch = patch
        self.note = note
        self.vel = (velocity / 127.0) ** 1.4
        self.freq = 440.0 * 2 ** ((note - 69) / 12.0)
        self.env = Envelope(sr, patch["a"], patch["d"], patch["s"], patch["r"])
        self.born = age_counter
        self.t = 0  # samples since note on
        self.held = True
        self.sustained = False
        eng = patch["engine"]
        if eng == "additive":
            nparts = len(patch["partials"])
            self.phases = np.zeros(nparts)
        elif eng == "fm":
            self.pc = 0.0
            self.pm = 0.0
        elif eng == "subtractive":
            self.phases = np.random.rand(patch.get("voices", 1))
            self.filt = Biquad(sr, "lowpass", patch["cutoff"], patch["q"], channels=1)
        elif eng == "ks":
            period = sr / self.freq
            self.P = max(2, int(period))
            self.frac = period - self.P
            size = self.P + 2
            noise = np.random.uniform(-1, 1, size)
            # pick position / brightness shaping of the initial excitation
            b = patch["brightness"]
            for _ in range(int((1 - b) * 4)):
                noise = 0.5 * (noise + np.roll(noise, 1))
            self.hist = (noise * self.vel).astype(np.float64)  # ring of last P+2 samples
            self.loss = patch["sustain"]

    @property
    def done(self):
        return self.env.stage == "off"

    def render(self, n: int, bend: float) -> np.ndarray:
        p = self.patch
        sr = self.sr
        f = self.freq * bend
        eng = p["engine"]
        if eng == "additive":
            ratios = p.get("ratios") or list(range(1, len(p["partials"]) + 1))
            out = np.zeros(n)
            t = (self.t + np.arange(n)) / sr
            for k, (amp, ratio) in enumerate(zip(p["partials"], ratios)):
                if amp <= 0:
                    continue
                fk = f * ratio * (1 + p.get("inharm", 0) * (ratio ** 2))
                if fk > sr * 0.45:
                    continue
                inc = TWO_PI * fk / sr
                ph = self.phases[k] + inc * np.arange(n)
                self.phases[k] = (ph[-1] + inc) % TWO_PI
                part = amp * np.sin(ph)
                if p.get("decay"):
                    part *= np.exp(-t / p["decay"][k])
                out += part
            cnt = max(0, min(n, int(sr * 0.01) - self.t))
            if p.get("click") and cnt:
                out[:cnt] += np.random.uniform(-1, 1, cnt) * p["click"] * 0.2
        elif eng == "fm":
            t = (self.t + np.arange(n)) / sr
            incc = TWO_PI * f / sr
            incm = TWO_PI * f * p["ratio"] / sr
            pc = self.pc + incc * np.arange(n)
            pm = self.pm + incm * np.arange(n)
            self.pc = (pc[-1] + incc) % TWO_PI
            self.pm = (pm[-1] + incm) % TWO_PI
            index = p["index"] * self.vel * np.exp(-t / p["index_decay"]) + 0.2
            out = np.sin(pc + index * np.sin(pm))
            if p.get("tine"):
                out += p["tine"] * np.sin(pm * 14) * np.exp(-t / 0.08)
        elif eng == "subtractive":
            nv = p.get("voices", 1)
            out = np.zeros(n)
            for v in range(nv):
                det = 1 + p["detune"] * 0.01 * (v - (nv - 1) / 2)
                inc = f * det / sr
                ph = (self.phases[v] + inc * np.arange(n)) % 1.0
                self.phases[v] = (ph[-1] + inc) % 1.0
                if p["wave"] == "saw":
                    out += 2 * ph - 1
                else:
                    out += np.where(ph < 0.5, 1.0, -1.0)
            out /= nv
            cutoff = p["cutoff"] * (1 + p["keytrack"] * (self.freq / 261.6 - 1)) + p["env_amt"] * self.env.level
            self.filt.set("lowpass", min(cutoff, sr * 0.45), p["q"])
            out = self.filt.process(out[np.newaxis, :].astype(np.float32))[0].astype(np.float64)
        elif eng == "ks":
            out = np.empty(n)
            P = self.P
            c = 0.5 * self.loss
            i = 0
            h = self.hist  # oldest -> newest, length P+2
            while i < n:
                L = min(P, n - i)
                # y[k] = c * (y[k-P] + y[k-P-1])
                base = len(h) - P
                seg = c * (h[base:base + L] + h[base - 1:base - 1 + L])
                out[i:i + L] = seg
                h = np.concatenate([h[L:], seg])
                i += L
            self.hist = h
        else:
            out = np.zeros(n)
        env = self.env.block(n)
        self.t += n
        return (out * env * self.vel).astype(np.float32)


class Synth:
    MAX_VOICES = 16

    def __init__(self, sr: float, patch_id: str = "grand_piano"):
        self.sr = sr
        self.voices: list[Voice] = []
        self.patch_id = patch_id if patch_id in PATCHES else "grand_piano"
        self.bend = 1.0
        self.sustain = False
        self.counter = 0
        self.level = 1.0

    @property
    def patch(self):
        return PATCHES[self.patch_id]

    def set_patch(self, patch_id: str):
        if patch_id not in PATCHES:
            raise KeyError(patch_id)
        self.patch_id = patch_id
        self.all_notes_off()

    def note_on(self, note: int, velocity: int):
        if velocity == 0:
            return self.note_off(note)
        # retrigger same note
        for v in self.voices:
            if v.note == note and v.held:
                v.held = False
                v.env.release()
        if len(self.voices) >= self.MAX_VOICES:
            self.voices.sort(key=lambda v: (v.held, v.born))
            self.voices.pop(0)
        self.counter += 1
        self.voices.append(Voice(self.sr, self.patch, note, velocity, self.counter))

    def note_off(self, note: int):
        for v in self.voices:
            if v.note == note and v.held:
                v.held = False
                if self.sustain:
                    v.sustained = True
                else:
                    v.env.release()

    def set_sustain(self, on: bool):
        self.sustain = on
        if not on:
            for v in self.voices:
                if v.sustained:
                    v.sustained = False
                    v.env.release()

    def pitch_bend(self, value14: int, semitones: float = 2.0):
        self.bend = 2 ** (((value14 - 8192) / 8192.0) * semitones / 12.0)

    def all_notes_off(self):
        self.voices.clear()
        self.sustain = False

    def render(self, n: int) -> np.ndarray:
        mono = np.zeros(n, dtype=np.float32)
        for v in self.voices:
            mono += v.render(n, self.bend)
        self.voices = [v for v in self.voices if not v.done]
        mono *= self.patch["gain"] * self.level
        return np.vstack([mono, mono])

    @property
    def active_notes(self) -> list[int]:
        return sorted({v.note for v in self.voices if v.held or v.sustained})


def patch_catalogue() -> list[dict]:
    return [{"id": k, "name": v["name"], "group": v["group"], "engine": v["engine"], "kind": "builtin"}
            for k, v in PATCHES.items()]
