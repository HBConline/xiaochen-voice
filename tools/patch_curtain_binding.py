# -*- coding: utf-8 -*-
"""One-shot patch: device-scoped relay bindings for curtain in device_layer.py."""
import io
import sys

PATH = "/home/orangepi/chenvoice/app/device_layer.py"
src = io.open(PATH, encoding="utf8", newline="").read()

ANCHOR_HELP = """    # --- state store -------------------------------------------------------"""
HELP_NEW = '''    def _binding_keys(self, pins: dict, device: str, targets: list) -> dict:
        """Map target rooms to backend binding keys for non-light devices.

        Lights bind per room (`topics[room]`). A curtain is one appliance: it
        binds through a device-scoped key - `curtain:<room>` when that room has
        one, or bare `curtain` for the roomless command (打开窗帘). A target
        with no key stays state-only instead of driving someone else's
        hardware (卧室窗帘 must not move the living-room curtain).
        """
        out = {}
        for r in targets:
            key = f'{device}:{r}'
            if key in pins:
                out[r] = key
            elif r == 'none' and device in pins:
                out[r] = device
        return out

    # --- state store -------------------------------------------------------'''

ANCHOR_HW = """        pins = getattr(self.backend, '_pins', None) or {}
        has_hardware = device in ('light',) and any(t in pins for t in targets)"""
HW_NEW = """        pins = getattr(self.backend, '_pins', None) or {}
        if device == 'light':
            # Legacy room-keyed binding; every target goes to the backend.
            binds = {r: r for r in targets}
            has_hardware = any(t in pins for t in targets)
        else:
            binds = self._binding_keys(pins, device, targets)
            has_hardware = bool(binds)"""

ANCHOR_LOOP = """            for r in targets:
                try:
                    self.backend.set_state(r, want)
                    done.append(r)"""
LOOP_NEW = """            for r in targets:
                if r not in binds:
                    continue
                try:
                    self.backend.set_state(binds[r], want)
                    done.append(r)"""

for anchor, new, tag in (
    (ANCHOR_HELP, HELP_NEW, "help"),
    (ANCHOR_HW, HW_NEW, "hw"),
    (ANCHOR_LOOP, LOOP_NEW, "loop"),
):
    n = src.count(anchor)
    if n != 1:
        sys.exit("anchor %s count %d != 1" % (tag, n))
    src = src.replace(anchor, new, 1)

io.open(PATH, "w", encoding="utf8", newline="").write(src)
print("[patched]", PATH)
