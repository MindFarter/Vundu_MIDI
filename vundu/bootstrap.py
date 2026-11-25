"""Self-setup for Vundu_MIDI. Standard library only.

`python3 vundu_midi.py` is the only command anyone needs. Before anything
else is imported, this module makes sure the project's own Python
environment (the `.venv` folder next to vundu_midi.py) exists, is healthy and
has the packages from requirements.txt, then restarts the program inside it.

  * First run: creates .venv and installs the requirements (a minute or two).
  * Every later run: a quick health check, then straight on.
  * If .venv is missing, broken, copied from elsewhere, or requirements.txt has
    changed, it is repaired or rebuilt automatically.

Nobody has to activate a virtual environment by hand.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
STAMP = VENV / ".vundu-requirements.sha256"
GUARD = "VUNDU_BOOTSTRAPPED"
MIN_PY = (3, 12)
# packages that must import for the server to start (sounddevice is checked
# separately because it raises OSError, not ImportError, without PortAudio)
CORE_IMPORTS = "import flask, numpy, scipy"

IS_WIN = os.name == "nt"
VENV_PY = VENV / ("Scripts/python.exe" if IS_WIN else "bin/python3")


def _say(msg: str = ""):
    print(msg, flush=True)


def _fail(lines: list[str]):
    _say("")
    _say("  Vundu_MIDI could not set itself up:")
    for line in lines:
        _say("    " + line)
    _say("")
    sys.exit(1)


def _req_hash() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def _inside_venv() -> bool:
    try:
        return Path(sys.prefix).resolve() == VENV.resolve()
    except OSError:
        return False


def _venv_python_works() -> bool:
    if not VENV_PY.exists():
        return False
    try:
        r = subprocess.run([str(VENV_PY), "-c", "import sys; print(sys.version_info[:2])"],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _venv_has_packages() -> bool:
    try:
        r = subprocess.run([str(VENV_PY), "-c", CORE_IMPORTS], capture_output=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _stamp_ok() -> bool:
    try:
        return STAMP.read_text().strip() == _req_hash()
    except OSError:
        return False


def _create_venv():
    if VENV.exists():
        _say("  Removing the old environment (.venv)...")
        shutil.rmtree(VENV, ignore_errors=True)
    _say(f"  Creating the Python environment in {VENV} ...")
    r = subprocess.run([sys.executable, "-m", "venv", str(VENV)], capture_output=True, text=True)
    if r.returncode != 0 or not VENV_PY.exists():
        shutil.rmtree(VENV, ignore_errors=True)
        detail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or ["unknown error"]
        if IS_WIN:
            fix = ["Reinstall Python from python.org and tick 'Add python.exe to PATH'."]
        elif sys.platform == "darwin":
            fix = ["Install Python 3.12+ from python.org or with: brew install python"]
        else:
            fix = ["Install the venv module, then run Vundu_MIDI again:",
                   "  sudo apt install python3-venv python3-pip",
                   "  python3 vundu_midi.py"]
        _fail([f"Python could not create a virtual environment ({detail[0]}).", *fix])


def _install_requirements():
    _say("  Installing Vundu_MIDI's packages (needs internet, about 1-2 minutes the first time)...")
    pip = [str(VENV_PY), "-m", "pip", "--disable-pip-version-check"]
    subprocess.run(pip + ["install", "--quiet", "--upgrade", "pip"], check=False)
    r = subprocess.run(pip + ["install", "-r", str(REQUIREMENTS)])
    if r.returncode != 0:
        _fail(["Installing the packages from requirements.txt failed (see the messages above).",
               "Check the internet connection and run 'python3 vundu_midi.py' again.",
               "To start completely fresh: python3 vundu_midi.py --reinstall"])
    STAMP.write_text(_req_hash())


def ensure_environment(argv: list[str]) -> None:
    """Make sure we run inside a healthy .venv. Restarts the process if needed."""
    if os.environ.get("VUNDU_NO_VENV"):
        return  # escape hatch: use whatever Python is running (advanced users, packagers)
    if _inside_venv():
        if not _stamp_ok():
            # requirements.txt changed since the last install: top up in place
            _install_requirements()
        return

    if sys.version_info < MIN_PY:
        _fail([f"Vundu_MIDI needs Python {MIN_PY[0]}.{MIN_PY[1]} or newer; this is "
               f"{sys.version.split()[0]} ({sys.executable})."])

    if os.environ.get(GUARD):
        # we already restarted once and still are not in the venv: avoid a loop
        _fail(["The project environment could not be started.",
               "Run: python3 vundu_midi.py --reinstall"])

    reinstall = "--reinstall" in argv
    healthy = (not reinstall) and _venv_python_works() and _venv_has_packages() and _stamp_ok()
    if not healthy:
        _say("")
        _say("  Vundu_MIDI first-time setup" if not VENV.exists() else "  Vundu_MIDI: repairing its Python environment")
        _say("  " + "-" * 52)
        if reinstall or not _venv_python_works():
            _create_venv()
        _install_requirements()
        if not _venv_has_packages():
            _fail(["The packages installed but still cannot be imported.",
                   "Run: python3 vundu_midi.py --reinstall"])
        _say("  Setup complete.")
        _say("")

    args = [a for a in argv if a != "--reinstall"]
    env = dict(os.environ, **{GUARD: "1"})
    cmd = [str(VENV_PY), str(ROOT / "vundu_midi.py"), *args]
    if IS_WIN:
        sys.exit(subprocess.call(cmd, env=env))
    os.execve(str(VENV_PY), cmd, env)
