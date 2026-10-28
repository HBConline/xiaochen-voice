"""Intent recognition for the smart home voice assistant — B3 orchestration.

The classifier is microrules: a full-element match for one atomic 开/关+设备+
房间 command, answered in <1ms. Everything else — negation, questions, compounds,
chit-chat, unknown devices — becomes action="other" and falls through to the
Qwen3 voice brain, which answers with MCP tools (voice_brain._chat_tools). A
named appliance this house lacks ("打开太阳能板") is claimed here as
absent_device so the reply refuses honestly instead of letting the model
improvise about hardware that does not exist.

History: this module used to load Laya, a 307M ModernBert encoder (~1.4GB RSS,
~8s per utterance on the Orange Pi 4 Pro), and before B3 the classifier was
intent_rules.py, a 679-line table. Measurement showed the table answered 98%
of real commands in <1ms and the encoder added memory and latency for nothing;
B3 cut the table down to microrules' one shape and moved everything else to
the model. No model is loaded here at all — importing this module costs
nothing.
"""
import os
import re
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

import microrules
import home_config

# compound-command splitter: punctuation + spoken connectives.
# 顿号(、) is deliberately NOT here: it is a pure noun-list separator ("客厅的
# 灯、卧室的灯都关掉"), so splitting on it severs the trailing verb from its
# first object. The list is left whole instead — microrules sees two device
# nouns and declines, and the model + tools drives both rooms.
_SPLIT = re.compile(r"[，,。！!？?；;]|然后再|然后|接着|还有|以及|并且")
# 和/跟/与 join two nouns far more often than two commands ("客厅的灯和卧室
# 的灯" is one command covering two rooms, not two clauses), so they are split
# only when BOTH sides independently parse as a room+action command. A blind
# character split would tear "温度调到26和30之间" apart.
_CONJ = re.compile(r"和|跟|与")

# repeated voice commands ("开灯" heard many times) skip the regex pass
_CACHE: dict = {}
_CACHE_MAX = 256

# Rule misses are appended here (one JSON object per line). This is the only
# way to see which real utterances the vocabulary does not cover, so the rules
# can be extended from evidence instead of guesswork. Set LOG_MISSES=0 to mute.
_MISS_LOG = os.environ.get(
    "MISS_LOG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "miss_log.jsonl")
)
_LOG_MISSES = os.environ.get("LOG_MISSES", "1") == "1"


def _log_miss(text: str) -> None:
    if not _LOG_MISSES:
        return
    try:
        import json
        with open(_MISS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "text": text}, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"miss-log write failed: {e}")


def _defer(text: str) -> dict:
    """Rule miss: hand off to the brain. Same shape as a rule hit, so callers
    cannot tell which path answered (that is the whole point of the contract)."""
    return {
        "action": "other",
        "room": "none",
        "urgency": 0.0,
        "urgency_level": 0,
        "text": text,
        "confidence": 0.0,
        "source": "defer",
    }


def _remember(cmd: dict) -> None:
    """Record a resolved command in the session so the next turn can refer to it.

    Only device-shaped actions are worth remembering; a query or a scene leaves
    the last device/room untouched (a bare "关掉它" after "晚安" should still
    resolve to the灯, not to the scene).
    """
    try:
        import session
        action = cmd.get("action")
        device = home_config.ACTION_DEVICE.get(action, (None, None))[0]
        session.note(cmd.get("text", ""), action, cmd.get("room", "none"), device)
    except Exception:
        pass


