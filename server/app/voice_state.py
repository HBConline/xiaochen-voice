# -*- coding: utf-8 -*-
"""Pipeline phase with TTL, for the display (聆听中/思考中/说话中).

The node path (wake -> record -> /node/audio) never touched MIC_STATE['state'],
so the screen sat on 待机 during whole voice turns. Every phase now flows
through set_state(); transient phases carry a TTL so a lost packet or crashed
client cannot freeze the screen on 聆听中 forever — expiry falls back to
waiting_wake. 'off' and 'waiting_wake' are sticky (no expiry).
"""
import time

STICKY = ('off', 'waiting_wake')

_state = 'waiting_wake'
_deadline = 0.0


def reset():
    """Back to defaults (tests)."""
    global _state, _deadline
    _state = 'waiting_wake'
    _deadline = 0.0


def set_state(state, ttl=0.0, now=None):
    """Set the current phase. ttl seconds from now; 0 = never expires."""
    global _state, _deadline
    _state = state
    if ttl and state not in STICKY:
        _deadline = (time.time() if now is None else now) + ttl
    else:
        _deadline = 0.0


def effective_state(now=None):
    """Current phase, honouring TTL expiry."""
    if _deadline and (time.time() if now is None else now) >= _deadline:
        return 'waiting_wake'
    return _state
