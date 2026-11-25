"""Tape deck: recording and playback of takes.

Recorder - writes 24-bit stereo WAV from the audio thread (via a queue),
           can pause and resume, and tags the file with the player's name,
           the audio name and the date (RIFF LIST/INFO chunk: IART, INAM, ICRD),
           which most audio players and editors display.
Player   - loads a take into memory and plays it through the console output,
           with pause, stop, rewind, fast forward and seek.
"""

from __future__ import annotations

import queue
import re
import struct
import threading
import wave
from datetime import date
from pathlib import Path

import numpy as np

INFO_FIELDS = {"artist": b"IART", "title": b"INAM", "date": b"ICRD", "software": b"ISFT"}


def _safe(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9 _\-]+", "", text or "").strip().replace(" ", "_")
    return s[:limit]


# --------------------------------------------------------------------------- #
# WAV metadata (RIFF LIST/INFO)
# --------------------------------------------------------------------------- #


def write_info(path: Path, meta: dict):
    """Append a LIST/INFO chunk to a finished WAV file and fix the RIFF size."""
    sub = b""
    for key, cid in INFO_FIELDS.items():
        val = (meta.get(key) or "").strip()
        if not val:
            continue
        data = val.encode("utf-8")[:250] + b"\x00"
        if len(data) % 2:
            data += b"\x00"
        sub += cid + struct.pack("<I", len(data)) + data
    if not sub:
        return
    chunk = b"LIST" + struct.pack("<I", 4 + len(sub)) + b"INFO" + sub
    with open(path, "r+b") as f:
        f.seek(0, 2)
        if f.tell() % 2:
            f.write(b"\x00")
        f.write(chunk)
        size = f.tell()
        f.seek(4)
        f.write(struct.pack("<I", size - 8))


def read_info(path: Path) -> dict:
    """Read LIST/INFO tags and basic format info from a WAV file."""
    out = {"artist": "", "title": "", "date": "", "seconds": 0.0, "sample_rate": 0, "channels": 0}
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return out
            fmt = None
            data_len = 0
            while True:
                h = f.read(8)
                if len(h) < 8:
                    break
                cid, size = h[:4], struct.unpack("<I", h[4:])[0]
                if cid == b"fmt ":
                    fmt = f.read(size)
                elif cid == b"data":
                    data_len = size
                    f.seek(size, 1)
                elif cid == b"LIST":
                    body = f.read(size)
                    if body[:4] == b"INFO":
                        i = 4
                        while i + 8 <= len(body):
                            sid, ssz = body[i:i + 4], struct.unpack("<I", body[i + 4:i + 8])[0]
                            val = body[i + 8:i + 8 + ssz].split(b"\x00")[0].decode("utf-8", "replace")
                            for key, code in INFO_FIELDS.items():
                                if sid == code:
                                    out[key] = val
                            i += 8 + ssz + (ssz % 2)
                else:
                    f.seek(size, 1)
                if size % 2:
                    f.seek(1, 1)
            if fmt:
                ch, sr = struct.unpack("<HI", fmt[2:8])
                bits = struct.unpack("<H", fmt[14:16])[0]
                out.update(channels=ch, sample_rate=sr)
                if sr and ch and bits:
                    out["seconds"] = round(data_len / (sr * ch * bits // 8), 2)
    except OSError:
        pass
    return out


def load_wav(path: Path, target_sr: int) -> np.ndarray:
    """Load a 16/24/32-bit PCM WAV as planar float32 stereo at target_sr."""
    with wave.open(str(path), "rb") as w:
        ch, width, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3)
        i = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
        i = np.where(i & 0x800000, i - 0x1000000, i)
        x = i.astype(np.float32) / 8388608.0
    elif width == 4:
        x = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        x = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128) / 128.0
    else:
        raise ValueError(f"unsupported sample width {width}")
    x = x.reshape(-1, ch).T
    if ch == 1:
        x = np.vstack([x[0], x[0]])
    else:
        x = x[:2]
    if sr != target_sr and x.shape[1] > 1:
        m = int(round(x.shape[1] * target_sr / sr))
        t_old = np.arange(x.shape[1]) / sr
        t_new = np.arange(m) / target_sr
        x = np.vstack([np.interp(t_new, t_old, x[c]) for c in range(2)])
    return np.ascontiguousarray(x, dtype=np.float32)


# --------------------------------------------------------------------------- #
# Recorder
# --------------------------------------------------------------------------- #


