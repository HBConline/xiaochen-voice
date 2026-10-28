# -*- coding: utf-8 -*-
"""Tests for voice_state — TTL'd pipeline phase for the display (聆听中/思考中/说话中).

Run: PYTHONUTF8=1 python test_voice_state.py  (exit code 0 = all pass)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

import voice_state

FAILS = []
N = [0]


def check(name, cond, detail=""):
    N[0] += 1
    status = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print("[%s] %s %s" % (status, name, detail if not cond else ""))


voice_state.reset()
check("default_waiting", voice_state.effective_state(now=0) == "waiting_wake")

voice_state.set_state("listening", ttl=8, now=100)
check("set_sticky_within_ttl", voice_state.effective_state(now=105) == "listening")

check("ttl_expires_to_waiting",
      voice_state.effective_state(now=108.5) == "waiting_wake")

voice_state.set_state("speaking", ttl=2.5, now=200)
check("speaking_holds", voice_state.effective_state(now=202) == "speaking")
check("speaking_expires", voice_state.effective_state(now=203) == "waiting_wake")

# zero ttl = no expiry
voice_state.set_state("waiting_wake", ttl=0, now=300)
check("no_ttl_sticky", voice_state.effective_state(now=1e9) == "waiting_wake")

# 'off' is sticky too (mic disabled)
voice_state.set_state("off", ttl=5, now=400)
check("off_sticky", voice_state.effective_state(now=1e9) == "off")

# later set_state overrides earlier regardless of prior ttl
voice_state.set_state("listening", ttl=30, now=500)
voice_state.set_state("processing", ttl=30, now=501)
check("override_wins", voice_state.effective_state(now=502) == "processing")

# bare set_state uses wall clock and still works
voice_state.reset()
voice_state.set_state("listening", ttl=60)
check("wall_clock_default", voice_state.effective_state() == "listening")

print("%d checks, %d fail" % (N[0], len(FAILS)))
if FAILS:
    print("FAILED:", ", ".join(FAILS))
sys.exit(1 if FAILS else 0)