def _annotate(cmd: dict) -> dict:
    """Tag a command with whether the device layer can actually carry it out.

    Checked here, at intent time, so the pipeline knows before it tries that
    "把灯调暗" has no hardware behind it — the reply can refuse up front instead
    of the device layer discovering it mid-execute. Adds `supported` and, when
    False, a spoken `reason`; leaves everything else untouched.
    """
    # action="other" means "no device action here — let the brain talk", which
    # is a different thing from "unsupported". Tagging it False made every
    # deferred utterance (status questions, chit-chat) answer with the refusal
    # line, so the brain never got its turn: "客厅灯开着吗" said "这个功能还没有
    # 接上" instead of reporting state.
    if cmd.get("action", "other") == "other":
        out = dict(cmd)
        out.setdefault("supported", True)
        return out
    try:
        import home_config
        sup, reason = home_config.is_supported(cmd.get("action", "other"))
    except Exception:
        return cmd
    out = dict(cmd)
    # An out-of-range value is a recognised request the house refuses, not a
    # deferral: say the range, do not let the reply invent an execution.
    if cmd.get("out_of_range"):
        sup = False
        reason = "这个数值超出范围了"
    # A named appliance this house does not have. Name it back so the refusal is
    # specific ("家里没有太阳能板") instead of the generic capability line.
    if cmd.get("action") == "absent_device":
        sup = False
        name = cmd.get("absent_device") or "这个设备"
        reason = f"家里没有{name}，换不了它。"
    # A declined command ("不要开灯"). The device is named so the refusal reads as
    # an acknowledgement ("灯就不动了，听你的") rather than the capability line
    # voice_brain speaks for any other supported=False action. Setting the reason
    # HERE is what routes it: voice_brain.reply refuses on `supported is False`
    # before any model turn runs — B3's tts.make_reply only reports execution
    # results, so the refusal phrasing has to live on the command itself.
    if cmd.get("action") == "negated":
        sup = False
        device = cmd.get("negated_device") or ""
        reason = f"{device or '它'}就不动了，听你的"
    out["supported"] = sup
    if not sup and reason:
        out["unsupported_reason"] = reason
    return out


def _remember_cached(primary: dict) -> None:
    """Re-note a cache-hit intent's commands in the session.

    A cache hit is still a spoken turn; without this, the session silently
    stops learning after the first time a phrase is heard, and every deictic
    follow-up ("关掉它") fails to resolve. Only device-shaped commands count —
    a chit-chat hit must not clobber the last real room/device.
    """
    for cmd in primary.get("commands") or [primary]:
        if cmd.get("source") in ("microrules", "rule", "session"):
            _remember(cmd)


def _acts_as_command(clause: str) -> bool:
    """True when a fragment is a device command on its own.

    Used to decide whether a 和/跟/与 is joining two clauses or two nouns.
    "卧室的灯都关掉" parses (bedroom + light_off); "30之间" does not.
    """
    try:
        hit = microrules.match(clause)
    except Exception:
        return False
    return hit is not None


# A second verb glued to the first with no connective at all: "关掉厨房灯打开客厅
# 空调". _SPLIT only knows punctuation and connectives, so this arrived as one
# fragment; _match_action then returned the LONGEST match and the other verb +
# device vanished (the AC was never touched and the light came back as ON,
# because the surviving match was 打开). Splitting before the second verb gives
# each half its own parse.
#
# The lookbehind requires a device noun or room right before the split point, so
# a verb inside a single phrase ("把灯打开调暗") is not torn apart.
_SECOND_VERB = re.compile(
    r"(?<=[灯空调扇视帘窗])"
    r"(打开|关掉|关闭|开启|点亮|拉开|拉上|开一?下|关一?下|开灯|关灯"
    r"|调暗|调亮|调高|调低|调大|调小)"
)
# A second verb introduced by a bare 再. _SPLIT knows 然后再/然后/接着 but not the
# common clipped form, so "把客厅灯打开再调暗" and "先开灯再关空调" arrived as one
# fragment and _match_action returned the longest match — the second command
# vanished silently (先开灯再关空调 switched the lamp and never touched the AC).
# Cut at 再 only, keeping it attached to the clause it introduces, and rely on
# the same both-halves-parse guard so a 再 inside one phrase is not torn apart.
_RESPLIT = re.compile(r"再(?=[一-鿿])")


