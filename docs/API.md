# Vundu_MIDI API v1

Base URL: `http://<host>:5080/api/v1`

- Requests and responses are JSON. Send `Content-Type: application/json` with bodies.
- Every response has `"ok": true`, or `"ok": false` with an `"error"` message and an HTTP status
  (400 bad request, 403 not allowed, 404 not found, 409 not available, e.g. Minihost missing).
- `GET /api/v1` lists every endpoint of the running server.
- There is no authentication yet: keep the server on a trusted local network.

## Status

| Method | Path | Description |
|---|---|---|
| GET | `/status` | Short summary: audio, MIDI, UDP MIDI, instrument, sound, preset, chain, recording, remotes, `rev` |
| GET | `/system` | Version, platform, LAN IP, uptime, Minihost availability |
| GET | `/state` | Everything the console needs: chain with all parameters, master, MIDI map, learn target |

`rev` increases whenever the console state changes (from the GUI, the API or MIDI). Clients can poll
`/meters` or the stream and re-read `/state` when `rev` changes.

## Devices

| Method | Path | Body | Description |
|---|---|---|---|
| GET | `/devices/audio` | | Audio inputs/outputs and the current engine |
| POST | `/devices/audio` | `audio_output`, `audio_input`, `sample_rate`, `block_size`, `input_channel`, `audio_input_enabled` | Change audio settings and restart audio |
| GET | `/devices/midi` | | MIDI input ports and connection |
| POST | `/devices/midi/connect` | `{"port": 1}` or `{"port": "USB MIDI"}` | Connect a MIDI port by index or (part of) name |
| POST | `/devices/midi/disconnect` | | Disconnect |
| POST | `/devices/midi/auto` | `{"enabled": true}` | Hot-plug auto-connect on/off |

## Instruments and sounds

| Method | Path | Body | Description |
|---|---|---|---|
| GET | `/instruments` | | `keyboard`, `guitar` and the current choice |
| POST | `/instrument` | `instrument`, `guitar_source` (`audio`/`midi`), `sound` | Any combination |
| GET | `/sounds` | | Built-in sounds (`builtin:<id>`) and instrument plugins (`plugin:<id>`) |

## Effects chain

| Method | Path | Body | Description |
|---|---|---|---|
| GET | `/effects` | | Catalogue: built-in effects with parameter specs, and scanned effect plugins |
| GET | `/chain` | | Current chain |
| POST | `/chain` | `{"type": "reverb", "params": {...}, "position": 2}` or `{"type": "plugin", "plugin": "<id or path>"}` | Add an effect (maximum 20; a 21st returns 400) |
| GET | `/chain/<slot_id>` | | One slot with every parameter (plugins: all params and programs) |
| PATCH | `/chain/<slot_id>` | `{"enabled": false}`, `{"params": {"mix": 0.4}}`, `{"program": 3}` | Change a slot |
| DELETE | `/chain/<slot_id>` | | Remove |
| POST | `/chain/<slot_id>/move` | `{"position": 0}` | Reorder |
| POST | `/chain/clear` | | Remove all effects |

Built-in parameter values are in real units (dB, ms, Hz). Plugin parameters are normalised 0..1 and
use the parameter index as `id`.

Built-in effect types: `gate`, `compressor`, `overdrive`, `distortion`, `amp`, `eq3`, `chorus`,
`tremolo`, `delay`, `reverb`, `utility`.

## Master bus

| Method | Path | Body | Description |
|---|---|---|---|
| GET | `/master` | | Master settings, EQ and compressor |
| PATCH (or POST) | `/master` | `volume` (dB), `pan` (-1..1), `input_gain` (dB), `instrument_level` (dB), `fx_return` (dB), `playback_level` (dB), `mute`, `inst_mute`, `input_mute`, `fx_mute` (bypasses all effects), `playback_mute`, `limiter`, `comp: {enabled, params}`, `eq: {enabled, params}` | Change any of them (the mixer uses these) |
| POST | `/control` | `{"target": "chain.0.mix", "value": 0.5}` | Set any mappable control from a 0..1 value |

Control targets: `master.volume`, `master.pan`, `master.input_gain`, `master.instrument_level`,
`master.fx_return`, `master.playback_level`,
`master.comp.<param>`, `master.eq.<param>`, `chain.<index>.<param>`, `chain.<index>.enabled`.

## Meters and live stream

| Method | Path | Description |
|---|---|---|
| GET | `/meters` | Peak/RMS dBFS, gain reduction, clip, DSP load, dropouts, active notes, recording, mixer channel peaks (`ch`: inst, input, fx, play, master), `deck`, `rev` |
| POST | `/meters/reset_clip` | Clear the clip light |
| GET | `/waveform?points=360` | Scrolling overview (`[min, max, recording]`) and oscilloscope samples |
| GET | `/spectrum?bands=48` | Spectrum in dBFS per log-spaced band (30 Hz - 18 kHz) |
| GET | `/stream?fps=20` | Server-Sent Events: `{"meters", "wave", "spectrum"}` per frame |

## Presets

