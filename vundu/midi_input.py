"""MIDI input: USB MIDI interfaces (e.g. a 5-pin DIN to USB cable).

Backends, in order of preference:
  1. Minihost's MidiIn (libremidi, cross-platform)
  2. mido + python-rtmidi (fallback)

Ports are re-checked every two seconds so a cable can be plugged in or
pulled out while the server runs (hot-plug).

Network MIDI (for the planned ESP32 MIDI interface) is handled by
`NetworkMidi`: raw MIDI bytes in UDP datagrams.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections import deque
from typing import Callable

log = logging.getLogger("vundu.midi")

try:
    import minihost  # type: ignore
    _HAS_MH = hasattr(minihost, "MidiIn")
except Exception:
    minihost = None
    _HAS_MH = False

try:
    import mido  # type: ignore
    _HAS_MIDO = True
except Exception:
    mido = None
    _HAS_MIDO = False

SKIP_AUTO = ("through", "midi through", "rtmidi", "vundu")

MidiHandler = Callable[[int, int, int, str], None]


def describe_message(status: int, d1: int, d2: int) -> str:
    kind = status & 0xF0
    ch = (status & 0x0F) + 1
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    note = f"{names[d1 % 12]}{d1 // 12 - 1}"
    if kind == 0x90 and d2 > 0:
        return f"Note On  {note} vel {d2} ch{ch}"
    if kind in (0x80, 0x90):
        return f"Note Off {note} ch{ch}"
    if kind == 0xB0:
        return f"CC {d1} = {d2} ch{ch}"
    if kind == 0xC0:
        return f"Program {d1 + 1} ch{ch}"
    if kind == 0xE0:
        return f"Pitch Bend {(d2 << 7 | d1) - 8192} ch{ch}"
    if kind == 0xD0:
        return f"Aftertouch {d1} ch{ch}"
    if kind == 0xA0:
        return f"Poly AT {note} {d2} ch{ch}"
    return f"0x{status:02X} {d1} {d2}"


class MidiParser:
    """Turns a byte stream into (status, d1, d2) messages. Handles running
    status and ignores SysEx / realtime bytes."""

    def __init__(self):
        self.status = 0
        self.data: list[int] = []
        self.in_sysex = False

    def feed(self, data: bytes):
        out = []
        for b in data:
            if b >= 0xF8:  # realtime: clock, start, stop... ignored for now
                continue
            if b == 0xF0:
                self.in_sysex = True
                continue
            if b == 0xF7:
                self.in_sysex = False
                continue
            if self.in_sysex:
                continue
            if b & 0x80:
                self.status = b
                self.data = []
                continue
            if not self.status:
                continue
            self.data.append(b)
            need = 1 if (self.status & 0xF0) in (0xC0, 0xD0) else 2
            if len(self.data) >= need:
                d1 = self.data[0]
                d2 = self.data[1] if need == 2 else 0
                out.append((self.status, d1, d2))
                self.data = []
        return out


class MidiManager:
    def __init__(self, handler: MidiHandler, auto_connect: bool = True, preferred: str | None = None):
        self.handler = handler
        self.auto_connect = auto_connect
        self.preferred = preferred
        self.backend = "minihost" if _HAS_MH else ("mido" if _HAS_MIDO else "none")
        self.port_name: str | None = None
        self._port = None
        self._parser = MidiParser()
        self.log: deque = deque(maxlen=64)
        self.activity = 0.0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._watch, daemon=True, name="midi-watch")

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.disconnect()

    # ------------------------------------------------------------------
    def list_ports(self) -> list[dict]:
        try:
            if self.backend == "minihost":
                return [{"index": p.get("index", i), "name": p.get("name", f"Port {i}")}
                        for i, p in enumerate(minihost.midi_get_input_ports())]
            if self.backend == "mido":
                return [{"index": i, "name": n} for i, n in enumerate(mido.get_input_names())]
        except Exception as exc:
            log.debug("port listing failed: %s", exc)
        return []

    def connect(self, port: int | str) -> str:
        ports = self.list_ports()
        match = None
        for p in ports:
            if (isinstance(port, int) and p["index"] == port) or (isinstance(port, str) and (
                    p["name"] == port or str(p["index"]) == port or port.lower() in p["name"].lower())):
                match = p
                break
        if not match:
            raise ValueError(f"MIDI port not found: {port}")
        with self._lock:
            self._close()
            if self.backend == "minihost":
                self._port = minihost.MidiIn.open(match["index"], self._on_bytes)
            elif self.backend == "mido":
                self._port = mido.open_input(match["name"], callback=self._on_mido)
            else:
                raise RuntimeError("No MIDI backend: pip install minihost (or mido python-rtmidi)")
            self.port_name = match["name"]
            self.preferred = match["name"]
        log.info("MIDI connected: %s", match["name"])
        return match["name"]

    def disconnect(self):
        with self._lock:
            self._close()
            self.preferred = None if not self.auto_connect else self.preferred

    def _close(self):
        if self._port is not None:
            try:
                self._port.close()
            except Exception:
                pass
        self._port = None
        self.port_name = None

    # ------------------------------------------------------------------
    def _on_bytes(self, data):
        for status, d1, d2 in self._parser.feed(bytes(data)):
            self.dispatch(status, d1, d2, "usb")

    def _on_mido(self, msg):
        b = msg.bytes()
        if b:
            self._on_bytes(bytes(b))

    def dispatch(self, status: int, d1: int, d2: int, source: str):
        self.activity = time.time()
        self.log.appendleft({"t": round(time.time(), 3), "source": source, "status": status,
                             "data1": d1, "data2": d2, "text": describe_message(status, d1, d2)})
        try:
            self.handler(status, d1, d2, source)
        except Exception:
            log.exception("MIDI handler failed")

    def _watch(self):
        while not self._stop.wait(2.0):
            try:
                names = [p["name"] for p in self.list_ports()]
                if self.port_name and self.port_name not in names:
                    log.info("MIDI device unplugged: %s", self.port_name)
                    with self._lock:
                        self._close()
                if self.port_name is None and self.auto_connect and names:
                    target = None
                    if self.preferred and self.preferred in names:
                        target = self.preferred
                    else:
                        target = next((n for n in names if not any(s in n.lower() for s in SKIP_AUTO)), None)
                    if target:
                        try:
                            self.connect(target)
                        except Exception as exc:
                            log.debug("auto-connect failed: %s", exc)
            except Exception:
                log.exception("MIDI watcher error")

    def status(self) -> dict:
        return {"backend": self.backend, "connected": self.port_name is not None, "port": self.port_name,
                "auto_connect": self.auto_connect, "last_activity": self.activity}


class NetworkMidi:
    """UDP listener: each datagram carries raw MIDI bytes, e.g. 90 3C 64.
    This is the low-latency path for the future ESP32 MIDI interface."""

    def __init__(self, manager: MidiManager, port: int = 5005, host: str = "0.0.0.0"):
        self.manager = manager
        self.port = port
        self.host = host
        self.running = False
        self.packets = 0
        self.peers: dict[str, float] = {}
        self._parsers: dict[str, MidiParser] = {}
        self._sock = None

    def start(self):
        if not self.port:
            return
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.host, self.port))
        except OSError as exc:
            log.warning("UDP MIDI disabled (port %s): %s", self.port, exc)
            return
        self.running = True
        threading.Thread(target=self._loop, daemon=True, name="udp-midi").start()
        log.info("UDP MIDI listening on %s:%s", self.host, self.port)

    def _loop(self):
        while self.running:
            try:
                data, addr = self._sock.recvfrom(512)
            except OSError:
                break
            ip = addr[0]
            self.packets += 1
            self.peers[ip] = time.time()
            parser = self._parsers.setdefault(ip, MidiParser())
            for status, d1, d2 in parser.feed(data):
                self.manager.dispatch(status, d1, d2, f"udp:{ip}")

    def stop(self):
        self.running = False
        if self._sock:
            self._sock.close()

    def status(self) -> dict:
        return {"enabled": self.running, "port": self.port, "packets": self.packets,
                "peers": {k: round(v, 1) for k, v in self.peers.items()}}