def _split_noverb(parts: list[str]) -> list[str]:
    """Split a fragment that packs two verbs with no connective between them.

    "关掉厨房灯打开客厅空调" is two commands; "把客厅灯打开调暗一点" is one (the
    second verb modifies the same lamp). The guard is the same as _split_conj's:
    only split when BOTH halves parse as commands on their own, which "打开调暗
    一点" does not.
    """
    out = []
    for part in parts:
        # re.split with a CAPTURING group keeps the verb as its own element, so
        # the raw output is [left, verb, right, verb, right, …]: the verb has to
        # be glued back onto the clause it introduced. A lookahead split (the
        # first attempt) sliced twice at the same spot and dropped the verb.
        raw = [p.strip() for p in _SECOND_VERB.split(part) if p and p.strip()]
        if len(raw) < 3:
            out.append(part)
            continue
        pieces = [raw[0]] + [raw[i] + raw[i + 1] for i in range(1, len(raw) - 1, 2)]
        if not all(_acts_as_command(p) for p in pieces):
            out.append(part)
            continue
        out.extend(pieces)
    return out


def _split_resplit(parts: list[str]) -> list[str]:
    """Split a fragment whose two commands are glued by a bare 再.

    "把客厅灯打开再调暗" is two instructions, but _SPLIT's connective list has
    only 然后再/然后/接着, so it survived as one fragment and _match_action kept
    the longest match: the lamp was switched and the 调暗 was dropped without a
    word. The guard is _split_noverb's — both halves must parse as commands on
    their own — so a 再 that does not separate two real commands ("还有没有再
    便宜点") is left alone. The 再 stays with the clause it introduces, since a
    reply that echoes "再调暗" reads better than a bare "调暗".
    """
    out = []
    for part in parts:
        raw = [p.strip() for p in _RESPLIT.split(part) if p and p.strip()]
        if len(raw) < 2:
            out.append(part)
            continue
        pieces = [raw[0]] + ["再" + p for p in raw[1:]]
        if not all(_acts_as_command(p) for p in pieces):
            out.append(part)
            continue
        out.extend(pieces)
    return out


def _fill_verb(parts: list[str]) -> list[str]:
    """Share one verb across a list of objects glued by 和/跟/与.

    "开客厅的灯和卧室的空调" is a single spoken clause: one verb (开) and two
    objects (客厅的灯, 卧室的空调). A whole-string parse cannot see that — it
    finds the verb, finds the first device, and stops, so the AC was reported as
    a lamp. The clause has to be cut at the connective FIRST, while the verb is
    still in front of the first object, and the verb then copied onto every
    object that lacks one.

    Split here is deliberately unconditional (any 和/跟/与), because the
    "both sides must parse" guard cannot apply yet — the second object does not
    parse until it has been given a verb, which is this function's whole job.
    The guard lives downstream in _split_conj, which then rejects the nouns it
    should not have split apart ("温度调到26和30之间" comes back as two halves
    that still do not parse, so it is rejoined by reassembly… see there).

    Only runs on a fragment that carries BOTH a verb and at least two device
    objects; anything else is returned untouched.
    """
    out = []
    for part in parts:
        verb = microrules.verb_in(part)
        if verb is None or len(_CONJ.findall(part)) == 0:
            out.append(part)
            continue
        objects = [p.strip() for p in _CONJ.split(part) if p and p.strip()]
        if len(objects) < 2:
            out.append(part)
            continue
        # EVERY piece must name a device. The verb can sit on any of them —
        # "开客厅的灯和卧室的空调" leads with it, while "卧室和客厅的灯都打开"
        # trails it, and the verb travels wherever it appears. A piece with no
        # device ("26" in "温度调到26和30之间") means this is not a shared verb
        # over objects at all, so nothing is split here.
        if not all(microrules.device_in(p) is not None for p in objects):
            out.append(part)
            continue
        word = "打开" if verb == "on" else "关掉"
        for obj in objects:
            if microrules.verb_in(obj):
                out.append(obj)
                continue
            # The verb goes in front of the object; parse() then sees a normal
            # self-contained clause and resolves room + device itself.
            out.append(f"{word}{obj}")
    return out


def _split_conj(parts: list[str]) -> list[str]:
    """Split "A和B" only when both sides are self-sufficient commands.

    和 joins nouns more often than clauses in house speech: "客厅的灯和卧室的
    灯都关掉" shares one trailing verb across two rooms — _fill_verb copies the
    verb onto each object first, so both halves then parse and both rooms get
    toggled. But "温度调到26和30之间" must stay whole, so the guard is
    symmetric: split only if BOTH sides parse.

    A fragment with no 和/跟/与 is passed through untouched.
    """
    out = []
    for part in parts:
        pieces = [p.strip() for p in _CONJ.split(part) if p and p.strip()]
        if len(pieces) < 2 or not all(_acts_as_command(p) for p in pieces):
            out.append(part)
            continue
        out.extend(pieces)
    return out


