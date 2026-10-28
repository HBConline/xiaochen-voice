"""Voice brain: Qwen3-0.6B-xiaochen voice model client (speaking text only).

Model: D:\\models\\Qwen3-0.6B-xiaochen-Q4_K_M.gguf (persona 「小宸同学」,
speed-tuned short replies, avg 9.4 chars). Fine-tuned to be a pure speech
model: it receives the user question plus tool-model context and returns
colloquial spoken text. It never emits tool calls — tool execution stays with
the intent/tool layer.

Context conventions (data/make_pure_voice_v2.py):
    执行结果：switch_light(客厅, on) 成功
    执行结果：... 失败：设备离线，没有响应
    设备状态：客厅灯=开启 / 设备状态：客厅空调=开启，制冷，24度
Hard rule from training: SCENE utterances ("我回来了" etc.) never get an
执行结果 line — context-free is the trained distribution.

Served by llama-server (OpenAI-compatible). Runs under the lerobot env like
wakeword_server; only stdlib is used so no extra dependency is needed.
"""
import json
import os
import re
import urllib.request

import home_config

# llama-server endpoint. 8080 is what scripts/start_llama.sh actually serves;
# 8091 was a stale default that silently sent every reply to the template
# fallback (health check failed, callers could not tell). Keep the two in sync.
BRAIN_URL = os.environ.get("BRAIN_URL", "http://127.0.0.1:8080/v1/chat/completions")
TIMEOUT_S = float(os.environ.get("BRAIN_TIMEOUT_S", "15"))
# Device turns stay short — the reply is a confirmation the rule layer usually
# writes better anyway. Chit-chat gets room to breathe: at 40 tokens "讲个笑话"
# died mid-sentence and an open "你好我爱你" felt clipped. 96 tokens is roughly
# 2-3 spoken sentences at this model's Chinese packing; past that the 0.6B
# starts drifting into topic loops.
MAX_NEW_TOKENS = 40
CHITCHAT_MAX_NEW_TOKENS = 96

# target hardware is Orange Pi 4 Pro (ARM CPU, 4GB shared RAM) — keep every
# stage CPU-friendly and bounded; nothing here may require CUDA.

# intent room -> Chinese room name, from the single source of truth.
from home_config import ROOM_ZH as _ROOM_ZH
from home_config import ALL_ROOMS as _ALL_ROOMS_ZH

# intent action -> tool call detail template. ONLY these four forms appear in
# training with 执行结果 context (data/make_pure_voice.py); anything else must
# go context-free — see build_context.
_TOOL_DETAIL = {
    "light_on": "switch_light({room}, on)",
    "light_off": "switch_light({room}, off)",
    "ac_on": "set_aircon({room}, on)",
    "ac_off": "set_aircon({room}, off)",
}
_CONTEXT_ACTIONS = set(_TOOL_DETAIL)


# status/question markers: these utterances must NOT receive an execution-result
# context (the model would answer "已打开" to "灯开着吗"). No device state is
# available yet, so they go context-free and the model asks or reports honestly.
_QUERY_MARKERS = ("吗", "没开", "开没", "状态", "多少度", "几度", "查", "看看",
                  "是什么", "怎么样", "有没有", "多久", "几点", "天气")


# Actions that ARE device commands. When the rule layer resolved one of these,
# the utterance is an order no matter how politely it was phrased — "打开灯好吗"
# carries a 吗 but means "switch it on", and answering with the lamp's state (as
# the marker-only check did) dropped the command entirely.
_DEVICE_ACTIONS = set()  # filled lazily from home_config.ACTION_DEVICE


def _looks_like_query(text: str, intent: dict | None = None) -> bool:
    if intent:
        cmds = intent.get("commands") or [intent]
        acts = [c.get("action", "") for c in cmds]
        if any(a.startswith(("query_", "scene_")) for a in acts):
            return True
        if any(a in home_config.ACTION_DEVICE or a in home_config.ACTION_REQUIREMENT
               for a in acts):
            return False
    return any(m in (text or "") for m in _QUERY_MARKERS)


# safety net: spoken Chinese must never contain raw English tokens (the model
# occasionally echoes tool-name fragments like "Home"). Values are bare scene
# names; the replacement always yields "<scene>模式", consuming any trailing
# 模式 so "movie模式" does not become "影院模式模式".
_LEAK_MAP = {
    "home": "回家", "movie": "影院", "sleep": "睡眠",
    "away": "离家", "party": "派对", "read": "阅读",
    "on": "", "off": "",
}


# Phrases the base model emits that promise action it cannot take or claim a
# state it does not know. A reply containing one is dropped (None) so the
# caller falls back to a template rather than speaking a false promise.
_FALSE_PROMISE = ("我正在尝试", "请稍等", "马上", "已经为您", "已为您",
                  "正在为您", "这就去", "稍等一下", "我来帮你看看",
                  "我来帮您看看", "帮您看看", "帮你看看")


# A deferred (action="other") utterance was NOT executed — no device moved. If
# the reply asserts a device state change anyway ("温度调到0度了，感觉有点凉爽"),
# it is reporting an action that never happened. Checked only for chit-chat /
# deferred turns; a real command's confirmation is expected to name devices.
_STATE_CLAIM = ("调到", "开了", "关了", "打开了", "关闭了", "调好", "设成",
                "已开", "已关", "启动了")


def _claims_unmade_action(text: str, intent: dict) -> bool:
    """True if a deferred turn's reply claims a device change that never ran."""
    cmds = intent.get("commands") or [intent]
    if any(c.get("action") not in ("other", None) for c in cmds):
        return False  # a real command ran; device talk is legitimate
    return any(p in (text or "") for p in _STATE_CLAIM)


