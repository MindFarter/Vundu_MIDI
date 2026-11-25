"""Built-in DSP effects for Vundu_MIDI.

Every effect works on planar stereo float32 blocks shaped (2, frames),
the same layout Minihost uses, so built-in effects and hosted plugins
can sit side by side in one chain.

All effects are pure numpy / scipy and vectorised per block so they run
in real time on a Raspberry Pi 5.
"""

from __future__ import annotations

import math
import uuid

import numpy as np
from scipy.signal import lfilter


def db_to_lin(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def lin_to_db(x: float, floor: float = -120.0) -> float:
    if x <= 1e-9:
        return floor
    return max(floor, 20.0 * math.log10(x))


# --------------------------------------------------------------------------- #
# Building blocks
# --------------------------------------------------------------------------- #


class Biquad:
    """RBJ cookbook biquad, stereo, with persistent state."""

    def __init__(self, sr: float, kind: str = "lowpass", freq: float = 1000.0,
                 q: float = 0.707, gain_db: float = 0.0, channels: int = 2):
        self.sr = sr
        self.channels = channels
        self.zi = np.zeros((channels, 2), dtype=np.float64)
        self.set(kind, freq, q, gain_db)

    def set(self, kind: str, freq: float, q: float = 0.707, gain_db: float = 0.0):
        self.kind, self.freq, self.q, self.gain_db = kind, freq, q, gain_db
        sr = self.sr
        freq = min(max(freq, 10.0), sr * 0.45)
        a_gain = 10 ** (gain_db / 40.0)
        w0 = 2 * math.pi * freq / sr
        cw, sw = math.cos(w0), math.sin(w0)
        alpha = sw / (2 * max(q, 1e-3))
        if kind == "lowpass":
            b = [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]
            a = [1 + alpha, -2 * cw, 1 - alpha]
        elif kind == "highpass":
            b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
            a = [1 + alpha, -2 * cw, 1 - alpha]
        elif kind == "bandpass":
            b = [alpha, 0.0, -alpha]
            a = [1 + alpha, -2 * cw, 1 - alpha]
        elif kind == "peak":
            b = [1 + alpha * a_gain, -2 * cw, 1 - alpha * a_gain]
            a = [1 + alpha / a_gain, -2 * cw, 1 - alpha / a_gain]
        elif kind == "lowshelf":
            sq = 2 * math.sqrt(a_gain) * alpha
            b = [a_gain * ((a_gain + 1) - (a_gain - 1) * cw + sq),
                 2 * a_gain * ((a_gain - 1) - (a_gain + 1) * cw),
                 a_gain * ((a_gain + 1) - (a_gain - 1) * cw - sq)]
            a = [(a_gain + 1) + (a_gain - 1) * cw + sq,
                 -2 * ((a_gain - 1) + (a_gain + 1) * cw),
                 (a_gain + 1) + (a_gain - 1) * cw - sq]
        elif kind == "highshelf":
            sq = 2 * math.sqrt(a_gain) * alpha
            b = [a_gain * ((a_gain + 1) + (a_gain - 1) * cw + sq),
                 -2 * a_gain * ((a_gain - 1) + (a_gain + 1) * cw),
                 a_gain * ((a_gain + 1) + (a_gain - 1) * cw - sq)]
            a = [(a_gain + 1) - (a_gain - 1) * cw + sq,
                 2 * ((a_gain - 1) - (a_gain + 1) * cw),
                 (a_gain + 1) - (a_gain - 1) * cw - sq]
        else:
            raise ValueError(kind)
        a0 = a[0]
        self.b = np.array(b, dtype=np.float64) / a0
        self.a = np.array(a, dtype=np.float64) / a0

    def process(self, x: np.ndarray) -> np.ndarray:
        y, self.zi = lfilter(self.b, self.a, x, axis=1, zi=self.zi)
        return y.astype(np.float32, copy=False)

    def reset(self):
        self.zi[:] = 0.0


class DelayLine:
    """Stereo circular buffer. Reads of `length` samples at a delay of
    `delay` samples are valid whenever length <= delay."""

    def __init__(self, max_samples: int, channels: int = 2):
        self.size = int(max_samples) + 4
        self.buf = np.zeros((channels, self.size), dtype=np.float32)
        self.pos = 0

    def read(self, delay: int, length: int) -> np.ndarray:
        start = (self.pos - delay) % self.size
        idx = (start + np.arange(length)) % self.size
        return self.buf[:, idx]

    def write(self, x: np.ndarray):
        n = x.shape[1]
        idx = (self.pos + np.arange(n)) % self.size
        self.buf[:, idx] = x
        self.pos = (self.pos + n) % self.size

    def read_frac(self, delays: np.ndarray) -> np.ndarray:
        """Fractional read *after* the block has been written. `delays` is
        per-sample delay relative to each sample of the last written block."""
        n = delays.shape[0]
        base = (self.pos - n + np.arange(n)) - delays
        i0 = np.floor(base).astype(np.int64)
        frac = (base - i0).astype(np.float32)
        a = self.buf[:, i0 % self.size]
        b = self.buf[:, (i0 + 1) % self.size]
        return a + (b - a) * frac

    def clear(self):
        self.buf[:] = 0.0


def one_pole_coef(ms: float, sr: float) -> float:
    if ms <= 0:
        return 0.0
    return math.exp(-1.0 / (ms * 0.001 * sr))


# --------------------------------------------------------------------------- #
# Effect base
# --------------------------------------------------------------------------- #


def P(pid, name, lo, hi, default, unit="", step=None, choices=None):
    d = {"id": pid, "name": name, "min": lo, "max": hi, "default": default, "unit": unit}
    if step is not None:
        d["step"] = step
    if choices is not None:
        d["choices"] = choices
    return d


class Effect:
    TYPE = "base"
    NAME = "Effect"
    CATEGORY = "Utility"
    COLOR = "#ffb347"
    DESCRIPTION = ""
    PARAMS: list[dict] = []

    def __init__(self, sr: float):
        self.sr = float(sr)
        self.slot_id = uuid.uuid4().hex[:8]
        self.enabled = True
        self.params = {p["id"]: p["default"] for p in self.PARAMS}
        self.meter_gr_db = 0.0  # gain reduction, for dynamics effects
        self.setup()
        self.update()

    # hooks -------------------------------------------------------------
    def setup(self):
        pass

    def update(self):
        """Recalculate coefficients after a parameter change."""

    def reset(self):
        pass

    def _process(self, x: np.ndarray) -> np.ndarray:
        return x

    # public ------------------------------------------------------------
    def process(self, x: np.ndarray) -> np.ndarray:
        if not self.enabled:
            return x
        return self._process(x)

    def set_param(self, pid: str, value) -> None:
        spec = next((p for p in self.PARAMS if p["id"] == pid), None)
        if spec is None:
            raise KeyError(pid)
        value = float(value)
        value = min(max(value, spec["min"]), spec["max"])
        if spec.get("step") == 1:
            value = round(value)
        self.params[pid] = value
        self.update()

    def set_params(self, params: dict):
        for k, v in (params or {}).items():
            if k in self.params:
                spec = next(p for p in self.PARAMS if p["id"] == k)
                v = min(max(float(v), spec["min"]), spec["max"])
                self.params[k] = v
        self.update()

    def describe(self) -> dict:
        return {
            "slot_id": self.slot_id,
            "type": self.TYPE,
            "name": self.NAME,
            "category": self.CATEGORY,
            "color": self.COLOR,
            "kind": "builtin",
            "enabled": self.enabled,
            "gr_db": round(self.meter_gr_db, 2),
            "params": [dict(p, value=self.params[p["id"]]) for p in self.PARAMS],
        }

    def to_preset(self) -> dict:
        return {"type": self.TYPE, "enabled": self.enabled, "params": dict(self.params)}

    @classmethod
    def catalogue_entry(cls) -> dict:
        return {"type": cls.TYPE, "name": cls.NAME, "category": cls.CATEGORY,
                "color": cls.COLOR, "description": cls.DESCRIPTION, "kind": "builtin",
                "params": cls.PARAMS}


def mix(dry: np.ndarray, wet: np.ndarray, amount: float) -> np.ndarray:
    return dry * (1.0 - amount) + wet * amount


# --------------------------------------------------------------------------- #
# Effects
# --------------------------------------------------------------------------- #


class NoiseGate(Effect):
    TYPE, NAME, CATEGORY, COLOR = "gate", "Noise Gate", "Dynamics", "#7fd1ff"
    DESCRIPTION = "Silences hum and hiss between notes."
    PARAMS = [P("threshold", "Thresh", -90, 0, -55, "dB"),
              P("attack", "Attack", 0.1, 50, 1, "ms"),
              P("release", "Release", 5, 1000, 120, "ms"),
              P("range", "Range", -90, 0, -80, "dB")]

    def setup(self):
        self.env = 0.0
        self.g = 1.0

    def _process(self, x):
        n = x.shape[1]
        thr = db_to_lin(self.params["threshold"])
        floor = db_to_lin(self.params["range"])
        att = one_pole_coef(self.params["attack"], self.sr)
        rel = one_pole_coef(self.params["release"], self.sr)
        det = np.max(np.abs(x), axis=0)
        env, zi = lfilter([1 - 0.995], [1, -0.995], det, zi=[self.env * 0.995])
        self.env = float(env[-1])
        target = np.where(env > thr, 1.0, floor)
        # smooth the gain: faster when opening, slower when closing
        coef = att if target[-1] > self.g else rel
        g, _ = lfilter([1 - coef], [1, -coef], target, zi=[self.g * coef])
        self.g = float(g[-1])
        self.meter_gr_db = lin_to_db(float(np.min(g)), -90)
        return (x * g.astype(np.float32)).astype(np.float32)


class Compressor(Effect):
    TYPE, NAME, CATEGORY, COLOR = "compressor", "Compressor", "Dynamics", "#ff6b6b"
    DESCRIPTION = "Feed-forward VCA style compressor with soft knee."
    PARAMS = [P("threshold", "Thresh", -60, 0, -18, "dB"),
              P("ratio", "Ratio", 1, 20, 4, ":1"),
              P("attack", "Attack", 0.1, 200, 10, "ms"),
              P("release", "Release", 10, 2000, 150, "ms"),
              P("knee", "Knee", 0, 24, 6, "dB"),
              P("makeup", "Makeup", 0, 24, 0, "dB"),
              P("mix", "Mix", 0, 1, 1, "")]
    CHUNK = 32

    def setup(self):
        self.gr = 0.0  # current gain reduction in dB (positive number)

    def _gain_computer(self, level_db):
        t, r, k = self.params["threshold"], self.params["ratio"], self.params["knee"]
        over = level_db - t
        if k > 0 and abs(over) <= k / 2:
            return (1 / r - 1) * (over + k / 2) ** 2 / (2 * k)
        if over > k / 2:
            return over * (1 / r - 1)
        return 0.0

    def _process(self, x):
        n = x.shape[1]
        ch = self.CHUNK
        chunks = max(1, n // ch)
        att = one_pole_coef(self.params["attack"], self.sr / ch)
        rel = one_pole_coef(self.params["release"], self.sr / ch)
        det = np.max(np.abs(x), axis=0)
        gains = np.empty(chunks + 1, dtype=np.float64)
        gains[0] = -self.gr
        gr = self.gr
        max_gr = 0.0
        for i in range(chunks):
            seg = det[i * ch:(i + 1) * ch] if i < chunks - 1 else det[i * ch:]
            lvl = lin_to_db(float(seg.max()) if seg.size else 0.0)
            target = -self._gain_computer(lvl)  # positive reduction
            coef = att if target > gr else rel
            gr = coef * gr + (1 - coef) * target
            gains[i + 1] = -gr
            max_gr = max(max_gr, gr)
        self.gr = gr
        self.meter_gr_db = -max_gr
        # interpolate chunk gains to per-sample
        pts = np.linspace(0, n, chunks + 1)
        g_db = np.interp(np.arange(n), pts, gains) + self.params["makeup"]
        g = np.power(10.0, g_db / 20.0).astype(np.float32)
        wet = x * g
        m = self.params["mix"]
        return wet if m >= 0.999 else mix(x, wet, m)


class Overdrive(Effect):
    TYPE, NAME, CATEGORY, COLOR = "overdrive", "Overdrive", "Drive", "#ffa94d"
    DESCRIPTION = "Warm tube-style soft clipping."
    PARAMS = [P("drive", "Drive", 0, 40, 18, "dB"),
              P("tone", "Tone", 500, 8000, 3200, "Hz"),
              P("level", "Level", -24, 12, -6, "dB"),
              P("mix", "Mix", 0, 1, 1, "")]

    def setup(self):
        self.hp = Biquad(self.sr, "highpass", 90)
        self.lp = Biquad(self.sr, "lowpass", 3200)

    def update(self):
        self.lp.set("lowpass", self.params["tone"], 0.707)

    def _process(self, x):
        g = db_to_lin(self.params["drive"])
        y = self.hp.process(x)
        y = np.tanh(y * g + 0.08) - math.tanh(0.08)  # slight asymmetry = even harmonics
        y = self.lp.process(y.astype(np.float32)) * db_to_lin(self.params["level"])
        return mix(x, y, self.params["mix"]).astype(np.float32)


class Distortion(Effect):
    TYPE, NAME, CATEGORY, COLOR = "distortion", "Distortion", "Drive", "#ff5e3a"
    DESCRIPTION = "Hard-clipping fuzz / distortion."
    PARAMS = [P("gain", "Gain", 0, 50, 30, "dB"),
              P("tone", "Tone", 400, 9000, 2500, "Hz"),
              P("fuzz", "Fuzz", 0, 1, 0.3, ""),
              P("level", "Level", -30, 6, -14, "dB")]

    def setup(self):
        self.hp = Biquad(self.sr, "highpass", 120)
        self.lp = Biquad(self.sr, "lowpass", 2500)

    def update(self):
        self.lp.set("lowpass", self.params["tone"], 0.8)

    def _process(self, x):
        y = self.hp.process(x) * db_to_lin(self.params["gain"])
        f = self.params["fuzz"]
        hard = np.clip(y, -0.6, 0.6) / 0.6
        soft = np.tanh(y)
        y = soft * (1 - f) + np.sign(hard) * np.abs(hard) ** 0.5 * f
        return (self.lp.process(y.astype(np.float32)) * db_to_lin(self.params["level"])).astype(np.float32)


class AmpSim(Effect):
    TYPE, NAME, CATEGORY, COLOR = "amp", "Amp & Cab", "Amp", "#ffd43b"
    DESCRIPTION = "Guitar amp tone stack and speaker cabinet simulation."
    PARAMS = [P("gain", "Gain", 0, 40, 12, "dB"),
              P("bass", "Bass", -12, 12, 2, "dB"),
              P("middle", "Middle", -12, 12, 0, "dB"),
              P("treble", "Treble", -12, 12, 1, "dB"),
              P("presence", "Presence", -12, 12, 2, "dB"),
              P("master", "Master", -30, 6, -8, "dB"),
              P("cab", "Cab", 0, 2, 1, "", step=1, choices=["Off", "1x12", "4x12"])]

    def setup(self):
        self.hp = Biquad(self.sr, "highpass", 80)
        self.bass = Biquad(self.sr, "lowshelf", 120)
        self.mid = Biquad(self.sr, "peak", 650, 0.8)
        self.treb = Biquad(self.sr, "highshelf", 2800)
        self.pres = Biquad(self.sr, "peak", 4200, 1.2)
        self.cab_lp = Biquad(self.sr, "lowpass", 5200, 0.9)
        self.cab_res = Biquad(self.sr, "peak", 110, 1.5, 3)

    def update(self):
        p = self.params
        self.bass.set("lowshelf", 120, 0.7, p["bass"])
        self.mid.set("peak", 650, 0.8, p["middle"])
        self.treb.set("highshelf", 2800, 0.7, p["treble"])
        self.pres.set("peak", 4200, 1.2, p["presence"])
        cab = int(p["cab"])
        self.cab_lp.set("lowpass", 6500 if cab == 1 else 4200, 0.9)
        self.cab_res.set("peak", 140 if cab == 1 else 95, 1.4, 3 if cab else 0)

    def _process(self, x):
        p = self.params
        y = self.hp.process(x) * db_to_lin(p["gain"])
        y = np.tanh(y).astype(np.float32)
        for f in (self.bass, self.mid, self.treb, self.pres):
            y = f.process(y)
        if int(p["cab"]):
            y = self.cab_res.process(self.cab_lp.process(y))
        return (y * db_to_lin(p["master"])).astype(np.float32)


class EQ3(Effect):
    TYPE, NAME, CATEGORY, COLOR = "eq3", "3-Band EQ", "EQ", "#69db7c"
    DESCRIPTION = "Console style low shelf, sweepable mid, high shelf."
    PARAMS = [P("low", "Low", -15, 15, 0, "dB"),
              P("low_freq", "Lo Freq", 40, 500, 120, "Hz"),
              P("mid", "Mid", -15, 15, 0, "dB"),
              P("mid_freq", "Mid Freq", 200, 6000, 1000, "Hz"),
              P("mid_q", "Mid Q", 0.3, 6, 1, ""),
              P("high", "High", -15, 15, 0, "dB"),
              P("high_freq", "Hi Freq", 1500, 16000, 6000, "Hz")]

    def setup(self):
        self.lo = Biquad(self.sr, "lowshelf", 120)
        self.md = Biquad(self.sr, "peak", 1000)
        self.hi = Biquad(self.sr, "highshelf", 6000)

    def update(self):
        p = self.params
        self.lo.set("lowshelf", p["low_freq"], 0.707, p["low"])
        self.md.set("peak", p["mid_freq"], p["mid_q"], p["mid"])
        self.hi.set("highshelf", p["high_freq"], 0.707, p["high"])

    def _process(self, x):
        return self.hi.process(self.md.process(self.lo.process(x)))


class Chorus(Effect):
    TYPE, NAME, CATEGORY, COLOR = "chorus", "Chorus", "Modulation", "#b197fc"
    DESCRIPTION = "Stereo analogue-style chorus."
    PARAMS = [P("rate", "Rate", 0.05, 8, 0.8, "Hz"),
              P("depth", "Depth", 0.1, 10, 3, "ms"),
              P("delay", "Delay", 2, 30, 8, "ms"),
              P("mix", "Mix", 0, 1, 0.5, "")]

    def setup(self):
        self.line = DelayLine(int(self.sr * 0.06))
        self.phase = 0.0

    def _process(self, x):
        n = x.shape[1]
        self.line.write(x)
        t = self.phase + np.arange(n) * (2 * math.pi * self.params["rate"] / self.sr)
        self.phase = float(t[-1] + 2 * math.pi * self.params["rate"] / self.sr) % (2 * math.pi)
        base = self.params["delay"] * 0.001 * self.sr
        depth = self.params["depth"] * 0.001 * self.sr
        dl = base + depth * 0.5 * (1 + np.sin(t))
        dr = base + depth * 0.5 * (1 + np.sin(t + math.pi / 2))
        wl = self.line.read_frac(dl)[0]
        wr = self.line.read_frac(dr)[1]
        wet = np.vstack([wl, wr]).astype(np.float32)
        return mix(x, wet, self.params["mix"]).astype(np.float32)


class Tremolo(Effect):
    TYPE, NAME, CATEGORY, COLOR = "tremolo", "Tremolo", "Modulation", "#e599f7"
    DESCRIPTION = "Amplitude modulation with sine or square shape."
    PARAMS = [P("rate", "Rate", 0.5, 15, 5, "Hz"),
              P("depth", "Depth", 0, 1, 0.6, ""),
              P("shape", "Shape", 0, 1, 0, "", step=1, choices=["Sine", "Square"]),
              P("stereo", "Stereo", 0, 1, 0, "", step=1, choices=["Mono", "Pan"])]

    def setup(self):
        self.phase = 0.0

    def _process(self, x):
        n = x.shape[1]
        inc = 2 * math.pi * self.params["rate"] / self.sr
        t = self.phase + np.arange(n) * inc
        self.phase = float(t[-1] + inc) % (2 * math.pi)
        lfo = np.sin(t)
        if int(self.params["shape"]):
            lfo = np.tanh(lfo * 6)
        d = self.params["depth"]
        gl = 1 - d * 0.5 * (1 + lfo)
        gr = 1 - d * 0.5 * (1 - lfo) if int(self.params["stereo"]) else gl
        return np.vstack([x[0] * gl, x[1] * gr]).astype(np.float32)


class Delay(Effect):
    TYPE, NAME, CATEGORY, COLOR = "delay", "Tape Delay", "Time", "#4dabf7"
    DESCRIPTION = "Echo with feedback, damping and ping-pong."
    MAX_MS = 2000
    PARAMS = [P("time", "Time", 20, 2000, 380, "ms"),
              P("feedback", "Feedback", 0, 0.95, 0.35, ""),
              P("tone", "Tone", 800, 12000, 4500, "Hz"),
              P("pingpong", "Ping-Pong", 0, 1, 0, "", step=1, choices=["Off", "On"]),
              P("mix", "Mix", 0, 1, 0.3, "")]

    def setup(self):
        self.line = DelayLine(int(self.sr * self.MAX_MS / 1000) + 8)
        self.damp = Biquad(self.sr, "lowpass", 4500)

    def update(self):
        self.damp.set("lowpass", self.params["tone"], 0.6)

    def reset(self):
        self.line.clear()

    def _process(self, x):
        d = max(1, int(self.params["time"] * 0.001 * self.sr))
        fb = self.params["feedback"]
        pp = int(self.params["pingpong"])
        n = x.shape[1]
        out = np.empty_like(x)
        i = 0
        while i < n:
            L = min(d, n - i)
            delayed = self.line.read(d, L)
            seg = x[:, i:i + L]
            if pp:
                fbk = np.vstack([delayed[1], delayed[0]])
                inp = np.vstack([seg.mean(axis=0), np.zeros(L, np.float32)])
            else:
                fbk, inp = delayed, seg
            self.line.write((inp + fbk * fb).astype(np.float32))
            out[:, i:i + L] = delayed
            i += L
        wet = self.damp.process(out)
        return mix(x, wet, self.params["mix"]).astype(np.float32)


class Reverb(Effect):
    """Freeverb-style reverb: 8 damped combs + 4 allpasses per side.

    All 16 comb lines are processed together as one matrix, so the cost
    is a handful of numpy calls per chunk regardless of comb count."""

    TYPE, NAME, CATEGORY, COLOR = "reverb", "Plate Reverb", "Time", "#38d9a9"
    DESCRIPTION = "Lush studio plate / hall reverb."
    COMBS = [1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617]
    ALLPASS = [556, 441, 341, 225]
    SPREAD = 23
    PARAMS = [P("size", "Size", 0, 1, 0.7, ""),
              P("damping", "Damping", 0, 1, 0.4, ""),
              P("predelay", "Pre-Delay", 0, 200, 15, "ms"),
              P("width", "Width", 0, 1, 1, ""),
              P("mix", "Mix", 0, 1, 0.25, "")]

    def setup(self):
        scale = self.sr / 44100.0
        cd = []
        for c in self.COMBS:
            cd += [int(c * scale), int((c + self.SPREAD) * scale)]
        self.comb_d = np.array(cd, dtype=np.int64)          # rows: L,R,L,R...
        self.comb_size = int(self.comb_d.max()) + 4096
        self.comb_buf = np.zeros((len(cd), self.comb_size), dtype=np.float64)
        self.comb_lp = np.zeros((len(cd), 1))
        self.comb_pos = 0
        self.ap_d = [np.array([int(a * scale), int((a + self.SPREAD) * scale)], dtype=np.int64)
                     for a in self.ALLPASS]
        self.ap_size = int(max(d.max() for d in self.ap_d)) + 4096
        self.ap_buf = [np.zeros((2, self.ap_size)) for _ in self.ap_d]
        self.ap_pos = 0
        self.pre = DelayLine(int(self.sr * 0.21) + 8)
        self.chunk = int(min(d.min() for d in self.ap_d))

    def reset(self):
        self.comb_buf[:] = 0
        self.comb_lp[:] = 0
        for b in self.ap_buf:
            b[:] = 0
        self.pre.clear()

    def _core(self, mono: np.ndarray) -> np.ndarray:
        L = mono.shape[0]
        ar = np.arange(L)
        g = 0.7 + 0.28 * self.params["size"]
        damp = 0.05 + 0.6 * self.params["damping"]
        # combs (all rows at once)
        ridx = (self.comb_pos - self.comb_d[:, None] + ar) % self.comb_size
        hist = np.take_along_axis(self.comb_buf, ridx, axis=1)
        lp, zf = lfilter([1 - damp], [1, -damp], hist, axis=1, zi=self.comb_lp * damp)
        self.comb_lp = lp[:, -1:].copy()
        widx = (self.comb_pos + ar) % self.comb_size
        self.comb_buf[:, widx] = mono[None, :] + g * lp
        self.comb_pos = (self.comb_pos + L) % self.comb_size
        acc = np.vstack([hist[0::2].sum(axis=0), hist[1::2].sum(axis=0)])
        # allpasses in series
        widx = (self.ap_pos + ar) % self.ap_size
        for d, buf in zip(self.ap_d, self.ap_buf):
            ridx = (self.ap_pos - d[:, None] + ar) % self.ap_size
            bufout = np.take_along_axis(buf, ridx, axis=1)
            buf[:, widx] = acc + bufout * 0.5
            acc = bufout - acc
        self.ap_pos = (self.ap_pos + L) % self.ap_size
        return acc

    def _process(self, x):
        n = x.shape[1]
        pd = int(self.params["predelay"] * 0.001 * self.sr)
        if pd >= 64:  # tiny pre-delays are inaudible; skip them
            d_in = np.empty_like(x)
            i = 0
            while i < n:
                L = min(pd, n - i)
                d_in[:, i:i + L] = self.pre.read(pd, L)
                self.pre.write(x[:, i:i + L])
                i += L
        else:
            d_in = x
        mono = (d_in[0] + d_in[1]).astype(np.float64) * 0.015
        wet = np.empty((2, n))
        i = 0
        while i < n:
            L = min(self.chunk, n - i)
            wet[:, i:i + L] = self._core(mono[i:i + L])
            i += L
        w = self.params["width"]
        wl = wet[0] * (0.5 + w / 2) + wet[1] * (0.5 - w / 2)
        wr = wet[1] * (0.5 + w / 2) + wet[0] * (0.5 - w / 2)
        wet = np.vstack([wl, wr]).astype(np.float32) * 3.0
        return mix(x, wet, self.params["mix"]).astype(np.float32)


class Utility(Effect):
    TYPE, NAME, CATEGORY, COLOR = "utility", "Gain / Pan", "Utility", "#ced4da"
    DESCRIPTION = "Gain, pan, polarity and mono switch."
    PARAMS = [P("gain", "Gain", -40, 24, 0, "dB"),
              P("pan", "Pan", -1, 1, 0, ""),
              P("mono", "Mono", 0, 1, 0, "", step=1, choices=["Stereo", "Mono"]),
              P("invert", "Phase", 0, 1, 0, "", step=1, choices=["Normal", "Invert"])]

    def _process(self, x):
        p = self.params
        y = x * db_to_lin(p["gain"])
        if int(p["mono"]):
            m = y.mean(axis=0)
            y = np.vstack([m, m])
        if int(p["invert"]):
            y = -y
        pan = p["pan"]
        a = (pan + 1) * math.pi / 4
        y = np.vstack([y[0] * math.cos(a) * 1.414, y[1] * math.sin(a) * 1.414])
        return y.astype(np.float32)


BUILTIN_EFFECTS: dict[str, type[Effect]] = {
    cls.TYPE: cls for cls in (NoiseGate, Compressor, Overdrive, Distortion, AmpSim,
                              EQ3, Chorus, Tremolo, Delay, Reverb, Utility)
}


def create_effect(effect_type: str, sr: float) -> Effect:
    try:
        return BUILTIN_EFFECTS[effect_type](sr)
    except KeyError:
        raise KeyError(f"unknown effect type: {effect_type}") from None


# --------------------------------------------------------------------------- #
# Master section: the console's main bus
# --------------------------------------------------------------------------- #


class MasterBus:
    """Master channel: input trim, EQ, bus compressor, pan, volume, limiter."""

    def __init__(self, sr: float):
        self.sr = sr
        self.eq = EQ3(sr)
        self.comp = Compressor(sr)
        self.comp.set_params({"threshold": -12, "ratio": 2, "attack": 20, "release": 200,
                              "knee": 8, "makeup": 0})
        self.comp.enabled = False
        self.eq.enabled = True
        self.settings = {"input_gain": 0.0, "instrument_level": 0.0, "fx_return": 0.0,
                         "playback_level": 0.0, "volume": -6.0, "pan": 0.0,
                         "mute": False, "limiter": True, "inst_mute": False, "input_mute": False,
                         "fx_mute": False, "playback_mute": False}

    def update(self, data: dict):
        for k in ("input_gain", "instrument_level", "fx_return", "playback_level", "volume", "pan"):
            if k in data:
                self.settings[k] = float(data[k])
        for k in ("mute", "limiter", "inst_mute", "input_mute", "fx_mute", "playback_mute"):
            if k in data:
                self.settings[k] = bool(data[k])
        if "eq" in data:
            eq = data["eq"]
            if "enabled" in eq:
                self.eq.enabled = bool(eq["enabled"])
            self.eq.set_params(eq.get("params", {}))
        if "comp" in data:
            c = data["comp"]
            if "enabled" in c:
                self.comp.enabled = bool(c["enabled"])
            self.comp.set_params(c.get("params", {}))

    def process(self, x: np.ndarray) -> np.ndarray:
        s = self.settings
        y = self.eq.process(x)
        y = self.comp.process(y)
        if not self.comp.enabled:
            self.comp.meter_gr_db = 0.0
        pan = s["pan"]
        a = (pan + 1) * math.pi / 4
        g = 0.0 if s["mute"] else db_to_lin(s["volume"])
        y = np.vstack([y[0] * math.cos(a) * 1.414 * g, y[1] * math.sin(a) * 1.414 * g])
        if s["limiter"]:
            y = np.tanh(y * 0.95) / 0.95 * 0.98
        return y.astype(np.float32)

    def describe(self) -> dict:
        return {**self.settings, "eq": self.eq.describe(), "comp": self.comp.describe()}

    def to_preset(self) -> dict:
        return {**self.settings,
                "eq": {"enabled": self.eq.enabled, "params": dict(self.eq.params)},
                "comp": {"enabled": self.comp.enabled, "params": dict(self.comp.params)}}
