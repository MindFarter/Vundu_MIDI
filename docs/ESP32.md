# ESP32 MIDI interface - integration notes

Goal for a later release: an ESP32 board with a 5-pin DIN MIDI IN socket (opto-isolated, 31250 baud)
that forwards MIDI to Vundu_MIDI over Wi-Fi, so an instrument can be played without a USB cable to the
computer. The server side has been ready since v0.0.1.

## Recommended flow

1. Join Wi-Fi, then find the server (fixed IP in settings first; mDNS can come later).
2. `POST /api/v1/remote/hello` with the board id, name and firmware version. The reply contains
   `udp_midi_port`. Repeat every 10-30 s as a heartbeat; the board then appears in `/api/v1/status`.
3. For every MIDI message read from the DIN socket, send its raw bytes in a UDP datagram to the server's
   `udp_midi_port` (default 5005). Batching several messages in one datagram is fine.
4. Optional: poll `GET /api/v1/status` to show the current preset on a display (for example an LCD
   Wizard screen), and `POST /api/v1/presets/load` or Program Change messages for preset buttons.

## Why UDP

HTTP per note adds connection overhead and jitter. A UDP datagram of 3 bytes on a local network
arrives in about a millisecond, which keeps playing feel tight. HTTP (`/api/v1/midi/send`) remains
available for buttons and occasional control changes.

## Hardware sketch

- MIDI IN: standard 6N138 (or H11L1) optocoupler circuit into an ESP32 UART RX pin, 31250 baud.
- Optional MIDI THRU / OUT for chaining.
- Any ESP32 with Wi-Fi (ESP32, ESP32-S3, ESP32-C3/C6).

## Firmware outline (Arduino)

```cpp
#include <WiFi.h>
#include <WiFiUdp.h>
#include <HTTPClient.h>

const char* SERVER = "192.168.1.50";   // Vundu_MIDI computer
const uint16_t UDP_PORT = 5005;
WiFiUDP udp;
HardwareSerial MidiSerial(1);

void hello() {
  HTTPClient http;
  http.begin(String("http://") + SERVER + ":5080/api/v1/remote/hello");
  http.addHeader("Content-Type", "application/json");
  http.POST("{\"id\":\"esp32-midi-1\",\"name\":\"DIN MIDI\",\"board\":\"ESP32-S3\",\"firmware\":\"0.1.0\"}");
  http.end();
}

void setup() {
  MidiSerial.begin(31250, SERIAL_8N1, /*rx*/ 16, /*tx*/ 17);
  WiFi.begin("ssid", "password");
  while (WiFi.status() != WL_CONNECTED) delay(200);
  hello();
}

void loop() {
  static uint8_t buf[64];
  static uint32_t lastHello = 0;
  size_t n = 0;
  while (MidiSerial.available() && n < sizeof(buf)) buf[n++] = MidiSerial.read();
  if (n) { udp.beginPacket(SERVER, UDP_PORT); udp.write(buf, n); udp.endPacket(); }
  if (millis() - lastHello > 15000) { hello(); lastHello = millis(); }
}
```

(Outline only - error handling, Wi-Fi settings storage and server discovery are for the firmware release.)