def _is_false_promise(text: str) -> bool:
    return any(p in (text or "") for p in _FALSE_PROMISE)


def _scrub_english(text: str) -> str:
    import re

    def rep(m):
        bare = _LEAK_MAP.get(m.group(1).lower())
        if bare is None:
            return ""
        return bare + ("模式" if bare else "")

    out = re.sub(r"([A-Za-z]+)(模式)?", rep, text)
    # The untuned base model decorates every sentence with emoji; a TTS voice
    # reads them as silence or garbage, so strip them. Also drop markdown
    # emphasis the base model adds, which the voice cannot render either.
    out = re.sub(r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF️]", "", out)
    out = re.sub(r"[*_`#>]", "", out)
    out = re.sub(r"\s+", " ", out)
    return out.strip() or text


# question-word -> device class and device class -> Chinese noun both come from
# the single source of truth, so a status question and the reply agree on terms.
from home_config import QUERY_DEVICE_HINT as _QUERY_DEVICE_HINT, DEVICE_ZH as _DEVICE_ZH


def _find_room_in_text(text: str) -> str:
    """Room label mentioned in the utterance, or 'none'. Reads the shared
    home_config vocabulary so the brain and the rules can never disagree on
    what 客厅 means."""
    try:
        return home_config.find_room(text) or "none"
    except Exception:
        return "none"


_CN_DIGIT = {0: "零", 1: "一", 2: "二", 3: "三", 4: "四",
             5: "五", 6: "六", 7: "七", 8: "八", 9: "九"}

# time.localtime().tm_wday: Monday=0. Index 0 is 一, not 日, so this is not
# _CN_DIGIT and must not be built from it.
_CN_WEEKDAY = ("一", "二", "三", "四", "五", "六", "日")


def _num_zh(n: int) -> str:
    """0-99 as Chinese numerals, for anything that will be spoken.

    "今天是10月13号" reached the TTS front end as Arabic digits, and its lexicon
    dropped every one of them (character-lexicon Ignore OOV '10' / '13'), so the
    spoken date came out as 今天是月号. Everything the user hears is spelled out.
    """
    if n < 0:
        return str(n)
    tens, ones = divmod(n, 10)
    if n < 10:
        return _CN_DIGIT[ones]
    if tens == 1:
        return "十" + (_CN_DIGIT[ones] if ones else "")
    return _CN_DIGIT[tens] + "十" + (_CN_DIGIT[ones] if ones else "")


# Only formulaic social turns belong here — ones with no content to answer.
# A broad "心情|无聊|累" pattern swallowed real openers ("我有点无聊") and
# returned a flat "嗯，我在听着呢" where the base model would have chatted;
# the canned line is a floor for small talk, not a replacement for it.
_CHITCHAT_REPLIES = (
    (r"^(你好|哈喽|嗨|早上好|中午好|下午好|晚上好|早安)(呀|啊|哦|老师)?$", "你好，我在。"),
    (r"^(谢谢|多谢|感谢)(你|啦|了)?$", "不客气。"),
    (r"^(再见|拜拜|回见|没事了|没事)$", "好，随时叫我。"),
    # Bare acknowledgements ("好的", "收到") arrive as their own turn when the
    # speaker answers a question the assistant asked. They carry no command, so
    # the brain inventing an execution ("好，都关好了") would be a lie; a plain
    # 好 confirms the turn was heard without claiming anything happened.
    (r"^(好的|好|行|收到|明白|知道了|嗯|哦)$", "好。"),
    # Who the assistant is, is a fact about this build, not a thing to generate.
    # Left to the brain it produced "我是小宸同学，帮你管灯和空调。" — right by luck
    # here, but the model has no access to the real capability list and will
    # happily claim features this house does not have. The menu is fixed.
    (r"^(你是谁|你叫什么|你叫什么名字|你是什么|介绍一下你(自己)?)$",
     "我是小宸同学，管这套房子的灯、空调、风扇、电视和窗帘。"),
)

# Normalisation for social repeat-detection. "你好，你好。" arrives split into
# two clauses by intent._SPLIT, so the anaphora has to be collapsed before the
# greeting test — otherwise the run never matches and the raw clause pair falls
# through to the template, which prints one "收到" per clause ("好的，收到，然后
# 收到。"). Punctuation and filler particles carry no meaning here.
_SOCIAL_STRIP_RE = re.compile(r"[，,。！!？?；;、\s~～吧呗啊呀哦嗯嘿哈啦了]")
_SPEAKER_STRIP_RE = re.compile(r"[，,。！!？?；;、\s~～吧呗啊呀嗯嘿哈]")


