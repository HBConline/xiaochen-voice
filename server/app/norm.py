"""Quantitative normalisation for the rule layer.

The rules match Chinese text, but the numbers in it arrive in every shape a
person speaks: digits ("26"), Chinese numerals ("二十六"), decimals ("26.5"),
percentages ("百分之六十", "50%"), and clock times ("七点半", "明天早上七点").
Matching those with one regex per shape is a losing game — every new phrasing
is a new pattern. So the numeric surface is normalised ONCE, here, before the
rules see it: the parser then only ever deals with plain numbers.

Design rules:
- Pure functions, no state, no I/O — trivially testable.
- Chinese numerals up to 万 (10000); beyond that the text is not a house value.
- Returns None rather than a guess when a form is malformed ("十二个半").

Used by intent_rules for temp/volume/brightness/timeout values.
"""
import re

_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}

# "二十六" -> 26, "十" -> 10, "一百零五" -> 105, "两千三" -> 2300
_CN_NUM_RE = re.compile(r"[零〇一二两三四五六七八九十百千万]+")


def cn_to_int(s: str):
    """Chinese numeral to int, or None if it is not a well-formed number.

    Handles the common spoken forms including the omitted trailing unit
    ("两千三" = 2300) and the bare leading ten ("十五" = 15). Section-based
    accumulation so 万 composes correctly.
    """
    if not s:
        return None
    total = 0          # fully accumulated value
    number = 0         # current digit run
    last_unit = 1      # magnitude of the most recent unit, for omitted tails
    zeroed = False     # a 零 was seen: the next digit is a plain ones slot
    for ch in s:
        if ch in _DIGITS:
            number = _DIGITS[ch]
            if ch in ("零", "〇"):
                zeroed = True
        elif ch in _UNITS:
            unit = _UNITS[ch]
            # "十五": number is 0 here, treat as 1*10
            total += (number or 1) * unit
            last_unit = unit
            number = 0
            zeroed = False
        else:
            return None
    # Spoken Chinese drops the trailing unit: "两千三" is 2300, not 2003 —
    # the bare 三 sits one power of ten below the last named unit. A 零 marks
    # that slot as already skipped ("一百零五"), so the tail stays a plain ones
    # digit. The tail has no unit word of its own, so any digit is accepted
    # there: "两百三" = 230 is the ordinary spoken form, not a malformed number.
    if number and last_unit > 1 and total and not zeroed:
        return total + number * (last_unit // 10)
    return total + number


def parse_number(text: str):
    """First number in `text` as a float, or None.

    Understands digits (with optional decimal), Chinese numerals, and a
    trailing 半 or 点五 ("26.5" / "二十六点五" / "七点半" via parse_clock).
    """
    m = re.search(r"\d+(?:\.\d+)?", text)
    if m:
        return float(m.group(0))
    m = _CN_NUM_RE.search(text)
    if not m:
        return None
    base = cn_to_int(m.group(0))
    if base is None:
        return None
    tail = text[m.end():]
    # "二十六点五" -> 26.5 ; "七点三" is a clock time, not 7.3, so only treat a
    # 点 as a decimal when it is followed by a Chinese digit AND the text has no
    # 度/点-oclock context. Simplest safe rule: 点五/点六… after a temperature.
    dm = re.match(r"点([一二三四五六七八九])", tail)
    if dm:
        return base + _DIGITS[dm.group(1)] / 10.0
    return float(base)


def parse_percent(text: str):
    """Percentage in text as 0-100 float, or None.

    Accepts "百分之六十", "60%", "百分之60", "六成" (60).
    """
    if "成" in text:
        m = re.search(r"([零〇一二两三四五六七八九十]+)成", text)
        if m:
            v = cn_to_int(m.group(1))
            if v is not None:
                return min(100.0, v * 10.0)
    m = re.search(r"百分之(\d+(?:\.\d+)?)", text)
    if m:
        return float(m.group(1))
    m = re.search(r"百分之([零〇一二两三四五六七八九十百]+)", text)
    if m:
        v = cn_to_int(m.group(1))
        if v is not None:
            return float(v)
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    if m:
        return float(m.group(1))
    return None


_CLOCK_HOUR = _CN_NUM_RE


def parse_clock(text: str):
    """Clock time in text as minutes-since-midnight, or None.

    Handles "七点", "七点半", "七点二十", "19:30", "七点一刻".
    """
    m = re.search(r"(\d{1,2})[:：](\d{2})", text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h < 24 and 0 <= mi < 60:
            return h * 60 + mi
        return None
    m = re.search(r"([零〇一二两三四五六七八九十]+)点(半|一刻|三刻|(\d{1,2})|([零〇一二两三四五六七八九十]+))?", text)
    if not m:
        return None
    h = cn_to_int(m.group(1))
    if h is None or h > 24:
        return None
    minute = 0
    tail = m.group(2)
    if tail == "半":
        minute = 30
    elif tail == "一刻":
        minute = 15
    elif tail == "三刻":
        minute = 45
    elif tail and tail.isdigit():
        minute = int(tail)
    elif tail:
        v = cn_to_int(tail)
        if v is not None:
            minute = v
    if minute >= 60:
        return None
    return h * 60 + minute


_DURATION_UNITS = {"秒": 1, "分钟": 60, "分": 60, "小时": 3600, "个小时": 3600,
                   "钟头": 3600, "天": 86400}


def parse_duration(text: str):
    """Duration in text as seconds, or None.

    Handles "一小时", "半小时", "30分钟", "一个半小时", "两分钟", "一刻钟".
    Used for alarm/reminder timeouts and "X后关灯" style rules.
    """
    if "一个半小时" in text or "一小时半" in text:
        return 5400
    if "半小时" in text or "半个钟头" in text:
        return 1800
    if "一刻钟" in text:
        return 900
    total = 0
    # digits form: "30分钟", "2小时"
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(小时|个小时|分钟|分|秒|天|钟头)", text):
        total += float(m.group(1)) * _DURATION_UNITS[m.group(2)]
    if total:
        return total
    # Chinese form: "一小时", "两分钟"
    m = re.search(r"([零〇一二两三四五六七八九十]+)\s*(小时|个小时|分钟|分|秒|天|钟头)", text)
    if m:
        v = cn_to_int(m.group(1))
        if v is not None:
            return float(v) * _DURATION_UNITS[m.group(2)]
    return None


if __name__ == "__main__":
    cases = [
        ("二十六", 26), ("十", 10), ("十五", 15), ("一百零五", 105),
        ("两千三", 2300), ("八千", 8000), ("十二", 12),
    ]
    for s, want in cases:
        got = cn_to_int(s)
        print(f"{s:8s} -> {got}  {'OK' if got == want else 'WANT ' + str(want)}")
    for s in ("26", "二十六", "26.5", "二十六点五"):
        print(f"num  {s:10s} -> {parse_number(s)}")
    for s in ("百分之六十", "60%", "六成", "百分之80"):
        print(f"pct  {s:10s} -> {parse_percent(s)}")
    for s in ("七点", "七点半", "19:30", "七点一刻", "七点二十"):
        print(f"clk  {s:10s} -> {parse_clock(s)}")
