# -*- coding: utf-8 -*-
"""Micro-rules: the only deterministic path left in B3.

Exactly one shape is matched here — an atomic "switch a device on/off in a
room" command with all three elements (verb, device, room or explicit
default) resolvable from the utterance itself. A hit returns in under a
millisecond and never touches the language model. Everything else —
negation, questions, compound sentences, unknown devices, chitchat —
returns None and belongs to the model + MCP tools path.

Design rule: full-element match only. If anything is left over after
removing the verb, device, room and politeness fillers, the utterance is
doing more than one thing and we defer. This is what keeps the 50-line rule
from growing back into the old 679-line rule layer.
"""
import re

from home_config import ACTION_DEVICE, QUERY_DEVICE_HINT, ROOM_WORDS

# on/off verbs, longest first so 关闭 beats 关 and 拉开 beats 开.
_ON_VERBS = ("打开", "开启", "启动", "拉开", "开")
_OFF_VERBS = ("关闭", "关掉", "关上", "拉上", "关")

# Anything semantic we do not model: negation, questions, multi-command joins.
_DEFER = re.compile(
    r"不|没|别|莫|禁|吗|呢|[？?]|多少|几|什么|怎|为什|为啥|是不是|有没有|多久"
    r"|然后|接着|还有|以及|并且|和|跟|与|再|先|边"
)

# Politeness/grammar noise stripped before the leftover check. 呢 is a defer
# marker (question), not a filler — it stays out of this list on purpose. 都 is
# the distributive in "卧室的灯都关掉" (the splitter hands us that clause shape
# after sharing one verb across several rooms).
_FILLERS = ("帮我", "给我", "麻烦", "请", "一下", "一哈",
            "把", "将", "的", "了", "呗", "吧", "啊", "呀", "哦", "嘛", "哈", "嗯", "去", "都")

# device+on -> action name, from the canonical table (no drift).
_SET_ACTION = {(dev, on): act for act, (dev, on) in ACTION_DEVICE.items()}


def _find_spans(text: str, words):
    """All (start, end, word) occurrences of `words`, longest-match wins on
    overlap: a hit is dropped when it sits inside a longer hit."""
    hits = []
    for word in words:
        start = 0
        while True:
            i = text.find(word, start)
            if i < 0:
                break
            hits.append((i, i + len(word), word))
            start = i + 1
    out = []
    for h in hits:
        longer_overlap = any(
            g[0] <= h[0] and h[1] <= g[1] and (g[1] - g[0]) > (h[1] - h[0])
            for g in hits
        )
        if not longer_overlap:
            out.append(h)
    out.sort()
    return out


def match(text: str):
    """{action, room, text, source} for an atomic on/off command, else None."""
    t = (text or "").strip()
    if not t or _DEFER.search(t):
        return None

    # Exactly one verb, and it must be an on/off verb.
    verbs = []
    for on, group in ((True, _ON_VERBS), (False, _OFF_VERBS)):
        for v in group:
            start = 0
            while True:
                i = t.find(v, start)
                if i < 0:
                    break
                verbs.append((i, i + len(v), v, on))
                start = i + 1
    # Drop short verbs nested in longer ones (关 inside 关闭).
    verbs = [v for v in verbs
             if not any(g[0] <= v[0] and v[1] <= g[1] and (g[1] - g[0]) > (v[1] - v[0])
                        for g in verbs)]
    if len(verbs) != 1:
        return None
    v_start, v_end, _v, want_on = verbs[0]

    # Exactly one device occurrence (room words like 所有灯 may contain it).
    devs = _find_spans(t, [w for w, _c in QUERY_DEVICE_HINT])
    if len(devs) != 1:
        return None
    d_start, d_end, d_word = devs[0]
    device = next(c for w, c in QUERY_DEVICE_HINT if w == d_word)

    # At most one room.
    rooms = _find_spans(t, list(ROOM_WORDS))
    if len(rooms) > 1:
        return None
    if rooms:
        r_start, r_end, r_word = rooms[0]
        room = ROOM_WORDS[r_word]
    else:
        r_start = r_end = -1
        room = "none"

    # Leftover check: remove the three elements and fillers; anything left
    # means the utterance does more than one thing.
    cut = list(t)
    for a, b in ((v_start, v_end), (d_start, d_end)) + (
            ((r_start, r_end),) if r_start >= 0 else ()):
        for i in range(a, b):
            cut[i] = ""
    leftover = "".join(cut)
    for f in _FILLERS:
        leftover = leftover.replace(f, "")
    if leftover.strip():
        return None

    action = _SET_ACTION.get((device, want_on))
    if action is None:
        return None
    return {"action": action, "room": room, "text": t, "source": "microrules"}


# ---------------------------------------------------------------------------
# Small vocabulary probes for intent.py's compound splitter. The splitter needs
# to know "does this fragment carry its own verb / name a device" before it can
# share one verb across a noun list; both probes read the SAME word tables match
# uses, so the splitter and the matcher can never disagree about vocabulary.
# ---------------------------------------------------------------------------

def verb_in(text: str):
    """'on' / 'off' when the clause carries an open/close verb, else None."""
    if any(v in (text or "") for v in _ON_VERBS):
        return "on"
    if any(v in (text or "") for v in _OFF_VERBS):
        return "off"
    return None


def device_in(text: str):
    """The device class named in `text`, or None (first hint wins)."""
    for word, dev in QUERY_DEVICE_HINT:
        if word in (text or ""):
            return dev
    return None
