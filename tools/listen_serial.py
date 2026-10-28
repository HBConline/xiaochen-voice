# -*- coding: utf-8 -*-
"""Listen on COM5 at 115200 and 74880 for any boot bytes while user power-cycles."""
import time

import serial

PORT = "COM5"
WINDOW = 60


def listen(baud, seconds):
    ser = serial.Serial(PORT, baud, timeout=0.2)
    ser.reset_input_buffer()
    end = time.time() + seconds
    got = b""
    while time.time() < end:
        chunk = ser.read(4096)
        if chunk:
            got += chunk
    ser.close()
    return got


def main():
    print("[listen] %d s window, 115200 then 74880 - power-cycle VCC now" % WINDOW, flush=True)
    half = WINDOW // 2
    for baud in (115200, 74880):
        got = listen(baud, half)
        if got:
            print("[baud %d] %d bytes:" % (baud, len(got)), flush=True)
            print(repr(got[:400]), flush=True)
        else:
            print("[baud %d] 0 bytes" % baud, flush=True)
    print("[done]", flush=True)


if __name__ == "__main__":
    main()
