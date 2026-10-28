"""Scene semantics: turn a mood utterance ("晚安", "我回来了") into real actions.

Before this module a scene was a label the brain narrated ("开启睡眠模式") and
nothing else happened — the lights stayed on. A scene is only worth recognising
if it does something, so each scene here expands into the concrete device
actions it means, and the normal execute path drives them.

This is the top half of the old _SCENE_DEFAULT_ROOM table in gen_dataset.py,
made executable. Rooms are the scene's *default* target; the session's current
room can override where that makes sense (e.g. "晚安" while already in the
bedroom).

Time of day comes from session.time_of_day so the same phrase can mean
different things at night and in the morning.
"""
from home_config import expand_rooms

# scene -> (default room, [(action, room_template), ...])
# room_template "self" means the scene's own room; "all" is the whole house.
SCENE_ACTIONS = {
    "scene_sleep": ("bedroom", [
        ("light_off", "all"),
        ("tv_off", "none"),
        ("ac_on", "self"),
    ]),
    "scene_home": ("living_room", [
        ("light_on", "living_room"),
        ("ac_on", "living_room"),
    ]),
    "scene_away": ("all", [
        ("light_off", "all"),
        ("ac_off", "none"),
        ("tv_off", "none"),
    ]),
    "scene_movie": ("living_room", [
        ("light_dim", "living_room"),
        ("tv_on", "living_room"),
    ]),
    "scene_party": ("living_room", [
        ("light_on", "all"),
    ]),
    "scene_read": ("study", [
        ("light_on", "self"),
    ]),
}

# Scenes whose default room may yield to the room the user last *commanded*.
# Deliberately EMPTY: session.last_room records the last room mentioned in
# conversation, not where the user is. "打开厨房灯" then "晚安" put the user in
# the kitchen by this rule and warmed the kitchen AC — wrong, and invisible
# until a status query contradicted it. A scene room comes from the utterance
# itself, or from the scene's own default. (A real presence signal, not a
# chat-side effect, is the only thing that should ever move this.)
_ROOM_FROM_SESSION: set = set()


def expand(scene: str, room: str = "none", current_room: str | None = None):
    """Expand a scene into a list of command dicts, or [] if unknown.

    `room` is what the utterance named (often "none" for a bare "晚安");
    `current_room` is the session's last real room, used for scenes that should
    follow the user. Returns command dicts shaped like microrules hits so
    the caller can execute them with no special case.
    """
    spec = SCENE_ACTIONS.get(scene)
    if spec is None:
        return []
    default_room, actions = spec
    base = room if room and room not in ("none", "all") else default_room
    if scene in _ROOM_FROM_SESSION and current_room and current_room != "none":
        base = current_room

    cmds = []
    for action, tmpl in actions:
        target = base if tmpl == "self" else tmpl
        cmds.append({
            "action": action,
            "room": target,
            "urgency": 0.0,
            "urgency_level": 0,
            "text": scene,
            "confidence": 1.0,
            "source": "scene",
        })
    return cmds


if __name__ == "__main__":
    for s in ("scene_sleep", "scene_home", "scene_away", "scene_movie",
              "scene_party", "scene_read"):
        print(s, "->", [(c["action"], c["room"]) for c in expand(s)])
    print("晚安 in bedroom ->",
          [(c["action"], c["room"]) for c in expand("scene_sleep", "none", "bedroom")])
