# -*- coding: utf-8 -*-
"""ST7789 240x240 SPI display driver + mic_status dialogue loop (Orange Pi 4 Pro).

Wiring (40-pin header, SPI3):
  VCC -> Pin 1 (3.3V ONLY),  GND -> Pin 6
  SCL -> Pin 23 (SCLK.3),    SDA -> Pin 19 (MOSI.3)
  CS  -> Pin 24 (CE.0 / spidev3.0),  DC -> Pin 11 (wPi 5)
  RES -> Pin 13 (wPi 7),     BLK -> Pin 15 (wPi 8, high = on)

Run:  python3 st7789_display.py          (root: spidev + wiringpi need it)
Polls http://127.0.0.1:8899/mic_status twice a second and pushes frames.
"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, '/home/orangepi/chenvoice/app')
import dialogue_view  # noqa: E402

import spidev  # noqa: E402
import wiringpi  # noqa: E402

SPI_PORT, SPI_DEV = 3, 0
PIN_DC, PIN_RST, PIN_BLK = 5, 7, 8        # wPi numbers (Pin 11 / 13 / 15)
MADCTL_ROTATIONS = [0x00, 0x60, 0xC0, 0xA0]
ROTATE = 0
STATUS_URL = 'http://127.0.0.1:8899/mic_status'
STATE_URL = 'http://127.0.0.1:8899/state'
POLL_SEC = 0.5


def _rgb565(img):
    """PIL RGB image -> big-endian RGB565 bytes for the ST7789."""
    try:
        import numpy as np
        arr = np.asarray(img.convert('RGB'), dtype=np.uint16)
        v = ((arr[..., 0] & 0xF8) << 8) | ((arr[..., 1] & 0xFC) << 3) | (arr[..., 2] >> 3)
        return v.astype('>u2').tobytes()
    except ImportError:
        raw = img.convert('RGB').tobytes()
        out = bytearray(len(raw) // 3 * 2)
        j = 0
        for i in range(0, len(raw), 3):
            v = ((raw[i] & 0xF8) << 8) | ((raw[i + 1] & 0xFC) << 3) | (raw[i + 2] >> 3)
            out[j] = v >> 8
            out[j + 1] = v & 0xFF
            j += 2
        return bytes(out)


class ST7789:
    def __init__(self):
        wiringpi.wiringPiSetup()
        for pin in (PIN_DC, PIN_RST, PIN_BLK):
            wiringpi.pinMode(pin, 1)  # OUTPUT
        wiringpi.digitalWrite(PIN_BLK, 0)
        self.spi = spidev.SpiDev()
        self.spi.open(SPI_PORT, SPI_DEV)
        self.spi.max_speed_hz = 40_000_000
        self.spi.mode = 0
        self._init_panel()

    def _cmd(self, c, data=None):
        wiringpi.digitalWrite(PIN_DC, 0)
        self.spi.writebytes([c])
        if data:
            wiringpi.digitalWrite(PIN_DC, 1)
            self.spi.writebytes(list(data))

    def _init_panel(self):
        wiringpi.digitalWrite(PIN_RST, 0)
        time.sleep(0.1)
        wiringpi.digitalWrite(PIN_RST, 1)
        time.sleep(0.12)
        self._cmd(0x01)                 # SWRESET
        time.sleep(0.15)
        self._cmd(0x11)                 # SLPOUT
        time.sleep(0.12)
        self._cmd(0x3A, [0x55])         # COLMOD: 16-bit
        self._cmd(0x36, [MADCTL_ROTATIONS[ROTATE]])
        self._cmd(0x21)                 # INVON — IPS panels need it (else 0x20)
        self._cmd(0x2A, [0, 0, 0, 239])  # CASET
        self._cmd(0x2B, [0, 0, 0, 239])  # RASET
        self._cmd(0x13)                 # NORON
        self._cmd(0x29)                 # DISPON
        time.sleep(0.1)
        wiringpi.digitalWrite(PIN_BLK, 1)

    def display(self, img):
        self._cmd(0x2A, [0, 0, 0, 239])
        self._cmd(0x2B, [0, 0, 0, 239])
        self._cmd(0x2C)                 # RAMWR
        wiringpi.digitalWrite(PIN_DC, 1)
        data = _rgb565(img)
        for i in range(0, len(data), 4096):
            self.spi.writebytes(list(data[i:i + 4096]))

    def backlight(self, on):
        wiringpi.digitalWrite(PIN_BLK, 1 if on else 0)


def fetch_json(url):
    """GET a localhost JSON endpoint; None means unreachable."""
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception:
        return None


def fetch_status():
    return fetch_json(STATUS_URL)


def fetch_devices():
    payload = fetch_json(STATE_URL)
    return (payload or {}).get('groups') or []


def main():
    screen = ST7789()
    print('[display] ST7789 up, polling %s + %s' % (STATUS_URL, STATE_URL), flush=True)
    while True:
        payload = fetch_status()
        state = (payload or {}).get('state') or 'off'
        events = (payload or {}).get('events') or []
        frame = dialogue_view.render_frame(state, events, devices=fetch_devices())
        screen.display(frame)
        time.sleep(POLL_SEC)


if __name__ == '__main__':
    main()
