#!/usr/bin/env bash
# Vundu_MIDI - optional system setup for Linux (Ubuntu, Debian, Raspberry Pi OS) and macOS.
#
# You do NOT need this to run Vundu_MIDI: `python3 vundu_midi.py` sets up its own
# Python packages. This script adds the system pieces Python cannot install:
#
#   ./scripts/install.sh                  # audio + MIDI system libraries, audio group
#   ./scripts/install.sh --service        # also start Vundu_MIDI automatically at boot
#   ./scripts/install.sh --with-minihost  # also build Minihost from source (Raspberry Pi / ARM)
#
# Safe to run again at any time.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
WITH_MINIHOST=0
SERVICE=0
for a in "$@"; do
  case "$a" in
    --with-minihost) WITH_MINIHOST=1 ;;
    --service) SERVICE=1 ;;
    -h|--help) sed -n 2,11p "$0"; exit 0 ;;
    *) echo "Unknown option: $a (try --help)"; exit 1 ;;
  esac
done

say() { printf '\n\033[1;33m==> %s\033[0m\n' "$*"; }
OS="$(uname -s)"
USER="${USER:-$(id -un)}"

# --- 1. system libraries -------------------------------------------------------
if [ "$OS" = "Linux" ] && command -v apt-get >/dev/null; then
  say "Installing system packages (asks for your password)"
  sudo apt-get update
  sudo apt-get install -y python3 python3-venv python3-pip libportaudio2 libasound2-dev alsa-utils
  if ! id -nG "$USER" | grep -qw audio; then
    sudo usermod -aG audio "$USER"
    echo "Added $USER to the 'audio' group: log out and back in once so MIDI and audio devices open."
  fi
elif [ "$OS" = "Darwin" ]; then
  if command -v brew >/dev/null; then brew install portaudio; else echo "Optional: install Homebrew, then 'brew install portaudio'."; fi
fi

# --- 2. Python environment (the same self-setup vundu_midi.py does) -----------
say "Setting up Vundu_MIDI's Python environment"
python3 "$ROOT/vundu_midi.py" --setup-only
VPY="$ROOT/.venv/bin/python3"

# --- 3. optional: Minihost from source (no ARM Linux wheels yet) ----------------
if [ "$WITH_MINIHOST" = "1" ]; then
  say "Building Minihost from source (20-40 minutes on a Raspberry Pi 5)"
  if [ "$OS" = "Linux" ]; then
    sudo apt-get install -y build-essential cmake ninja-build pkg-config git \
         libfreetype-dev libfontconfig1-dev libcurl4-openssl-dev libx11-dev libxrandr-dev \
         libxinerama-dev libxcursor-dev libgl-dev
  fi
  mkdir -p third_party
  [ -d third_party/minihost ] || git clone --depth 1 https://github.com/shakfu/minihost third_party/minihost
  ( cd third_party/minihost
    "$VPY" scripts/download_juce.py
    "$VPY" -m pip install scikit-build-core nanobind
    CMAKE_BUILD_PARALLEL_LEVEL="$(nproc 2>/dev/null || echo 2)" "$VPY" -m pip install . )
  "$VPY" -c "import minihost; print('Minihost', minihost.__version__, 'installed')"
fi

# --- 4. optional: start at boot -------------------------------------------------
if [ "$SERVICE" = "1" ] && [ "$OS" = "Linux" ]; then
  say "Installing the systemd service 'vundu-midi'"
  sed -e "s#__DIR__#$ROOT#g" -e "s#__USER__#$USER#g" -e "s#__UID__#$(id -u)#g" \
      scripts/vundu-midi.service | sudo tee /etc/systemd/system/vundu-midi.service >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable --now vundu-midi
  echo "Running. Logs: journalctl -u vundu-midi -f"
fi

say "Done. Start Vundu_MIDI with:"
echo "  cd \"$ROOT\""
echo "  python3 vundu_midi.py"