def understand(text: str) -> dict:
    """Convert ASR text to smart home intent(s).

    B3 micro-rules first: microrules.match() answers atomic 开/关+设备+房间
    commands in <1ms. A miss becomes action="other", which downstream treats as
    "let the brain + MCP tools talk" rather than a device command. A named
    appliance this house does not have ("打开太阳能板") is claimed here as
    absent_device so the reply refuses honestly instead of letting the model
    improvise a claim about hardware that does not exist.

    Compound sentences ("打开空调，关掉灯") are split and parsed per clause.
    Returns: {action, room, urgency, urgency_level, text, confidence, commands:[...]}
    Top-level fields mirror the FIRST command (backward compat).
    """
    cached = _CACHE.get(text)
    if cached is not None:
        # Replaying a memorised utterance is a fresh turn: the session must see
        # it, or a repeated "打开客厅灯" leaves session empty and the very next
        # "关掉它" has nothing to resolve against. match() does this on a miss;
        # the cache sits in front of match(), so it has to do it here too.
        _remember_cached(cached)
        return cached
    parts = [p.strip() for p in _SPLIT.split(text) if p and p.strip()]
    if not parts:
        parts = [text]
    # ORDER MATTERS: _fill_verb runs BEFORE _split_conj. Its job is to hand a
    # verbless device clause the verb its sibling established, and _split_conj's
    # guard ("both sides must parse as commands") rejects exactly those verbless
    # clauses — so running the splitter first deadlocks: the guard blocks the
    # split, the split is what the filler waited for. Filling first turns
    # "卧室的空调" into "打开卧室的空调", which then both splits cleanly.
    #
    # _split_noverb stays last: it reads the verb pattern directly rather than
    # via match(), so it is not blocked by the same guard, and it must see the
    # filler's output as one fragment when the two verbs share a device.
    parts = _fill_verb(parts)
    parts = _split_conj(parts)
    parts = _split_noverb(parts)
    parts = _split_resplit(parts)

    commands = []
    for p in parts:
        hit = microrules.match(p)
        if hit is not None:
            hit.setdefault("urgency", 0.0)
            hit.setdefault("urgency_level", 0)
            hit.setdefault("confidence", 1.0)
        else:
            # A verb + a known-absent appliance ("打开太阳能板") is a request we
            # must refuse by name, not hand to a model that will pretend to
            # carry it out.
            absent = home_config.find_absent_device(p)
            if absent:
                hit = {
                    "action": "absent_device",
                    "room": "none",
                    "urgency": 0.0,
                    "urgency_level": 0,
                    "text": p,
                    "confidence": 1.0,
                    "source": "rule",
                    "absent_device": absent,
                }
            else:
                _log_miss(p)
                hit = _defer(p)
        commands.append(_annotate(hit))

    primary = dict(commands[0])
    primary["text"] = text
    primary["commands"] = commands
    # Session-resolved turns must NOT be cached: "关掉它" means a different
    # device in a different context, so a memorised answer would keep switching
    # the first lamp forever. Rule hits are context-free and safe to cache.
    if any(c.get("source") == "session" for c in commands):
        return primary
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))  # drop oldest insertion
    _CACHE[text] = primary
    return primary


if __name__ == "__main__":
    test_cases = [
        "打开客厅灯",
        "关闭卧室灯",
        "把温度调高一点",
        "播放音乐",
        "打开窗帘",
        "晚安",
        "今天天气怎么样",
        "你好呀今天心情不错",
    ]
    print("=== Rule Intent Test ===\n")
    for text in test_cases:
        intent = understand(text)
        print(f"输入: {text}")
        print(f"  动作: {intent['action']}  房间: {intent['room']}  "
              f"来源: {intent.get('source')}")
        print()