def _collapse_social(text: str) -> str:
    """Collapse a repeated social utterance to one instance: "你好你好你好" and
    "你好，你好。" both become "你好".

    Returns "" when the utterance is not a single repeated social token, so the
    caller can tell "谢谢谢谢" (one 谢谢) from "你好谢谢" (two different turns that
    deserve the brain, not a canned line).
    """
    t = _SOCIAL_STRIP_RE.sub("", text or "")
    if not t:
        return ""
    for base in ("你好", "哈喽", "嗨", "早上好", "中午好", "下午好", "晚上好",
                 "早安", "谢谢", "多谢", "感谢", "再见", "拜拜", "回见", "没事"):
        if t and t == base * (len(t) // len(base)):
            return base
    return ""


def _chitchat_answer(text: str) -> str | None:
    """A fixed line for plain social openers, or None.

    The 0.6B brain answers "你好呀今天心情不错" with "好，都关好了，欢迎回家" —
    a leftover from its house-command training distribution. Chat openers have
    no informational content, so the model adds nothing but risk; a canned line
    and a clock-accurate greeting are both more correct and instant.

    A repeated opener ("你好，你好。") is matched after collapsing the anaphora:
    the splitter hands us clause fragments, not the whole sentence, so the
    anchored patterns alone would miss it and the turn would reach the template.
    """
    t = (text or "").strip()
    # The strip-set removes a trailing 了/啦, so a bare "明白了" collapses to
    # "明白" — match both the raw and the collapsed form rather than enumerating
    # every particle combination in the table.
    variants = {t, _SOCIAL_STRIP_RE.sub("", t)}
    collapsed = _collapse_social(t)
    if collapsed:
        variants.add(collapsed)
    probe = next((v for v in (collapsed, t) if v), t)
    for pat, reply in _CHITCHAT_REPLIES:
        if any(re.search(pat, v) for v in variants if v):
            if probe[:2] in ("你好", "早上", "中午", "下午", "晚上", "早安") or probe[:1] == "早":
                tod = __import__("session").time_of_day()
                if tod == "morning":
                    return "早上好，我在。"
                if tod == "night":
                    return "晚上好，还没睡呀？"
            return reply
    return None


def _clock_answer(text: str) -> str | None:
    """Answer a clock question from the host clock, or None if not a time query.

    The 0.6B brain has no clock and answered "现在几点了" with "我没有时钟…帮你
    把卧室调好了" — an invention plus an action it never took. The time is a
    fact the host knows; recite it, do not generate it.
    """
    import re
    import time
    t = text or ""
    # Date and weekday are the same kind of hard fact as the clock: the host
    # knows them, so recite them. "今天几号" and "今天星期几" matched none of the
    # clock patterns, fell to the brain, and it answered "我看不到时间，你说吧。"
    # and "我看不到世界，你说。" — the first is false (there is a clock), the
    # second is a generation breakdown. Neither is an answer.
    if re.search(r"几号|日期|今天.{0,2}是几|星期几|礼拜几|周几|今天星期", t):
        lt = time.localtime()
        if re.search(r"星期几|礼拜几|周几|星期|礼拜", t):
            # tm_wday: Monday=0. Chinese week runs 一二三四五六日.
            return f"今天星期{_CN_WEEKDAY[lt.tm_wday]}。"
        return f"今天是{_num_zh(lt.tm_mon)}月{_num_zh(lt.tm_mday)}号。"
    # Same vocabulary the old rule layer used for query_time — this gate is
    # checked first and unconditionally, so any form that reaches reply() as a
    # time question is one this function can answer. Missing a form here means
    # the turn falls to the model.
    if not re.search(r"几点了?|几点钟|现在.{0,2}(时间|什么时候)|报时|现在几点|什么时间了?", t):
        return None
    lt = time.localtime()
    # (weekday/date handled above; fall through to the clock branches)
    h, m = lt.tm_hour, lt.tm_min
    hour_zh = {0: "零点", 1: "一点", 2: "两点", 3: "三点", 4: "四点", 5: "五点",
               6: "六点", 7: "七点", 8: "八点", 9: "九点", 10: "十点", 11: "十一点",
               12: "十二点"}.get(h if h <= 12 else h - 12, f"{h}点")
    if m == 0:
        return f"现在{hour_zh}。"
    # Speak the minutes in Chinese like the hour, not "七点20分" — mixing scripts
    # in one phrase reads wrong to the TTS front end.
    if m < 10:
        min_zh = f"零{_CN_DIGIT[m]}"
    else:
        min_zh = _num_zh(m)
    return f"现在{hour_zh}{min_zh}分。"


def _weather_answer(text: str) -> str | None:
    """Honest answer for a weather question, or None.

    There is no weather source here, and the base model filled the gap with
    "今天天气不错，阳光明媚呢！" — a confident invention. Saying it cannot see
    outside is both true and the trained failure phrasing; a wrong forecast is
    worse than no forecast.
    """
    import re
    if not re.search(r"天气|气温|下雨|下雪|外面|屋外|室外|temperature|weather",
                     text or ""):
        return None
    return "我这儿看不到外面的天气，得连上才知道。"


def _sensor_answer(text: str) -> str | None:
    """Honest answer for a temperature/humidity reading, or None.

    The house has no thermometer and no hygrometer, and the 0.6B model fills
    the gap with inventions: "客厅温度多少" became "客厅的温度我没操作，用空调
    调好了。" and "家里湿度多少" became "我不能直接控制设备，看你那台空调。"
    A made-up number is worse than none, so answer it here the same way weather
    is answered. Moves to sensor_mcp when real sensors exist.
    """
    import re
    if not re.search(r"温度|湿度|多少度|几度|摄氏度|冷热", text or ""):
        return None
    if "湿度" in text or "潮" in text:
        return "家里没有湿度计，这个我测不了。"
    return "家里没有温度计，这个我测不了。"


def _query_context(intent: dict) -> str:
    """Build a 当前状态 line for a status question from the device store.

    Returns "" when the question names no recognised device, so the model falls
    back to its trained context-free behaviour rather than being told a wrong
    class. When the device is recognised but its state is unknown, says so
    explicitly — a model told "未知" reports uncertainty; a model told nothing
    invents a state.
    """
    text = intent.get("text", "") or ""
    device = None
    for word, dev in _QUERY_DEVICE_HINT:
        if word in text:
            device = dev
            break
    if device is None:
        return ""
    # The rule layer defers status questions with room="none", so the room must
    # be read from the utterance itself — trusting intent["room"] here made
    # every "客厅灯开着吗" query the whole house.
    room = _find_room_in_text(text)
    cmds = intent.get("commands") or [intent]
    if room == "none" and cmds:
        room = cmds[0].get("room", "none") or "none"
    room_zh = _ROOM_ZH.get(room, "全屋")
    try:
        import device_layer
        state = device_layer.get_controller().query(device, room)
    except Exception as e:  # device layer absent (PC dev) -> unknown
        print(f"device_layer query unavailable: {e}")
        state = None
    what = f"{room_zh}{_DEVICE_ZH.get(device, device)}"
    if state is None:
        # State was never recorded. Say so in a way the small model cannot
        # "helpfully" finish: an explicit instruction not to guess beat the
        # bare "未知" line, which it kept appending an invented state to.
        return (f"当前状态：{what} 没有记录，未知。"
                f"请只回答不知道，不要猜测它的开关状态。")
    return f"当前状态：{what}{'开着' if state else '关着'}"


def _summary_context(cmds: list) -> str:
    """Summarise multi-device / unsupported commands from what really happened.

    Returns "" when nothing in the intent touched hardware (chit-chat, scenes),
    preserving the trained context-free behaviour for those. When it does
    return, the line states per-room success/failure and flags unsupported
    actions, so the reply never claims more than the hardware delivered.
    """
    lines = []
    for cmd in cmds:
        line = _summary_line(cmd)
        if line:
            lines.append(line)
    return "\n".join(lines)


def _summary_line(cmd: dict) -> str | None:
    """One natural-language sentence for what a single command actually did.

    This is the spoken form. The trained model wants the machine shape
    "执行结果：switch_light(客厅, on) 成功", so the two live side by side: this
    returns None when the command has no executed result yet, and is also used
    directly as the reply text when no model turn is available — a refusal on a
    compound ("把客厅灯打开再调暗") otherwise fell back to the raw context line
    and read the user "switch_light(客厅, on) 成功" out loud.
    """
    action = cmd.get("action", "other")
    room = cmd.get("room", "none")
    res = _LAST_DEVICE_RESULT.get((action, room))
    if res is None:
        return None
    if not res.get("supported", True):
        return res.get("detail", "该功能暂不支持")
    done = res.get("executed") or []
    failed = res.get("failed") or []
    dev_zh = _DEVICE_ZH.get(home_config.ACTION_DEVICE.get(action, ("", 0))[0], "")
    # The verb carries the meaning: "客厅灯" alone does not say whether it
    # was switched on or off, and a compound "开客厅灯，关卧室灯" needs both.
    verb = "关" if home_config.ACTION_DEVICE.get(action, ("", 0))[1] is False else "开"
    # A state-only device (AC/fan/TV/curtain) has no per-room binding, so a
    # room="none" record is one appliance, not a whole-house sweep.
    if done == ["none"] or failed == ["none"]:
        if failed:
            return f"{dev_zh}没成，设备没有响应"
        return f"{dev_zh}{verb}好了"
    # A roomless light that reached the store with no room at all: the sweep was
    # suppressed (device_layer._NoSweep, for a bare "开灯" inside a two-command
    # turn). Nothing was named, and _ROOM_ZH["none"] would have called this
    # "全屋" — the one thing it demonstrably is not. Driven, name no room.
    if done == [room] and room in _ALL_ROOMS_ZH:
        if failed:
            return f"{dev_zh}没成，设备没有响应"
        return f"{dev_zh}{verb}好了"
    if failed and done:
        ok_rooms = "、".join(_ROOM_ZH.get(r, r) for r in done)
        bad_rooms = "、".join(_ROOM_ZH.get(r, r) for r in failed)
        return f"{ok_rooms}{dev_zh}{verb}好，{bad_rooms}没成"
    if failed:
        bad_rooms = "、".join(_ROOM_ZH.get(r, r) for r in failed)
        return f"{bad_rooms}{dev_zh}没成，设备没有响应"
    if done:
        ok_rooms = "、".join(_ROOM_ZH.get(r, r) for r in done)
        return f"{ok_rooms}{dev_zh}{verb}好了"
    return None


def speak_results(intent: dict) -> str | None:
    """Spoken form of what this turn actually did (the 执行结果拼装), or None.

    One sentence per command that left a hardware result, joined naturally
    ("客厅灯开好了，卧室灯没成，设备没有响应。"). Nothing executed, nothing to
    say — returns None so the caller falls back to its neutral line instead of
    a canned "收到". This is tts.make_reply's whole vocabulary in B3: the
    per-action template table echoed the request, this reports the outcome.
    """
    cmds = intent.get("commands") or [intent]
    parts = [line for line in (_summary_line(c) for c in cmds) if line]
    if not parts:
        return None
    # Refusal details end in 。 ("…换不了它。"); strip per part so joins never
    # double the mark, then close the sentence once.
    text = "，".join(p.rstrip("。") for p in parts)
    return text + "。"


def build_context(intent: dict) -> str:
    """Build the input-context string from an intent result.

    Training distribution (data/make_pure_voice_v2.py): only single-device
    light/AC actions carry 执行结果 lines (switch_light / set_aircon). Scene
    modes, multi-device sweeps and chitchat are DIRECT speech — the model
    answers them with no context at all. Feeding a scene an 执行结果 line puts
    the model off-distribution and it starts hallucinating device state
    ("我回来了" answered with random lights/AC). So: context only for the four
    trained tool forms; everything else goes context-free.
    Status queries get the 当前状态 line from _query_context instead (state
    read from the device store, never fabricated).

    The 执行结果 line now reflects what the hardware layer actually did
    (device_layer.execute). On success it emits the trained success phrasing;
    on failure it emits the trained failure phrasing so the model says the
    device is offline instead of claiming it acted.
    """
    if _looks_like_query(intent.get("text", ""), intent):
        # Status question: never let the brain invent state. Ask the device
        # store for the truth and hand the model a "当前状态" line instead of
        # nothing — otherwise it answers "已打开" to "灯开着吗". Unknown state
        # (device never touched) becomes an explicit line, so the model says it
        # does not know rather than guessing.
        return _query_context(intent)
    cmds = intent.get("commands") or [intent]
    # Pure chit-chat ("你好呀今天心情不错"): no device was touched this turn, so
    # the brain must get NO context at all. _summary_context reads
    # _LAST_DEVICE_RESULT, which still holds the PREVIOUS turn's result — without
    # this guard, small talk was answered with "好，都关好了，欢迎回家" (the last
    # light command's leftover context). Context-free is the trained shape.
    if all(c.get("action") in ("other",) for c in cmds):
        return ""
    # Single trained tool form: keep the exact phrasing the model was trained on
    # (switch_light / set_aircon), now sourced from the real result.
    if len(cmds) == 1:
        cmd = cmds[0]
        action = cmd.get("action", "other")
        room = cmd.get("room", "none")
        if action in _CONTEXT_ACTIONS:
            res = _LAST_DEVICE_RESULT.get((action, room), {})
            # A sweep that partly failed must not use the single-device success
            # phrasing — fall through to the summary, which names the rooms.
            if not res.get("failed"):
                room_zh = _ROOM_ZH.get(room)
                tmpl = _TOOL_DETAIL.get(action)
                if tmpl and room_zh:
                    detail = tmpl.format(room=room_zh)
                    if res.get("ok", True):
                        return "执行结果：%s 成功" % detail
                    return "执行结果：%s 失败：设备离线，没有响应" % detail
    # Everything else that touched a device: summarise what actually happened so
    # the reply can report a partial sweep or an unsupported action instead of a
    # blanket claim. Built from the executed results only.
    return _summary_context(cmds)


# (action, room) -> full result dict from device_layer, so build_context
# reports the same outcome execute_intent just produced instead of guessing.
# The whole dict (not just ok) is kept so the reply can distinguish a partial
# sweep ("客厅好了,厨房没成") from an unbound action ("这个还没接上").
_LAST_DEVICE_RESULT: dict = {}


def _execute_device(action: str, room: str, no_sweep: bool = False) -> dict:
    """Drive the hardware layer; returns its result dict.

    Kept import-local and exception-safe: if the device layer is missing or
    raises, return a synthetic failure so the reply falls back to the trained
    failure phrasing rather than crashing the whole reply path.

    no_sweep is passed straight through to device_layer: it suppresses the
    roomless-light all-rooms sweep for a command that came out of a turn with
    more than one command in it (see device_layer._NoSweep).
    """
    try:
        import device_layer
        return device_layer.execute(action, room, no_sweep=no_sweep)
    except Exception as e:  # pragma: no cover - board/hardware dependent
        print(f"device_layer unavailable, reporting failure: {e}")
        return {"ok": False, "supported": False, "executed": [], "failed": [room],
                "detail": str(e)}



def build_prompt(question: str, context: str) -> str:
    """Alpaca-style content: instruction + optional input context line(s)."""
    q = (question or "").strip()
    return q + ("\n" + context if context else "")


# The base model needs a persona instruction; the SFT model was trained INTO
# the persona and ignored one. Kept minimal and voice-specific: one or two
# spoken sentences, no emoji, no markdown, no invented facts — the four failure
# modes the untuned model showed (self-reference drift, weather invention,
# emoji, over-long answers).
_SYSTEM = (
    "你是小宸同学，一个中文语音助手，用第一人称回答用户。"
    "闲聊时可以多说几句，自然一点；操作设备时只说一两句口语。"
    "不用表情符号和标点之外的符号，不要说谎编造信息。"
    "你不知道的事情就说不知道。"
    "不要把用户说的话当成自己说的话。"
)


def _chat(content: str, max_tokens: int | None = None) -> str:
    """One greedy chat completion against llama-server; returns spoken text.

    max_tokens=None uses the device-turn budget; chit-chat passes
    CHITCHAT_MAX_NEW_TOKENS for longer, natural replies."""
    body = json.dumps({
        "model": "qwen3-xiaochen",
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": content},
        ],
        "max_tokens": max_tokens or MAX_NEW_TOKENS,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode("utf-8")
    req = urllib.request.Request(
        BRAIN_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data["choices"][0]["message"]["content"] or "").strip()


# Pre-warmed MCP tool pool (servers/home_mcp.py). Lazy: importing voice_brain
# must not spawn subprocesses — only the first tools-loop turn does.
_TOOL_POOL = None


def _tool_pool():
    global _TOOL_POOL
    if _TOOL_POOL is None:
        import mcp_pool
        server = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "servers", "home_mcp.py")
        _TOOL_POOL = mcp_pool.ToolPool(servers=[server], timeout=5.0)
        _TOOL_POOL.start()
    return _TOOL_POOL


def _chat_tools(content: str, max_tokens: int | None = None,
                max_rounds: int = 3) -> str:
    """Tools loop: chat completion that may call MCP tools; returns spoken text.

    This is the B3 model path. The model sees the home tool surface
    (set_device/query_device/run_scene/list_rooms) and decides what to call;
    each result is fed back as a tool message until it answers in plain text.
    Returns "" when max_rounds is exhausted — better silence than a reply that
    pretends a pending tool call finished.
    """
    pool = _tool_pool()
    tools = pool.schemas()
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": content},
    ]
    for _ in range(max_rounds):
        body = json.dumps({
            "model": "qwen3-xiaochen",
            "messages": messages,
            "tools": tools,
            "max_tokens": max_tokens or MAX_NEW_TOKENS,
            "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        }).encode("utf-8")
        req = urllib.request.Request(
            BRAIN_URL, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            return (msg.get("content") or "").strip()
        messages.append({
            "role": "assistant",
            "content": msg.get("content"),
            "tool_calls": calls,
        })
        for call in calls:
            fn = call.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            result = pool.call(fn.get("name", ""), args)
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": result,
            })
    return ""


# name-calls short-circuit to the trained reply bank (gen_vocative in
# make_pure_voice_v2.py). Why not ask the model: the vocative patch has a known
# hallucination tail on some name forms ("在，听我说，最近空调好像离线了…"),
# and a lookup is instant — speed first.
_VOCATIVE_REPLIES = {
    "小宸同学": "在，你说。",
    "小宸": "在。",
    "小宸同学在吗": "在，你说。",
    "小宸在吗": "在的，说吧。",
    "在吗": "在的，说吧。",
    "喂": "在，你说。",
    "你好小宸同学": "你好，有什么事？",
    "小宸同学你好": "你好，有什么事？",
    "你好小宸": "你好，有什么事？",
    "哎小宸": "在，你说。",
    "那个小宸同学": "在，有什么事？",
    "小宸小宸": "在，你说。",
}


def _vocative_reply(text: str) -> str | None:
    """Answer a bare address ("小宸同学") or a greeting aimed at the assistant.

    Exact-table lookup first — the curated lines are better than anything a
    generic rule produces. The fallback exists because the table is
    order-sensitive and the ASR is not: "小宸你好" and "你好小宸" are the same
    utterance to a speaker, but only one is a key, so the reversed form used to
    fall through to the template and answer "好的，收到。". Co-occurrence of the
    name and a greeting word is the real signal; order is not.
    """
    norm = _SPEAKER_STRIP_RE.sub("", text or "")
    hit = _VOCATIVE_REPLIES.get(norm)
    if hit:
        return hit
    has_name = "小宸" in norm or "小陈" in norm  # ASR hears the name as 小陈
    if has_name and len(norm) <= 8:
        if any(w in norm for w in ("你好", "您好", "哈喽", "在吗", "喂")):
            return "在，有什么事？"
        return "在，你说。"
    return None


# Below this confidence a resolved command is guessed rather than known (the
# session follow-up path runs at 0.9, rules at 1.0). Acting on a guess is worse
# than asking: it switches the wrong lamp. So a low-confidence command confirms.
_CONFIRM_BELOW = float(os.environ.get("CONFIRM_BELOW", "0.95"))


def _confirm_reply(intent: dict) -> str | None:
    """Ask before acting on a low-confidence command. Returns text, or None.

    `supported=False` is deliberately NOT a confirm case — an unsupported action
    is refused plainly, not questioned.
    """
    cmds = intent.get("commands") or [intent]
    # A session-resolved command is NOT a guess: _resolve_follow_up only fires
    # when a device AND a room were both just established, and it says so in
    # `source`. Scoring it 0.9 made every "关掉它" ask "你是说关客厅灯吗？" while
    # the harness executed it anyway — the question was pure noise on a command
    # that had already happened. Only a genuinely low-confidence *rule* hit asks.
    low = [c for c in cmds if c.get("confidence", 1.0) < _CONFIRM_BELOW
           and c.get("action") not in ("other",)
           and c.get("source") != "session"]
    if not low:
        return None
    c = low[0]
    dev = _DEVICE_ZH.get(home_config.ACTION_DEVICE.get(c.get("action"), ("", 0))[0], "")
    room = _ROOM_ZH.get(c.get("room", "none"), "全屋")
    act = "关" if "off" in c.get("action", "") else "开"
    return f"你是说{act}{room}{dev}吗？"


# device class -> verb for a spoken confirmation. "客厅灯开啦" beats the base
# model's "好的，我来帮你设置一下卧室的灯光" (which says it is about to act after
# acting, because the untuned model does not read 执行结果 as a report).
_CONFIRM_ZH = {
    ("light", True): "灯开啦", ("light", False): "灯关啦",
    ("ac", True): "空调开了", ("ac", False): "空调关了",
    ("fan", True): "风扇开了", ("fan", False): "风扇关了",
    ("tv", True): "电视开了", ("tv", False): "电视关了",
    # Curtains had no fixed phrasing, so _speak_result returned None and a
    # plainly-understood "拉窗帘" was handed to the 0.6B model, which answered
    # "好，开好了。" — no noun, and a verb that fits a lamp. With an entry here
    # the deterministic layer answers it.
    ("curtain", True): "窗帘开了", ("curtain", False): "窗帘关了",
}


# scene -> spoken confirmation. A scene is a fixed program ("晚安" = lights off,
# AC on), so its acknowledgement is fixed too — the base model answered
# "看电影" with "我还没看呢，你先看吧", reading the mood word as an invitation.
_SCENE_ZH = {
    "scene_sleep": "好，晚安。",
    "scene_home": "欢迎回家。",
    "scene_away": "出门注意安全。",
    "scene_movie": "影院模式好了。",
    "scene_party": "派对模式，玩得开心。",
    "scene_read": "阅读模式好了。",
}


def _speak_scene(cmds: list) -> str | None:
    """Fixed acknowledgement for a scene, or None if this is not one."""
    for c in cmds:
        reply = _SCENE_ZH.get(c.get("action", ""))
        if reply:
            return reply
    return None


def _speak_result(cmds: list) -> str | None:
    """Deterministic spoken confirmation for a plain device command, or None.

    Only 4-8 device forms exist and every one has a fixed natural phrasing, so
    there is nothing for a language model to add and a lot for it to get wrong
    (the untuned base said "我来帮你" after the light was already on). Returns
    None for anything with more than one command, a scene, or a failure — those
    go through the summary or the model.
    """
    moved = [c for c in cmds if c.get("action") not in ("other",)
             and not c.get("action", "").startswith("scene_")]
    if len(moved) != 1:
        return None
    cmd = moved[0]
    action = cmd.get("action", "")
    res = _LAST_DEVICE_RESULT.get((action, cmd.get("room", "none")))
    if not res or not res.get("ok") or res.get("failed"):
        return None
    device, on = home_config.ACTION_DEVICE.get(action, (None, None))
    if device is None:
        return None
    verb = _CONFIRM_ZH.get((device, bool(on)))
    if not verb:
        return None
    room = cmd.get("room", "none")
    room_zh = "" if room in ("none", "all") else _ROOM_ZH.get(room, "")
    if room == "all":
        room_zh = "全屋"
    return f"{room_zh}{verb}。"


def reply(question: str, intent: dict, source_out: dict | None = None) -> str | None:
    """Spoken reply text from the voice brain, or None to trigger fallback.

    Guards against tool-call leakage and empty output.

    source_out, when given, receives {"source": <str>} naming what actually
    produced the text, so the caller stops guessing. Without it every non-empty
    return was labelled "brain" — even the deterministic _speak_result()
    confirmations, which never touch the model (the rule-matched "打开卧室的灯"
    reported reply_source=brain at 0.66s, too fast for a 0.6B generate).
    Values: "rule" (deterministic phrasing / fact lookup / vocative / chitchat)
    and "brain" (the model actually generated it).
    """
    def _tag(src: str, text: str | None) -> str | None:
        if source_out is not None and text is not None:
            source_out["source"] = src
        return text

    _clear_results()
    hit = _vocative_reply(question)
    if hit:
        return _tag("rule", hit)
    # Refuse plainly what the house cannot do, before any hardware attempt.
    cmds_all = intent.get("commands") or [intent]
    unsupported = next((c for c in cmds_all if c.get("supported") is False), None)
    if unsupported is not None:
        reason = unsupported.get("unsupported_reason") or "这个功能还没接上。"
        # A compound turn can hold a supported command AND an unsupported one
        # ("把客厅灯打开再调暗"): the lamp really was switched, then the dim was
        # refused. Returning the refusal alone reported "nothing happened" over
        # a lit lamp — the user hears a lie of omission. Recite what ran first,
        # then the refusal.
        ran = [c for c in cmds_all
               if c.get("supported") is not False
               and c.get("action") not in ("other", None)]
        if ran:
            execute_intent({"commands": ran, "text": intent.get("text", "")})
            context = build_context({"commands": ran, "text": intent.get("text", "")})
            done_line = _speak_summary(context, ran) or _speak_result(ran)
            if done_line:
                return _tag("rule", f"{done_line}{reason}")
        return _tag("rule", reason)
    # Clock/date/weekday are host facts, and the rule layer has no query_date
    # action — "今天几号" arrives as action="other", which is _looks_like_query
    # False, so gating the clock inside the query branch meant the brain answered
    # it ("我看不到时间，你说吧。"). Check it unconditionally: the text is the only
    # thing that decides whether this is a time question. (B3: the clock/weather/
    # sensor trio stay deterministic until clock_mcp / weather_mcp / sensor_mcp
    # land — a host fact recited beats a 0.6B guess. Device status questions are
    # NOT here any more: they go to the model with the query_device tool and the
    # _query_context state line, see build_context.)
    clock = _clock_answer(question)
    if clock:
        return _tag("rule", clock)
    is_query = _looks_like_query(question, intent)
    if is_query:
        # No weather source, no thermometer: the honest refusal IS the answer.
        # These move to weather_mcp / sensor_mcp when real sources exist.
        ans = _weather_answer(question) or _sensor_answer(question)
        if ans:
            return _tag("rule", ans)
    # Plain social openers get a fixed line: the brain's house-command bias
    # turned "你好呀今天心情不错" into "好，都关好了，欢迎回家". Independent of the
    # query branch — a greeting is not a failed query, and the earlier `elif`
    # made it one.
    chitchat = _chitchat_answer(question)
    if chitchat:
        return _tag("rule", chitchat)
    # Ask before acting on a guess (session-resolved follow-ups).
    confirm = _confirm_reply(intent)
    if confirm:
        return _tag("rule", confirm)
    # Drive the hardware BEFORE building context so the spoken reply reflects
    # what actually happened. Runs for every command (all-rooms, multi-command)
    # regardless of whether that command yields an 执行结果 context line.
    execute_intent(intent)
    context = build_context(intent)
    # A multi-device command must report EVERY device it moved. The 0.6B model
    # was trained on short single-clause replies and drops the second half of
    # "打开客厅灯，关掉卧室灯" — it said the living room light was on and never
    # mentioned the bedroom. The summary already lists both lines correctly, so
    # for multi-command turns speak it straight instead of trusting the model to
    # recite a list it was never trained on.
    cmds = intent.get("commands") or [intent]
    moved = [c for c in cmds if c.get("action") not in ("other",)]
    if len(moved) > 1 and context:
        return _tag("rule", _speak_summary(context, cmds))
    # Single device command: a fixed, correct confirmation instead of asking the
    # base model to narrate a report it reads as a request.
    result_line = _speak_result(cmds) or _speak_scene(cmds)
    if result_line:
        return _tag("rule", result_line)
    content = build_prompt(question, context)
    # A turn with no device command is chat: give it the longer budget so the
    # answer is not clipped mid-sentence. Device turns keep the short budget —
    # they are confirmations, and speed matters more than colour there.
    is_chat = all(c.get("action") == "other" for c in cmds)
    budget = CHITCHAT_MAX_NEW_TOKENS if is_chat else None
    # B3: a deferred turn (no device command survived the micro-rules) gets the
    # tools loop — the model answers chit-chat directly and calls home tools for
    # everything else ("关掉它", "客厅什么灯亮着", capabilities rules never cover).
    if is_chat:
        text = _chat_tools(content, max_tokens=budget)
    else:
        text = _chat(content, max_tokens=budget)
    if not text or "tool_call" in text:
        return None
    text = _scrub_english(text)
    # Drop a false-promise reply so the caller uses the template instead: "打开
    # 又关掉" got "我正在尝试操作，请稍等" — the pipeline acts instantly, so
    # promising to act later is always a lie.
    if _is_false_promise(text) or _claims_unmade_action(text, intent):
        return None
    return _tag("brain", text)


def _speak_summary(context: str, cmds: list | None = None) -> str:
    """Turn a turn's execution results into one spoken sentence, or "".

    Preferred input is the context built for the model: the trained
    "执行结果：..." lines, joined into a single natural clause so a two-device
    turn is never truncated to one. When none of them parse — the compound
    refusal path builds context for a run that produced no trained-shape line —
    fall back to the per-command natural sentence. Returning "" there left the
    caller to speak the raw context line, and the user heard
    "switch_light(客厅, on) 成功" instead of "客厅灯开啦".
    """
    import re
    parts = []
    # Only the SPOKEN summary lines carry this exact prefix. The single-device
    # context line ("执行结果：switch_light(客厅, on) 成功") shares the 执行结果：
    # head but is a machine report, and matching it here read the raw tool call
    # out loud on any turn that produced it — the compound refusal said
    # "switch_light(客厅, on) 成功" instead of "客厅灯开啦". Go straight to the
    # natural phrasing whenever the tool line is present.
    if "执行结果：" in context:
        for line in context.splitlines():
            m = re.match(r"^执行结果：(.*)(?:好了|好，|没成.*)$", line.strip())
            if not m:
                continue
            body = m.group(0)[len("执行结果："):].strip()
            if body:
                parts.append(body)
    if not parts and cmds:
        for cmd in cmds:
            line = _summary_line(cmd)
            if line:
                parts.append(line)
    if not parts:
        return ""
    return "，".join(parts) + "。"


def _expand_scenes(cmds: list) -> list:
    """Replace each scene command with the device actions it stands for.

    A scene ("晚安") is a name for a bundle of device commands; running it is
    the whole point of recognising it. Expansion uses the session's current room
    so "晚安" in the bedroom warms the bedroom, not the living room. Non-scene
    commands pass through untouched, and an unknown scene expands to nothing
    (it is then simply narrated, as before).
    """
    import session
    out = []
    for cmd in cmds:
        action = cmd.get("action", "")
        if not action.startswith("scene_"):
            out.append(cmd)
            continue
        try:
            import scenes
            expanded = scenes.expand(action, cmd.get("room", "none"),
                                     session.last_room())
        except Exception as e:
            print(f"scene expansion failed for {action}: {e}")
            expanded = []
        # Carry the annotation down so an unsupported scene action (e.g. dim)
        # is still refused rather than silently attempted.
        for c in expanded:
            c["supported"] = cmd.get("supported", c.get("supported"))
        out.extend(expanded or [cmd])
    return out


def _clear_results() -> None:
    """Drop last turn's execution results before a new turn runs.

    _LAST_DEVICE_RESULT is keyed (action, room) and only overwritten when the
    SAME action runs again, so a stale entry from three turns ago can leak into
    this turn's context. Clearing per turn keeps build_context to this turn's
    work only.
    """
    _LAST_DEVICE_RESULT.clear()


def execute_intent(intent: dict) -> list:
    """Carry out every command in the intent; returns per-command result dicts.

    Execution happens for every action that maps to a device (light/ac/fan/tv),
    not just the four the model was trained to narrate — otherwise "打开电视"
    was recognised, then silently dropped. Results are stashed per (action, room)
    so build_context reports exactly what the hardware did. Idempotent.
    """
    import home_config
    cmds = _expand_scenes(intent.get("commands") or [intent])
    results = []
    known = set(home_config.ACTION_DEVICE) | set(home_config.ACTION_REQUIREMENT)
    # A roomless light command inside a multi-command turn must not be read as
    # "the whole house": "先开灯再关空调" answers with a three-room sweep the user
    # never asked for. A single-command turn is left alone — then the sweep is
    # exactly what "开灯" means.
    no_sweep = len(cmds) > 1
    for cmd in cmds:
        action = cmd.get("action")
        # Annotated-unsupported commands are refused upstream, never executed.
        if cmd.get("supported") is False:
            _LAST_DEVICE_RESULT[(action, cmd.get("room", "none"))] = {
                "ok": True, "supported": False, "executed": [], "failed": [],
                "detail": cmd.get("unsupported_reason", "该功能暂不支持"),
            }
            continue
        # Execute anything device-shaped: bound on/off actions drive hardware,
        # unbound ones (dim/colour/speed/media) still get a result so the reply
        # can say "暂不支持" instead of going silent.
        if action not in known:
            continue
        room = cmd.get("room", "none")
        result = _execute_device(action, room, no_sweep=no_sweep)
        _LAST_DEVICE_RESULT[(action, room)] = result
        results.append(result)
    return results


def healthy() -> bool:
    """True if llama-server answers a ping."""
    try:
        req = urllib.request.Request(BRAIN_URL.rsplit("/v1/", 1)[0] + "/health")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False
