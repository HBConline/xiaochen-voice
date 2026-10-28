# -*- coding: utf-8 -*-
"""Dialogue view model + frame rendering for a 240x240 ST7789 voice display.

Pure logic + Pillow only — no hardware here, so it runs on the PC (tests,
design previews) and on the board unchanged. The hardware driver lives in
st7789_display.py and only converts render_frame() output to SPI bytes.

Layout (user spec):
  top bar   = clock + status dot/word
  middle    = latest one Q&A pair (一问一答)
  remainder = device status rows

Data sources (wakeword_server):
  /mic_status  state: off | waiting_wake | listening | processing
               events: newest-first {kind: wake|turn|error, said, reply, ...}
               reply is a str (node path) or a dict {text,...} (mic path)
  /state       groups: [{device_zh, on, scope: house|mixed, exceptions:[...]}]
"""
import os
import time

from PIL import Image, ImageDraw, ImageFont

W, H = 240, 240
TOP_H = 28
DIVIDER_Y = 116           # dialogue zone = TOP_H .. DIVIDER_Y (max 4 rows)
DEV_DOT_X = 14
DEV_ROW0_Y = DIVIDER_Y + 28
DEV_ROW_STEP = 20
DEV_TEXT_X = 26
DOT_CX, DOT_CY = 14, 14
DOT_R = 5
BG = (12, 14, 18)
BAR_BG = (20, 24, 30)
DIVIDER = (40, 46, 52)
TEXT = (220, 224, 228)
DIM = (130, 136, 142)
DEV_ON = (39, 174, 96)
DEV_OFF = (100, 106, 112)

# Status palette: user-approved panel colors (green/amber/red family).
_STATES = {
    'off':          {'label': '离线',   'color': (128, 128, 128), 'blink': False},
    'waiting_wake': {'label': '待机',   'color': (58, 107, 80),   'blink': False},
    'listening':    {'label': '聆听中', 'color': (39, 174, 96),   'blink': True},
    'processing':   {'label': '思考中', 'color': (245, 166, 35),  'blink': True},
    'speaking':     {'label': '说话中', 'color': (52, 152, 219),  'blink': True},
    'error':        {'label': '出错',   'color': (224, 32, 32),   'blink': True},
}
ROLE_COLOR = {'you': TEXT, 'bot': (126, 214, 175), 'err': (224, 100, 100)}
ROLE_PREFIX = {'you': '你: ', 'bot': '宸: ', 'err': '!: '}
BLINK_PERIOD = 0.5  # seconds, matches the panel-dot spec

FONT_CANDIDATES = [
    r'C:\Windows\Fonts\msyh.ttc',
    r'C:\Windows\Fonts\simhei.ttf',
    r'C:\Windows\Fonts\Deng.ttf',
    '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
    '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
]


def state_meta(state):
    """Status word + dot color + blink flag for one pipeline state."""
    return _STATES.get(state, _STATES['waiting_wake'])


def format_clock(ts):
    """HH:MM:SS wall clock for the top bar."""
    return time.strftime('%H:%M:%S', time.localtime(ts))


def text_units(text):
    """Display width in font-size units: CJK = 1.0, anything else = 0.5."""
    total = 0.0
    for ch in text:
        total += 1.0 if ord(ch) > 0x2E7F else 0.5
    return total


def _hard_break(text, limit):
    """Break one un-splittable token into chunks of at most `limit` units."""
    out, cur, cur_u = [], '', 0.0
    for ch in text:
        u = text_units(ch)
        if cur and cur_u + u > limit:
            out.append(cur)
            cur, cur_u = ch, u
        else:
            cur += ch
            cur_u += u
    if cur:
        out.append(cur)
    return out


def wrap_text(text, limit):
    """Word-wrap on spaces; hard-break tokens wider than `limit` units.

    Space-separated words stay whole when they fit; CJK runs fall through to
    _hard_break. Returns [] for empty text.
    """
    if not text:
        return []
    lines, cur = [], ''
    for word in text.split(' '):
        if not word:
            continue
        wu = text_units(word)
        if wu > limit:
            if cur:
                lines.append(cur)
                cur = ''
            lines.extend(_hard_break(word, limit))
            continue
        cand = word if not cur else cur + ' ' + word
        if text_units(cand) <= limit:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _reply_text(reply):
    """Node path sends reply as str, mic loop sends {'text': ...}."""
    if isinstance(reply, dict):
        return (reply.get('text') or '').strip()
    return (reply or '').strip()