| Method | Path | Body | Description |
|---|---|---|---|
| GET | `/presets` | | All presets (factory first, then user) and the current one |
| GET | `/presets/<id>` | | Full preset JSON |
| POST | `/presets/load` | `{"id": "factory:metal_lead"}` | Load |
| POST | `/presets` | `{"name": "My sound", "category": "Guitar", "description": ""}` | Save current settings as a user preset |
| DELETE | `/presets/<id>` | | Delete a user preset (factory presets are protected) |
| GET | `/snapshot` | | Current settings in preset format |

## Tape deck and recordings

| Method | Path | Body | Description |
|---|---|---|---|
| GET | `/deck` | | `state` (`empty`, `stopped`, `playing`, `paused`, `recording`, `rec_paused`), `position` and `length` in seconds, `file`, `take` tags, `meta` (the deck's name fields) |
| POST (or PATCH) | `/deck/meta` | `{"artist": "Bertie", "title": "Blues in E", "date": "2026-11-25"}` | Set Your name / Audio name / Date for the next recording |
| POST | `/deck/record` | optional meta as above | Start recording, or resume a paused recording |
| POST | `/deck/pause` | | Pause / resume recording or playback |
| POST | `/deck/stop` | | Stop: finishes a recording (returns its file, length and tags, and loads it) or stops playback and returns to the start |
| POST | `/deck/play` | optional `{"file": "<name>.wav"}` | Play the loaded take, or load and play the given one |
| POST | `/deck/load` | `{"file": "<name>.wav"}` | Load a take without playing |
| POST | `/deck/rewind` | optional `{"seconds": 5}` | Wind back |
| POST | `/deck/forward` | optional `{"seconds": 5}` | Wind forward |
| POST | `/deck/seek` | `{"position": 12.5}` or `{"delta": -2}` | Jump to a time or move relative to the current one |
| POST | `/record/start` | optional `{"name": "take one"}` | Older shortcut kept from v0.0.1, same as `/deck/record` (`name` = audio name) |
| POST | `/record/stop` | | Older shortcut kept from v0.0.1, same as `/deck/stop` while recording |
| GET | `/recordings` | | WAV files with `artist`, `title`, `date`, `seconds`, and the `loaded` take |
| GET | `/recordings/<file>` | | Stream the file (`?download=1` to download) |
| DELETE | `/recordings/<file>` | | Delete (not while it is being recorded) |

Winding and playing are refused with 409 while recording.

## MIDI

| Method | Path | Body | Description |
|---|---|---|---|
| POST | `/midi/send` | see below | Inject MIDI as if it came from the cable |
| GET | `/midi/log` | | Last 64 messages from all sources |
| POST | `/panic` | | All notes off, effect tails cleared |
| GET | `/midi/map` | | CC mappings (`"channel:cc": "target"`) |
| POST | `/midi/map` | `{"channel": 1, "cc": 21, "target": "chain.0.drive"}` | Add a mapping |
| DELETE | `/midi/map/<channel:cc>` | | Remove a mapping |
| POST | `/midi/learn` | `{"target": "master.volume"}` (or `null` to cancel) | The next CC received is mapped to the target |

`/midi/send` accepts one message or `{"messages": [...]}`. Each message is either raw:

```json
{"status": 144, "data1": 60, "data2": 100}
```

or named (channel 1-16, default 1):

```json
{"type": "note_on", "note": 60, "velocity": 100, "channel": 1}
{"type": "note_off", "note": 60}
{"type": "cc", "control": 7, "value": 100}
{"type": "program_change", "program": 4}
{"type": "pitch_bend", "value": 2048}
```

## Plugins (Minihost)

| Method | Path | Body | Description |
|---|---|---|---|
| GET | `/plugins` | | Whether Minihost is available, scanned plugins, search folders, scan progress |
| POST | `/plugins/scan` | `{"refresh": false}` | Start a background scan |
| GET | `/plugins/scan` | | Scan progress |
| POST | `/plugins/dirs` | `{"dirs": ["/home/pi/my-plugins"]}` | Extra folders to scan |

## Remote controllers (ESP32)

| Method | Path | Body | Description |
|---|---|---|---|
| POST | `/remote/hello` | `{"id": "esp32-a1b2", "name": "Stage MIDI", "board": "ESP32-S3", "firmware": "0.1.0"}` | Register / heartbeat; returns server version, UDP MIDI port, sample rate, current preset |
| GET | `/remote` | | Registered controllers |

## Settings

| Method | Path | Body | Description |
|---|---|---|---|
| GET | `/settings` | | Contents of `data/config.json` |
| PATCH | `/settings` | `startup_preset`, `theme`, `midi_auto_connect` | Change and save |

## UDP MIDI

Send raw MIDI bytes to UDP port 5005 (configurable with `--udp-port`). Several messages may share a
datagram and running status is understood. SysEx and real-time bytes (clock, start, stop) are ignored for now.

```bash
# note on C4 velocity 100, from a shell
printf '\x90\x3c\x64' | nc -u -w0 <server-ip> 5005
```
