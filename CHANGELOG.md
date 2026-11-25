# Changelog

All notable changes to Vundu_MIDI are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Planned
- ESP32 MIDI interface firmware that talks to the server (UDP MIDI + `/api/v1/remote/hello`)
- Native plugin editor windows
- Multi-track recording and playback of takes
- MIDI clock / tempo sync for delays and tremolo

## [0.0.2] - 2026-11-25

### Added
- **Tape deck** under the VU meters: Record, Pause, Stop, Play, Rewind and Fast forward
  (click for a 5-second jump, hold to keep winding), a clickable position bar, and a take selector.
  Its display shows the state, a counter, the take length, spinning reels and the take's details.
- **Your name, Audio name and Date** fields on the deck. They name the file
  (`date_name_audio-name.wav`) and are written into the WAV as standard tags
  (artist, title, date) that music players and editors show.
- Recording can be paused and resumed within one take.
- Takes play back through the console output (speakers, line out or amp), following the Main fader.
- **Mini mixer** between the dB meter and the bus compressor: Keys, Guitar, Effects (return level;
  Mute bypasses every effect), Playback and Main, each with a level meter, fader and mute.
  The channel not used by the current instrument is dimmed.
- Recordings panel shows the name, audio name and date of each take and can play it on the deck.
- New API: `/api/v1/deck`, `/deck/meta`, `/deck/record`, `/deck/pause`, `/deck/stop`, `/deck/play`,
  `/deck/load`, `/deck/seek`, `/deck/rewind`, `/deck/forward`; mixer fields on `/master`
  (`fx_return`, `playback_level`, `inst_mute`, `input_mute`, `fx_mute`, `playback_mute`);
  per-channel levels in `/meters` (`ch`).
- README: a step-by-step "How to use" guide covering a whole session, from starting the server to
  recording, playback, presets, amp output and MIDI learn.

### Changed
- Up to **20 effects** in the chain and in presets (was 8). The desk shows how many slots are used.
- Bus compressor and Master EQ moved to the right of the meter bridge with their knobs in
  vertical columns.
- Record and Stop moved from the top transport bar to the tape deck.
- `/api/v1/record/start` and `/record/stop` still work and now use the deck (the `name` field
  becomes the audio name).
- README, API reference (`docs/API.md`) and ESP32 notes updated for the tape deck, mixer and
  20-effect chain.

### Removed
- The `max_slots` setting (replaced by the fixed limit of 20); it is dropped from old config files.

## [0.0.1] - 2026-11-25

First release.

### Added
- `python3 vundu_midi.py` is the only command needed. It sets up its own Python environment
  (`.venv`) on the first run, checks it on every start, repairs it if it is missing or damaged,
  and updates it when `requirements.txt` changes. `--reinstall` rebuilds it from scratch.
- The server (Flask) listens on port 5080, reachable from the whole LAN.
- Studio console web GUI: menu bar, glass status screen with waveform, oscilloscope and
  spectrum analyser, back-lit analogue VU meters, LED peak meters with peak hold, clip light,
  bus compressor with gain-reduction meter, master EQ, light-up knobs and faders, colour-capped
  channel strips for every effect in the chain.
- Instruments: Keyboard (MIDI) and Guitar (audio input through the jack, or MIDI guitar).
- USB MIDI input (5-pin DIN to USB cables and USB MIDI keyboards) with hot-plug auto-connect,
  via Minihost (libremidi) or mido/python-rtmidi as fallback.
- Built-in sound generator with 10 sounds: grand piano, electric piano, tonewheel organ,
  strings, warm pad, analog lead, synth bass, clean electric guitar, nylon guitar, bells.
- 11 built-in effects: noise gate, compressor, overdrive, distortion, amp and cab, 3-band EQ,
  chorus, tremolo, tape delay, plate reverb, gain/pan.
- VST3 / LV2 / CLAP (and AudioUnit on macOS) plugin hosting through Minihost: plugin scanning,
  effect plugins in the chain, instrument plugins as sounds, full parameter editor, programs,
  plugin state saved in presets.
- 12 factory presets for keyboard and guitar; save and delete user presets.
- MIDI Program Change selects presets; sustain pedal and pitch bend; MIDI learn for any knob or fader.
- Recording to 24-bit stereo WAV, with playback, download and delete in the Recordings panel.
- Audio device, sample rate, buffer size and guitar input channel selection.
- On-screen piano and computer-keyboard playing for testing without hardware.
- REST API v1 (`/api/v1`) covering every console function, Server-Sent Events meter stream.
- Network MIDI for the future ESP32 interface: raw MIDI over UDP (port 5005) and JSON over HTTP.
- Optional `scripts/install.sh` for the system audio/MIDI libraries, a start-at-boot systemd
  service, and building Minihost on ARM (Raspberry Pi OS Trixie).
- README, API reference, ESP32 integration notes, tests and GitHub Actions CI.
