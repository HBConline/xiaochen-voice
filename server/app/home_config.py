"""Single source of truth for the home's topology and device capabilities.

Before this module, four files each carried their own copy of "what rooms
exist" and "what a room is called": the rule layer's _ROOM_WORDS, device_layer's
GPIO_LED_PINS and _ACTION_DEVICE, and voice_brain._ROOM_ZH. They drifted — a
status question could not find its room because the rule layer's vocabulary and
the brain's room table disagreed. Everything room- or device-shaped now lives
here, and the other modules import it. There is exactly one place to add a room
or teach the house a new kind of device.

Nothing in here imports the other app modules, so it can be imported first
without cycles.
"""

# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------
# Canonical room labels. Order is cosmetic (web panel ordering); the dicts below
# are the real contract.
ROOMS = (
    "living_room", "bedroom", "kitchen", "bathroom", "study",
    "balcony", "dining_room", "kids_room",
)

# label -> Chinese name. "all"/"none" are pseudo-rooms (whole house / current).
ROOM_ZH = {
    "living_room": "客厅", "bedroom": "卧室", "kitchen": "厨房",
    "bathroom": "卫生间", "study": "书房", "balcony": "阳台",
    "dining_room": "餐厅", "kids_room": "儿童房",
    "all": "全屋", "none": "全屋",
}

# label -> Chinese nouns a user might say. Longest match wins downstream, so
# "儿童房" must be probed before any bare "房".
ROOM_WORDS = {
    "客厅": "living_room", "起居室": "living_room",
    "卧室": "bedroom", "主卧": "bedroom", "次卧": "bedroom",
    "厨房": "kitchen",
    "卫生间": "bathroom", "浴室": "bathroom", "厕所": "bathroom",
    "书房": "study",
    "阳台": "balcony",
    "餐厅": "dining_room", "饭厅": "dining_room",
    "儿童房": "kids_room", "宝宝房": "kids_room",
    "所有房间": "all", "所有灯": "all", "全部": "all", "全屋": "all",
    "所有": "all", "每个房间": "all", "家里": "all", "整屋": "all",
}

# Rooms with a physical GPIO LED on the Orange Pi 40-pin header. Pin choice:
# plain GPIO, no default mux function.
#   living_room Pin 11 = wPi 5 ; bedroom Pin 13 = wPi 7 ; kitchen Pin 15 = wPi 8
GPIO_LED_PINS = {
    "living_room": 5,
    "bedroom": 7,
    "kitchen": 8,
}

# Rooms that mean "every one of them".
ALL_ROOMS = ("all", "none")

# Pseudo-rooms that expand to every real (hardware-bound) room.
def expand_rooms(room: str) -> list:
    if room in ALL_ROOMS:
        return list(GPIO_LED_PINS.keys())
    return [room]


# ---------------------------------------------------------------------------
# Devices and capabilities
# ---------------------------------------------------------------------------
# device class -> capabilities it actually supports. This is the boundary the
# intent layer checks against: an action whose requirement is missing is
# declined with a spoken reason instead of being silently dropped.
DEVICE_CAPS = {
    "light": {"on_off": True, "brightness": False, "color": False},
    "ac":    {"on_off": True, "temperature": False},
    "fan":   {"on_off": True, "speed": False},
    "tv":    {"on_off": True},
    "curtain": {"on_off": True},
}

# Device class -> which rooms physically have it. The LEDs are per-room lights;
# AC/fan/TV are recorded by intent but have no GPIO yet (state-only), so they
# are listed for every room — extend when real relays land.
DEVICE_ROOMS = {
    "light": tuple(GPIO_LED_PINS.keys()),
    "ac": ROOMS,
    "fan": ROOMS,
    "tv": ROOMS,
    "curtain": ROOMS,
}

# action -> (device class, target on/off). Mutating actions only.
ACTION_DEVICE = {
    "light_on": ("light", True),  "light_off": ("light", False),
    "ac_on": ("ac", True),        "ac_off": ("ac", False),
    "fan_on": ("fan", True),      "fan_off": ("fan", False),
    "tv_on": ("tv", True),        "tv_off": ("tv", False),
    # Curtains are recorded by intent (state-only, no relay yet) exactly like
    # ac/fan/tv. Leaving them out of this table did not make them "unknown" —
    # it made them fall through to ACTION_REQUIREMENT, which has no entry, so a
    # plainly understood "拉上窗帘" was refused with "这个功能还没有接上".
    "curtain_open": ("curtain", True), "curtain_close": ("curtain", False),
}

# query_* action -> device class, for status questions.
QUERY_DEVICE = {
    "query_light": "light",
    "query_ac": "ac",
    "query_fan": "fan",
    "query_tv": "tv",
    # Curtains are state-only like ac/fan/tv, but they were absent from this
    # table, so a curtain status question was not a query at all: it fell to
    # action="other", which by design hands the turn to the brain. The brain
    # invented the state and contradicted itself between phrasings.
    "query_curtain": "curtain",
}