class Recorder:
    def __init__(self, folder: Path):
        self.folder = folder
        self.q: queue.Queue = queue.Queue()
        self.active = False
        self.paused = False
        self.path: Path | None = None
        self.meta: dict = {}
        self.frames = 0
        self.sr = 48000
        self._thread: threading.Thread | None = None

    def _new_path(self, meta: dict) -> Path:
        parts = [meta.get("date") or date.today().isoformat(), _safe(meta.get("artist", "")),
                 _safe(meta.get("title", "")) or "take"]
        stem = "_".join(p for p in parts if p)
        path = self.folder / f"{stem}.wav"
        k = 2
        while path.exists():
            path = self.folder / f"{stem}_{k}.wav"
            k += 1
        return path

    def start(self, sr: int, meta: dict | None = None) -> Path:
        if self.active:
            return self.path
        self.folder.mkdir(parents=True, exist_ok=True)
        self.meta = {k: (meta or {}).get(k, "") for k in ("artist", "title", "date")}
        self.meta["date"] = self.meta["date"] or date.today().isoformat()
        self.meta["software"] = "Vundu_MIDI"
        self.path = self._new_path(self.meta)
        self.sr = int(sr)
        self.frames = 0
        self.paused = False
        self.q = queue.Queue()
        self.active = True
        self._thread = threading.Thread(target=self._writer, args=(self.path, self.q, self.sr), daemon=True)
        self._thread.start()
        return self.path

    def pause(self, on: bool | None = None):
        if self.active:
            self.paused = (not self.paused) if on is None else bool(on)

    def push(self, block: np.ndarray):
        if self.active and not self.paused:
            self.q.put(block.copy())
            self.frames += block.shape[1]

    def stop(self) -> dict | None:
        if not self.active:
            return None
        self.active = False
        self.paused = False
        self.q.put(None)
        if self._thread:
            self._thread.join(timeout=10)
        try:
            write_info(self.path, self.meta)
        except OSError:
            pass
        return {"file": self.path.name, "seconds": round(self.frames / self.sr, 2), **self.meta}

    @staticmethod
    def _writer(path: Path, q: queue.Queue, sr: int):
        with wave.open(str(path), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(3)
            w.setframerate(sr)
            while True:
                blk = q.get()
                if blk is None:
                    break
                ints = np.ascontiguousarray((np.clip(blk.T, -1.0, 1.0) * 8388607.0).astype("<i4"))
                w.writeframes(ints.view(np.uint8).reshape(-1, 4)[:, :3].tobytes())

    @property
    def seconds(self) -> float:
        return self.frames / self.sr if self.sr else 0.0


# --------------------------------------------------------------------------- #
# Player
# --------------------------------------------------------------------------- #


class Player:
    def __init__(self, sr: int):
        self.sr = sr
        self.data: np.ndarray | None = None
        self.file: str | None = None
        self.info: dict = {}
        self.pos = 0
        self.state = "stopped"  # stopped | playing | paused

    def load(self, path: Path):
        data = load_wav(path, self.sr)
        self.state = "stopped"
        self.data, self.file, self.pos = data, path.name, 0
        self.info = read_info(path)

    def unload(self):
        self.state, self.data, self.file, self.pos, self.info = "stopped", None, None, 0, {}

    @property
    def length(self) -> int:
        return 0 if self.data is None else self.data.shape[1]

    def play(self):
        if self.data is None:
            raise RuntimeError("no take loaded")
        if self.pos >= self.length:
            self.pos = 0
        self.state = "playing"

    def pause(self):
        if self.state == "playing":
            self.state = "paused"
        elif self.state == "paused":
            self.state = "playing"

    def stop(self):
        self.state = "stopped"
        self.pos = 0

    def seek(self, seconds: float | None = None, delta: float | None = None):
        if self.data is None:
            return
        target = self.pos / self.sr if seconds is None else float(seconds)
        if delta is not None:
            target += float(delta)
        self.pos = int(min(max(target, 0.0), self.length / self.sr) * self.sr)

    def render(self, n: int) -> np.ndarray | None:
        if self.state != "playing" or self.data is None:
            return None
        start = self.pos
        end = min(start + n, self.length)
        out = np.zeros((2, n), dtype=np.float32)
        out[:, : end - start] = self.data[:, start:end]
        self.pos = end
        if end >= self.length:
            self.state = "stopped"
            self.pos = self.length
        return out