def build_transcript(events, max_turns=1):
    """mic_status events (newest-first) -> chronological (role, text) pairs.

    role: you | bot | err. Wake markers are display noise and get dropped.
    Default keeps ONE latest Q&A pair — the screen shows 一问一答 only.
    """
    pairs = []
    for ev in reversed(list(events or [])):
        kind = ev.get('kind')
        said = (ev.get('said') or '').strip()
        if kind == 'wake':
            continue
        if kind == 'turn':
            reply = _reply_text(ev.get('reply'))
            if said:
                pairs.append(('you', said))
            if reply:
                pairs.append(('bot', reply))
        elif kind == 'error':
            if said:
                pairs.append(('you', said))
            pairs.append(('err', '（出错）'))
    if max_turns and len(pairs) > max_turns * 2:
        pairs = pairs[-max_turns * 2:]
    return pairs


def device_row_text(group):
    """One /state group -> '客厅灯 开' / '空调 关（卧室:开）'."""
    name = group.get('device_zh') or group.get('device') or '?'
    state = '开' if group.get('on') else '关'
    exceptions = group.get('exceptions') or []
    if group.get('scope') == 'mixed' and exceptions:
        parts = ['%s:%s' % (e.get('room_zh') or e.get('room') or '?',
                            '开' if e.get('on') else '关')
                 for e in exceptions[:3]]
        return '%s %s（%s）' % (name, state, '、'.join(parts))
    return '%s %s' % (name, state)


def resolve_font():
    """First CJK-capable font file that exists, else None."""
    for path in FONT_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def _dot_color(meta, now):
    if not meta['blink']:
        return meta['color']
    on = int(now / BLINK_PERIOD) % 2 == 0
    if on:
        return meta['color']
    return tuple(int(c * 0.35) for c in meta['color'])


def _body_rows(pairs, limit_units):
    """(role, line) rows: prefix on the first line of each utterance."""
    rows = []
    for role, text in pairs:
        wrapped = wrap_text(text, max(limit_units - text_units(ROLE_PREFIX[role]), 1))
        if not wrapped:
            wrapped = ['']
        for i, line in enumerate(wrapped):
            rows.append((role, (ROLE_PREFIX[role] + line) if i == 0 else '  ' + line))
    return rows


def _draw_devices(draw, font, devices, now):
    """Device status block: header + one dotted row per /state group."""
    draw.text((10, DIVIDER_Y + 6), '设备状态', font=font, fill=DIM)
    for i, group in enumerate(devices or []):
        y = DEV_ROW0_Y + i * DEV_ROW_STEP
        if y + 14 > H:
            break
        color = DEV_ON if group.get('on') else DEV_OFF
        draw.ellipse((DEV_DOT_X - 4, y, DEV_DOT_X + 4, y + 8), fill=color)
        text = device_row_text(group)
        for j, line in enumerate(wrap_text(text, (W - DEV_TEXT_X - 10) / 14.0)[:1]):
            draw.text((DEV_TEXT_X, y - 3), line, font=font, fill=TEXT)


def render_frame(state, events, devices=None, now=None, font_path=None):
    """Compose one 240x240 RGB frame per the layout contract."""
    now = time.time() if now is None else now
    font_path = font_path or resolve_font()
    font = ImageFont.truetype(font_path, 14) if font_path else ImageFont.load_default()

    img = Image.new('RGB', (W, H), BG)
    draw = ImageDraw.Draw(img)
    meta = state_meta(state)

    # top bar: status dot + clock + status word
    draw.rectangle((0, 0, W, TOP_H), fill=BAR_BG)
    draw.ellipse((DOT_CX - DOT_R, DOT_CY - DOT_R,
                  DOT_CX + DOT_R, DOT_CY + DOT_R), fill=_dot_color(meta, now))
    draw.text((26, 5), format_clock(now), font=font, fill=TEXT)
    label = meta['label']
    lw = draw.textlength(label, font=font)
    draw.text((W - 10 - lw, 5), label, font=font, fill=meta['color'])

    # dialogue zone: latest Q&A, newest sitting just above the divider
    limit_units = (W - 20) / 14.0
    rows = _body_rows(build_transcript(events), limit_units)
    y = DIVIDER_Y - 8 - 18
    for role, line in reversed(rows):
        if y < TOP_H + 2:
            break
        draw.text((10, y), line, font=font, fill=ROLE_COLOR[role])
        y -= 18

    draw.line((0, DIVIDER_Y, W, DIVIDER_Y), fill=DIVIDER)
    _draw_devices(draw, font, devices, now)
    return img
