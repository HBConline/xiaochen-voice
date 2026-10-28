"""Dialogue session state: what the user just said, where they are, what time it is.

The intent layer judges each utterance in isolation, which breaks the moment a
person speaks the way people actually do — by reference and omission:

    "打开客厅灯"  -> fine
    "关掉它"      -> which one? (needs to remember 客厅灯)
    "再暗一点"    -> dim what, from what? (needs the last device)

This module is the memory that closes that gap. It is deliberately small and
in-process: one room, one device, one action, a short utterance history, plus a
coarse time-of-day. Enough to resolve 它/这个/那个/再/还 and to infer a current
room, and nothing more — a big context window is not the goal, working deixis is.

Resolution is conservative: if the reference is ambiguous (nothing memorable
yet, or two candidate devices), the caller must fall through rather than guess.
Same contract as microrules: precision over recall.
"""
import re
import threading
import time

# How many turns of raw text to keep for "刚才我说什么了" style follow-ups and
# for debugging. Small on purpose — this is a routing aid, not a transcript.
_HISTORY_MAX = 8

# Reference words that mean "the thing we were just talking about".
_DEVICE_REF = ("它", "这个", "那个", "这盏", "那盏", "这台", "那台")
# Verb-only follow-ups that inherit the last device+room and just change state.
_FOLLOW_UP_RE = re.compile(
    r"^(再|又|还|也要|给我)?(调|弄|搞|设|设成|调成)?(暗|亮|大|小|高|低|开|关)"
    r"(一点|一些|下|一下|点|些)?$"
)

_state_lock = threading.Lock()
_state = {
    "last_room": None,      # canonical room label, e.g. "living_room"
    "last_device": None,    # device class, e.g. "light"
    "last_action": None,    # e.g. "light_on"
    "history": [],          # [(ts, text, action, room, device), ...] oldest first
}


def note(text: str, action: str, room: str, device: str | None = None) -> None:
    """Record a resolved turn so the next utterance can refer to it."""
    with _state_lock:
        _state["last_action"] = action
        # Keep the last *real* room/device even across context-free turns: a bare
        # "关掉它" after "你好" should still resolve to the灯, not to nothing.
        if room and room not in ("none", "other"):
            _state["last_room"] = room
        if device:
            _state["last_device"] = device
        _state["history"].append((time.time(), text, action, room, device))
        if len(_state["history"]) > _HISTORY_MAX:
            _state["history"].pop(0)


def last_room():
    return _state["last_room"]


def last_device():
    return _state["last_device"]


def last_action():
    return _state["last_action"]


def recent(n: int = 1):
    """Most recent n recorded turns, newest first."""
    with _state_lock:
        return list(reversed(_state["history"][-n:]))


def has_reference(text: str) -> bool:
    """True if the utterance points back at a previous device ("关掉它")."""
    return any(w in (text or "") for w in _DEVICE_REF)


def is_bare_follow_up(text: str) -> bool:
    """True for a verb-only delta with no device noun ("再暗一点", "小一点").

    These carry a direction but no object, so they only make sense resolved
    against the last device — the caller pairs them with last_room/last_device.
    """
    t = re.sub(r"[，,。！!？?；;、\s~～]", "", text or "")
    if not t or len(t) > 8:
        return False
    return bool(_FOLLOW_UP_RE.match(t))


def time_of_day(ts: float | None = None) -> str:
    """Coarse bucket used to bias greetings and '晚安'-style replies."""
    hour = time.localtime(ts or time.time()).tm_hour
    if hour < 6:
        return "night"
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "evening"


def snapshot() -> dict:
    with _state_lock:
        return {
            "last_room": _state["last_room"],
            "last_device": _state["last_device"],
            "last_action": _state["last_action"],
            "time_of_day": time_of_day(),
            "history_len": len(_state["history"]),
        }


def reset() -> None:
    """Clear all session memory (tests, or a fresh conversation)."""
    with _state_lock:
        _state.update(last_room=None, last_device=None, last_action=None, history=[])