# device class -> Chinese noun, for spoken replies and status lines.
DEVICE_ZH = {
    "light": "灯", "ac": "空调", "fan": "风扇", "tv": "电视",
    "speaker": "音响", "curtain": "窗帘",
}

# device noun in an utterance -> device class, for status questions. Order
# matters: specific nouns are checked before the generic 灯 marker.
QUERY_DEVICE_HINT = (
    ("空调", "ac"), ("风扇", "fan"), ("电扇", "fan"),
    ("电视", "tv"), ("窗帘", "curtain"), ("帘子", "curtain"),
    ("灯", "light"),
)

# action -> capability it needs (beyond on/off). Missing capability => the
# action is "recognised but unsupported" and must be answered honestly.
ACTION_REQUIREMENT = {
    "light_dim": ("light", "brightness"),
    "light_color": ("light", "color"),
    "fan_speed": ("fan", "speed"),
    "temp_set": ("ac", "temperature"),
    "temp_up": ("ac", "temperature"),
    "temp_down": ("ac", "temperature"),
    "volume_up": ("speaker", "volume"),
    "volume_down": ("speaker", "volume"),
    "volume_set": ("speaker", "volume"),
    "music_play": ("speaker", "media"),
    "music_stop": ("speaker", "media"),
    "music_next": ("speaker", "media"),
    "music_prev": ("speaker", "media"),
}

# Devices that exist only as voice/IoT targets with no GPIO binding.
DEVICE_CAPS.setdefault("speaker", {"volume": False, "media": False})

# Household appliances that are NOT wired into this house. The rules match none
# of them, so "打开太阳能板" fell through to the language model, which cannot
# know what the house contains and improvised ("太阳能板？我还没打开呢") — a
# claim about a device that does not exist. Naming the known-absent set lets the
# reply refuse honestly instead. This is a list of things a person plausibly
# asks for; anything not here still defers to the model.
ABSENT_DEVICES = (
    "太阳能板", "太阳能", "光伏", "洗碗机", "洗衣机", "烘干机", "冰箱",
    "微波炉", "烤箱", "电磁炉", "热水器", "饮水机", "扫地机", "扫地机器人",
    "投影仪", "投影", "加湿器", "除湿机", "净化器", "空气净化器", "暖气片",
    "地暖", "烤箱", "消毒柜", "抽油烟机", "油烟机", "燃气灶", "浴霸",
    "智能马桶", "鱼缸", "宠物喂食器", "门铃", "监控", "摄像头",
)

# Verbs that turn a noun into a device request. Used together with
# ABSENT_DEVICES: a verb + a known-absent appliance is a request we must refuse
# rather than hand to a model that will pretend to carry it out.
_DEVICE_VERBS = (
    "打开", "开", "关闭", "关掉", "关", "启动", "开启", "启用", "打开一下",
)


def find_absent_device(text: str):
    """The known-absent appliance named in `text`, or None.

    Only fires when the utterance also carries a device verb, so a bare mention
    ("太阳能板多少钱") is not mistaken for a command to switch one on. The
    longest name wins, so 扫地机器人 beats 扫地机.
    """
    if not text:
        return None
    if not any(v in text for v in _DEVICE_VERBS):
        return None
    hit = None
    for name in ABSENT_DEVICES:
        if name in text and (hit is None or len(name) > len(hit)):
            hit = name
    return hit


def find_room(text: str):
    """Longest room keyword present in `text`, or None. Longest wins so
    儿童房 beats a bare overlap and 所有房间 beats 所有."""
    best = None
    for word, label in ROOM_WORDS.items():
        if word in (text or "") and (best is None or len(word) > len(best[0])):
            best = (word, label)
    return best[1] if best else None


def is_supported(action: str):
    """(supported: bool, reason: str|None) for a mutating/action command.

    Supported means either it is a plain on/off device action (always supported
    where the device is bound) or its required capability is present. Anything
    else returns a human reason the reply layer can speak.
    """
    if action in ACTION_DEVICE:
        return True, None
    # A named appliance the house does not contain: recognised, refused by name.
    if action == "absent_device":
        return False, None      # reason is built per-device in the reply layer
    # A negated command ("不要开灯"). Nothing is driven and nothing is refused on
    # capability grounds — the refusal is semantic, so it always stands and the
    # reason is phrased per-device in the reply layer.
    if action == "negated":
        return False, None
    # Scenes are supported by definition: they expand into device actions
    # (scenes.py) that are themselves validated as they run. Refusing them here
    # would block the whole bundle before a single light was switched.
    if action.startswith("scene_"):
        return True, None
    # Queries read state, they never drive hardware — always answerable (the
    # reply says "不知道" when the state is unknown). Tagging them unsupported
    # made "客厅灯开着吗" answer with the refusal line instead of the state.
    if action in QUERY_DEVICE or action.startswith("query_"):
        return True, None
    req = ACTION_REQUIREMENT.get(action)
    if req is None:
        return False, "这个功能还没有接上"
    device, cap = req
    caps = DEVICE_CAPS.get(device, {})
    if caps.get(cap):
        return True, None
    return False, f"{DEVICE_ZH.get(device, device)}还不支持这个功能"
