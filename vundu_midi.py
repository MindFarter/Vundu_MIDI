#!/usr/bin/env python3
"""Vundu_MIDI - start the server. This is the only command you need:

    python3 vundu_midi.py

The first run sets everything up by itself (a private Python environment in
.venv with all packages); later runs start straight away. Other options:

    python3 vundu_midi.py --port 8080     # another port
    python3 vundu_midi.py --list-devices  # show audio + MIDI devices and exit
    python3 vundu_midi.py --reinstall     # rebuild the Python environment from scratch

Vundu_MIDI is open source software based on Minihost
(https://github.com/shakfu/minihost). All attribution for Minihost
belongs to its developers.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 1. Before anything else: make sure we run inside a healthy project
#    environment (creates, repairs or updates .venv, then restarts in it).
from vundu.bootstrap import ensure_environment  # noqa: E402  (standard library only)

if __name__ == "__main__":
    ensure_environment(sys.argv[1:])

# 2. From here on all packages from requirements.txt are available.
import argparse  # noqa: E402
import logging  # noqa: E402
import signal  # noqa: E402

from vundu import APP_NAME, __version__  # noqa: E402

BANNER = """
  +--------------------------------------------------+
  |   V U N D U _ M I D I     v{ver:<22}|
  |   studio console for MIDI keyboards and guitars  |
  |   powered by Minihost: github.com/shakfu/minihost|
  +--------------------------------------------------+
"""


def parse_args():
    p = argparse.ArgumentParser(prog="vundu_midi.py", description=f"{APP_NAME} {__version__} server")
    p.add_argument("--host", help="interface to listen on (default 0.0.0.0 = whole LAN)")
    p.add_argument("--port", type=int, help="web / API port (default 5080)")
    p.add_argument("--udp-port", type=int, dest="udp_midi_port", help="UDP MIDI port for ESP32 (default 5005, 0=off)")
    p.add_argument("--data", default=os.environ.get("VUNDU_DATA", str(HERE / "data")),
                   help="folder for settings, user presets and recordings (default ./data)")
    p.add_argument("--sample-rate", type=int, dest="sample_rate")
    p.add_argument("--block-size", type=int, dest="block_size", help="audio buffer size (latency)")
    p.add_argument("--output", dest="audio_output", help="audio output device index or name")
    p.add_argument("--input", dest="audio_input", help="audio input device index or name")
    p.add_argument("--no-audio", action="store_true", help="run without opening an audio device")
    p.add_argument("--list-devices", action="store_true", help="list audio and MIDI devices and exit")
    p.add_argument("--debug", action="store_true", help="verbose logging")
    p.add_argument("--reinstall", action="store_true", help="rebuild the Python environment (.venv) from scratch")
    p.add_argument("--setup-only", action="store_true", help="set up the Python environment, then exit")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return p.parse_args()


def _device_arg(v):
    if v is None:
        return None
    return int(v) if str(v).isdigit() else v


def list_devices():
    from vundu.engine import sd, SD_ERROR
    from vundu.midi_input import MidiManager
    print(f"{APP_NAME} {__version__} - devices\n")
    if sd is None:
        print(f"  Audio: PortAudio not available ({SD_ERROR})")
    else:
        print(sd.query_devices())
    m = MidiManager(lambda *a: None, auto_connect=False)
    print(f"\nMIDI inputs (backend: {m.backend}):")
    ports = m.list_ports()
    for port in ports:
        print(f"  [{port['index']}] {port['name']}")
    if not ports:
        print("  (none found - plug in your MIDI to USB cable)")


def main():
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    if args.setup_only:
        print(f"{APP_NAME} {__version__} is set up. Start it with: python3 vundu_midi.py")
        return
    if args.list_devices:
        list_devices()
        return

    from vundu.server import create_app, lan_ip

    overrides = {"host": args.host, "port": args.port, "udp_midi_port": args.udp_midi_port,
                 "sample_rate": args.sample_rate, "block_size": args.block_size,
                 "audio_output": _device_arg(args.audio_output), "audio_input": _device_arg(args.audio_input)}
    data_dir = Path(args.data).expanduser().resolve()
    app, vapp = create_app(data_dir, overrides, start=False)
    if args.no_audio:
        vapp.config["no_audio"] = True
    vapp.start()

    host = vapp.config.get("host", "0.0.0.0")
    port = int(vapp.config.get("port", 5080))
    print(BANNER.format(ver=__version__))
    print(f"  Console  : http://localhost:{port}   (LAN: http://{lan_ip()}:{port})")
    print(f"  API      : http://localhost:{port}/api/v1")
    eng = vapp.engine
    if eng.backend == "null":
        print(f"  Audio    : OFF - {eng.backend_error or 'no audio device'}")
        if "PortAudio" in (eng.backend_error or "") and sys.platform.startswith("linux"):
            print("             fix: sudo apt install libportaudio2   then restart Vundu_MIDI")
    else:
        print(f"  Audio    : {eng.backend} @ {eng.sr} Hz, block {eng.block}")
    print(f"  MIDI     : backend {vapp.midi.backend}, auto-connect {'on' if vapp.midi.auto_connect else 'off'}")
    print(f"  UDP MIDI : port {vapp.net_midi.port if vapp.net_midi.running else 'off'}")
    print(f"  Plugins  : Minihost {'ready' if vapp.plugins.available else 'not installed (built-in effects only)'}")
    print(f"  Data     : {data_dir}\n  Ctrl+C to stop.\n")

    def _stop(*_):
        print("\nStopping Vundu_MIDI...")
        vapp.shutdown()
        os._exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    # Flask's threaded server streams the live meters (SSE) without buffering,
    # which is what a single-user local console needs.
    app.run(host=host, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
