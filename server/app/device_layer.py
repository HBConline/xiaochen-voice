"""Device-control layer: intent (action + room) -> hardware, with real results.

This is the "MCP" the voice pipeline talks to. It is deliberately a plain
in-process module with a swappable backend, not a separate protocol server:
the only consumer is Laya intent, running in the same process, and a socket
hop would buy nothing but latency on a 4-core board.

Backends (DEVICE_BACKEND env):
  - "gpio": drive an LED per room directly on the 40-pin header (wiringOP).
            This is the bring-up backend and also the fallback when the
            relay hardware is absent.
  - "mqtt": publish to an MQTT topic consumed by an ESP32/ESP-01 relay board.

Both implement the same DeviceBackend interface, so swapping hardware never
touches intent.py, voice_brain.py, or the web panel.

The result dict returned by `execute` feeds straight into voice_brain's
context, which is what makes the spoken reply reflect reality instead of the
old hardcoded "成功".
"""
import os
import subprocess
import threading
import time

from home_config import (
    GPIO_LED_PINS,
    ACTION_DEVICE,
    QUERY_DEVICE,
    DEVICE_ROOMS,
    ACTION_REQUIREMENT,
    ALL_ROOMS as _ALL_ROOMS,
    expand_rooms,
    is_supported,
)

# action -> target state. Derived from the canonical ACTION_DEVICE table so the
# two can never drift: every mutating action maps to (device, on/off).
ACTION_STATE = {a: on for a, (_dev, on) in ACTION_DEVICE.items()}

# All-rooms expansion is for utterances that MEAN "every light". Default is to
# expand, so a bare "关掉所有灯" and every direct caller keep that behavior; the
# turn runner turns it off for a bare "开灯" it heard on its own.
_ALL_ROOMS_SWEEP = True


class _NoSweep:
    """Context manager: room="all"/"none" hits one room instead of the house.

    A bare "开灯" names no room, and on its own the all-rooms sweep is the right
    reading — the state store answers "灯开着吗" for any room afterwards, which a
    single-room write would not. But "先开灯再关空调" is one turn with two
    commands, and resolving the roomless one as 客厅+卧室+厨房 is a guess the
    user never made: the reply said "客厅、卧室、厨房灯开好了" for a request that
    likely meant the one lamp they were looking at. _resolve_rooms rewrites the
    implicit room from the session before this runs; what is left over is
    genuinely unresolvable, so it is driven as a single appliance.
    """

    def __enter__(self):
        global _ALL_ROOMS_SWEEP
        self._saved = _ALL_ROOMS_SWEEP
        _ALL_ROOMS_SWEEP = False
        return self

    def __exit__(self, *exc):
        global _ALL_ROOMS_SWEEP
        _ALL_ROOMS_SWEEP = self._saved
        return False


# Device class each action belongs to (state store keys on device, not action).
_ACTION_DEVICE = {a: dev for a, (dev, _on) in ACTION_DEVICE.items()}

# Rooms that mean "every room with a light". Imported from home_config; the
# alias keeps existing references in this file readable.
class BackendError(RuntimeError):
    """Raised when the hardware layer cannot carry out a request."""


# Persistent state store — survives restart so the brain never has to invent
# device state. One small JSON file; written atomically.
STATE_FILE = os.environ.get(
    "DEVICE_STATE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "device_state.json"),
)


class DeviceBackend:
    """Interface every backend implements."""

    name = 'base'

    def set_state(self, room: str, on: bool) -> None:
        raise NotImplementedError

    def get_state(self, room: str) -> bool | None:
        raise NotImplementedError


class GpioLedBackend(DeviceBackend):
    """Drive one LED per room via wiringOP's `gpio` CLI.

    The CLI is used rather than a Python GPIO library so the board needs no
    extra dependency beyond the wiringOP package the image already ships.
    """

    name = 'gpio'

    def __init__(self, pins: dict | None = None) -> None:
        self._pins = dict(pins or GPIO_LED_PINS)
        self._state: dict = {}
        for room, pin in self._pins.items():
            subprocess.run(['gpio', 'mode', str(pin), 'out'],
                           check=False, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            self._state[room] = False

    def set_state(self, room: str, on: bool) -> None:
        pin = self._pins.get(room)
        if pin is None:
            raise BackendError(f'no GPIO configured for room {room!r}')
        proc = subprocess.run(['gpio', 'write', str(pin), '1' if on else '0'],
                              check=False, capture_output=True, text=True)
        if proc.returncode != 0:
            raise BackendError(proc.stderr.strip() or f'gpio write failed ({proc.returncode})')
        self._state[room] = bool(on)

    def get_state(self, room: str):
        """Read the pin back from hardware — the cache is only a fallback.

        A written-then-cached state lies the moment the hardware stops
        cooperating (LED dies, wire loosens, pin re-muxed). `gpio read` returns
        the pin's real level, so the brain reports what the board sees. If the
        CLI itself fails, fall back to the last write so a transient gpio error
        does not blank every status answer.
        """
        pin = self._pins.get(room)
        if pin is None:
            return None
        proc = subprocess.run(['gpio', 'read', str(pin)],
                              check=False, capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip() in ('0', '1'):
            self._state[room] = (proc.stdout.strip() == '1')
        return self._state.get(room)


class RemoteGpioBackend(DeviceBackend):
    """Drive the board's LEDs over SSH from a PC-hosted pipeline.

    Used when the voice front end (mic/speaker) runs on the PC but the LEDs
    live on the Orange Pi. Each command is one `gpio write` on the remote,
    batched into a single ssh call per request to avoid per-pin round trips.
    Assumes key-based, non-interactive ssh (BatchMode) to the board.
    """

    name = 'remote'

    def __init__(self, host: str, user: str = 'orangepi',
                 pins: dict | None = None, ssh_timeout: int = 8) -> None:
        self._host = host
        self._user = user
        self._pins = dict(pins or GPIO_LED_PINS)
        self._timeout = ssh_timeout
        self._state: dict = {r: False for r in self._pins}
        # Configure every pin as output once, so later writes are one round trip.
        modes = '; '.join(f'gpio mode {p} out' for p in self._pins.values())
        self._run(modes)

    def _run(self, remote_cmd: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ['ssh', '-o', 'BatchMode=yes',
             '-o', f'ConnectTimeout={self._timeout}',
             f'{self._user}@{self._host}', remote_cmd],
            check=False, capture_output=True, text=True, timeout=self._timeout + 4)

    def set_state(self, room: str, on: bool) -> None:
        pin = self._pins.get(room)
        if pin is None:
            raise BackendError(f'no remote GPIO configured for room {room!r}')
        proc = self._run(f'gpio write {pin} {1 if on else 0}')
        if proc.returncode != 0:
            raise BackendError(proc.stderr.strip() or 'ssh gpio write failed')
        self._state[room] = bool(on)

    def get_state(self, room: str):
        """Read the remote pin back over the same ssh channel (one round trip).
        Cache is the fallback if the ssh read fails, matching GpioLedBackend."""
        pin = self._pins.get(room)
        if pin is None:
            return None
        proc = self._run(f'gpio read {pin}')
        if proc.returncode == 0 and proc.stdout.strip() in ('0', '1'):
            self._state[room] = (proc.stdout.strip() == '1')
        return self._state.get(room)


def parse_relay_topics(spec: str | None) -> dict:
    """Parse "room=topic,room=topic" into a room -> MQTT topic map."""
    if not spec:
        return {}
    out = {}
    for part in spec.split(','):
        part = part.strip()
        if not part:
            continue
        room, _, topic = part.partition('=')
        if room and topic:
            out[room.strip()] = topic.strip()
    return out


class MqttRelayBackend(DeviceBackend):
    """Publish relay commands to an ESP32/ESP-01 running the MQTT device agent.

    Topic contract (matches the Phase-1 ESP32 agent):
        chenvoice/<room>/set   payload "on" | "off"   (QoS 1, retained)
        chenvoice/<room>/state payload "on" | "off"   (reported back)

    `topics` overrides the whole topic per room, for relays that speak their
    own dialect (Tasmota: cmnd/<name>/POWER, payload on/off is case-blind).

    paho-mqtt is imported lazily so the GPIO backend keeps working on a board
    where the MQTT client is not installed. Tests inject a fake `client`.
    """

    name = 'mqtt'

    def __init__(self, host: str = '127.0.0.1', port: int = 1883,
                 topic_prefix: str = 'chenvoice', topics: dict | None = None,
                 client=None) -> None:
        self._prefix = topic_prefix
        self._topics = dict(topics or {})
        self._state: dict = {}
        # execute() decides "has hardware" by asking the backend for _pins
        # (room -> binding). For MQTT the binding is the command topic.
        self._pins = dict(self._topics)
        if client is not None:
            self._client = client
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError as e:  # pragma: no cover - board-dependent
            raise BackendError(
                'paho-mqtt not installed; pip install paho-mqtt to use BACKEND=mqtt'
            ) from e
        self._client = mqtt.Client()
        self._client.connect(host, port, keepalive=30)
        self._client.loop_start()

    def _topic(self, room: str, leaf: str) -> str:
        if leaf == 'set' and room in self._topics:
            return self._topics[room]
        return f'{self._prefix}/{room}/{leaf}'

    def set_state(self, room: str, on: bool) -> None:
        payload = 'on' if on else 'off'
        info = self._client.publish(self._topic(room, 'set'), payload,
                                    qos=1, retain=True)
        info.wait_for_publish(timeout=3)
        if not info.is_published():
            raise BackendError('mqtt publish timed out; is the broker reachable?')
        self._state[room] = bool(on)

    def get_state(self, room: str):
        return self._state.get(room)


class MockBackend(DeviceBackend):
    """In-memory backend for PC development and tests: no hardware required.

    Records state so the whole pipeline — execute -> state store -> status
    query -> reply — can be exercised off-board. Never selects itself unless
    DEVICE_BACKEND=mock is set explicitly.
    """

    name = 'mock'

    def __init__(self, pins: dict | None = None) -> None:
        self._pins = dict(pins or GPIO_LED_PINS)
        self._state: dict = {r: False for r in self._pins}

    def set_state(self, room: str, on: bool) -> None:
        if room not in self._pins:
            raise BackendError(f'no pin for room {room!r}')
        self._state[room] = bool(on)

    def get_state(self, room: str):
        return self._state.get(room)


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------

class DeviceController:
    """Turn intent commands into hardware actions and report the truth back."""

    def __init__(self, backend: DeviceBackend | None = None) -> None:
        self._lock = threading.Lock()
        self.backend = backend or _make_backend()
        # Authoritative state store, keyed "device:room" -> bool. Backends keep
        # their own last-write cache, but this is the one the voice brain reads,
        # and it covers devices with no GPIO binding (AC/fan/TV) too. Loaded
        # from disk so a restart does not reset what the house believes.
        self._states: dict = {}
        self._load_states()

    @property
    def backend_name(self) -> str:
        return self.backend.name

    def _targets(self, device: str, room: str) -> list:
        """Rooms to drive for this command.

        An all-rooms sweep expands for EVERY device class, not just the lights.
        It used to expand only lights: "关掉空调" (room=none) then wrote the
        single key `ac:none`, while `query` expands none to DEVICE_ROOMS and
        reads `ac:bedroom`, `ac:living_room`… — different keys. The write landed
        where no read ever looked, so "关掉空调" followed by "卧室空调开着吗"
        answered from the stale per-room entry. Same rooms on both sides now.
        """
        if room in _ALL_ROOMS:
            # Only the light class is bound per room (GPIO_LED_PINS). AC/fan/TV
            # are state-only, and a curtain is one appliance, not eight — the
            # "all rooms" sweep applies to the lights alone.
            #
            # Expanding EVERY device class here made "打开窗帘" (room=none) report
            # eight executed rooms ("客厅、卧室、厨房…窗帘开好了") for a house
            # with no curtain hardware at all: _summary_context reads `executed`
            # straight into the spoken line, so the sweep was spoken back as a
            # claim about rooms that were never touched. A state-only device
            # records one key under "none" instead of inventing a per-room sweep.
            #
            # _NoSweep suppresses even the light sweep for a roomless command
            # inside a multi-command turn (see there).
            if device in ('light',) and _ALL_ROOMS_SWEEP:
                return list(DEVICE_ROOMS.get(device) or GPIO_LED_PINS.keys())
            return [room]
        return [room]

    # --- state store -------------------------------------------------------

    def _load_states(self) -> None:
        """Load the state store. Entries are {on, ts, by}; a bare bool from an
        older file is upgraded in place so upgrades need no manual migration."""
        try:
            import json
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self._states = {}
            for k, v in raw.items():
                if isinstance(v, dict):
                    self._states[k] = v
                else:  # legacy bool entry
                    self._states[k] = {"on": bool(v), "ts": 0, "by": "unknown"}
        except FileNotFoundError:
            self._states = {}
        except Exception as e:
            print(f"state load failed, starting empty: {e}")
            self._states = {}

    def _save_states(self) -> None:
        try:
            import json
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._states, f, ensure_ascii=False, indent=0)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            print(f"state save failed: {e}")

    def _record(self, device: str, room: str, on: bool, by: str = "voice") -> None:
        """Write one device state with a timestamp and actor, then persist.

        The {on, ts, by} triple is what lets a later question answer "开多久了"
        and lets rules like "离家时关灯" know what changed since when. `by`
        distinguishes a voice command from (future) auto-rules or the web panel.
        """
        self._states[f"{device}:{room}"] = {
            "on": bool(on), "ts": time.time(), "by": by,
        }
        self._save_states()

    def _live_state(self, device: str, room: str):
        """State for one (device, room): hardware read when bound, else the store.

        The light class is GPIO-bound, so its state is read back from the pin —
        the written cache lies when hardware fails. AC/fan/TV have no binding,
        so their state comes from the store (what we last told them to do), the
        only truth available for a device we cannot observe.
        """
        if device == 'light' and room in GPIO_LED_PINS:
            hw = self.backend.get_state(room)
            if hw is not None:
                prev = self._states.get(f"{device}:{room}") or {}
                if prev.get("on") != hw:  # hardware diverged: log the change
                    self._record(device, room, hw, by="hardware")
                return hw
        entry = self._states.get(f"{device}:{room}")
        return entry.get("on") if isinstance(entry, dict) else entry

    def query(self, device: str, room: str):
        """Current state of a device in a room: True/False, or None if unknown.

        'all'/'none' report True only when every room agrees, so "全屋灯开着吗"
        cannot be answered from a single lucky room.
        """
        if room in _ALL_ROOMS:
            rooms = DEVICE_ROOMS.get(device) or tuple(GPIO_LED_PINS.keys())
            vals = [self._live_state(device, r) for r in rooms]
            if any(v is None for v in vals) or not vals:
                return None
            return all(vals)
        return self._live_state(device, room)

    def query_action(self, action: str, room: str):
        """Map a query action (query_light…) to its device class and answer it."""
        device = QUERY_DEVICE.get(action)
        if device is None:
            return None
        return self.query(device, room)

    def last_change(self, device: str, room: str):
        """Epoch seconds when this device last changed, or None. Enables
        duration answers ("灯开多久了") without any sensor hardware."""
        entry = self._states.get(f"{device}:{room}")
        if isinstance(entry, dict):
            return entry.get("ts")
        return None

    def history(self, device: str | None = None) -> list:
        """[(key, on, ts, by), ...] sorted newest first, for the panel and for
        '刚才' questions. Optionally filtered to one device class."""
        rows = []
        for key, entry in self._states.items():
            dev, _, room = key.partition(":")
            if device and dev != device:
                continue
            if isinstance(entry, dict):
                rows.append((key, entry.get("on"), entry.get("ts"), entry.get("by")))
            else:
                rows.append((key, bool(entry), 0, "unknown"))
        rows.sort(key=lambda r: r[2] or 0, reverse=True)
        return rows

    def execute(self, action: str, room: str) -> dict:
        """Carry out one intent command, all-or-nothing per room.

        Returns {ok, action, room, detail, executed, failed, supported}. `ok` is
        True only when every target room succeeded; a partial sweep reports ok
        False with both lists so the reply can say "客厅好了,厨房没成" instead of
        a blanket claim. Actions that are recognised but unbound/unsupported
        return ok True, supported False, with a spoken `detail`.
        """
        supported, reason = is_supported(action)
        if action not in ACTION_STATE:
            return {
                'ok': True,
                'supported': supported if action in ACTION_REQUIREMENT else False,
                'action': action,
                'room': room,
                'executed': [],
                'failed': [],
                'detail': reason or f'{action} has no hardware binding yet',
            }

        want = ACTION_STATE[action]
        device = _ACTION_DEVICE.get(action, action)
        targets = self._targets(device, room)
        # Only the light class has a real per-room GPIO today. Devices with no
        # hardware binding are state-only: recording intent is the whole action,
        # and driving the backend for them would report a false failure.
        #
        # "Has hardware" is per TARGET, not per device class. A light command
        # that resolved to no room at all — the all-rooms sweep suppressed for a
        # roomless one inside a multi-command turn — carries the room label
        # "none", and there is no pin called "none": the GPIO write raised
        # "no GPIO configured for room 'none'" and the reply told the user
        # "灯没成，设备没有响应" for a lamp that was never even attempted.
        # With no target there is nothing to drive, so record the intent instead,
        # exactly like the state-only classes.
        pins = getattr(self.backend, '_pins', None) or {}
        has_hardware = device in ('light',) and any(t in pins for t in targets)
        done, failed = [], []
        with self._lock:
            if not has_hardware:
                for r in targets:
                    self._record(device, r, want)
                return {
                    'ok': True, 'supported': True, 'action': action, 'room': room,
                    'executed': targets, 'failed': [],
                    'detail': f'{"on" if want else "off"}: {",".join(targets)} (state-only)',
                }
            for r in targets:
                try:
                    self.backend.set_state(r, want)
                    done.append(r)
                except BackendError as e:
                    # PC dev has no GPIO; DEVICE_ASSUME_OK lets the state store
                    # be exercised without changing board semantics (default off).
                    if os.environ.get('DEVICE_ASSUME_OK') == '1':
                        done.append(r)
                        continue
                    failed.append(r)
                    self._fail_reason = str(e)
            # Record only what the hardware accepted, so the store never claims
            # a change that failed. Devices with no GPIO binding have no target
            # to sweep: when done is empty and there was no failure, record the
            # named room so AC/fan/TV status still reflects intent.
            for r in done:
                self._record(device, r, want)
            if not targets or (not done and not failed):
                self._record(device, room, want)
        ok = not failed
        if ok:
            detail = f'{"on" if want else "off"}: {",".join(done) or room}'
        else:
            detail = f'failed: {",".join(failed)}'
        return {
            'ok': ok,
            'supported': True,
            'action': action,
            'room': room,
            'executed': done,
            'failed': failed,
            'detail': detail,
        }

    def snapshot(self) -> dict:
        """Current state per room for the web panel (light class by default)."""
        return {r: self.query('light', r) for r in GPIO_LED_PINS}


def _make_backend() -> DeviceBackend:
    kind = os.environ.get('DEVICE_BACKEND', 'gpio').lower()
    if kind == 'gpio':
        return GpioLedBackend()
    if kind == 'mqtt':
        host = os.environ.get('MQTT_HOST', '127.0.0.1')
        port = int(os.environ.get('MQTT_PORT', '1883'))
        topics = parse_relay_topics(os.environ.get('MQTT_RELAY_TOPICS'))
        return MqttRelayBackend(host, port, topics=topics)
    if kind == 'remote':
        host = os.environ.get('REMOTE_HOST', '192.168.207.150')
        user = os.environ.get('REMOTE_USER', 'orangepi')
        return RemoteGpioBackend(host, user)
    if kind == 'mock':
        return MockBackend()
    raise BackendError(f'unknown DEVICE_BACKEND {kind!r}')


_controller: DeviceController | None = None
_controller_lock = threading.Lock()


def get_controller() -> DeviceController:
    """Process-wide singleton so GPIO mode is configured once."""
    global _controller
    with _controller_lock:
        if _controller is None:
            _controller = DeviceController()
    return _controller


def execute(action: str, room: str, no_sweep: bool = False) -> dict:
    """Carry out one command. `room="none"` sweeps every light by default.

    Pass no_sweep=True for a roomless command that came out of a turn with more
    than one command in it: there the house-wide reading is a guess the user
    never made, and the reply says so out loud.
    """
    ctl = get_controller()
    if no_sweep:
        with _NoSweep():
            return ctl.execute(action, room)
    return ctl.execute(action, room)


def query_action(action: str, room: str):
    """Answer a status question: True/False, or None when unknown."""
    return get_controller().query_action(action, room)


if __name__ == '__main__':
    ctl = get_controller()
    print('backend:', ctl.backend_name)
    for a, r in (('light_on', 'living_room'), ('light_off', 'living_room'),
                 ('light_on', 'all'), ('light_off', 'all')):
        print(f'{a:10s} {r:12s} ->', ctl.execute(a, r))
        time.sleep(0.4)
    print('snapshot:', ctl.snapshot())
