"""Quartz CGEvent keyboard and mouse injection, the Mac twin of
win_app/input_injector.py.

`receiver.py` calls the same five functions on either machine. The wire's key
names are already Mac-shaped -- "cmd" is Command here and the Windows key
there -- so the Windows capture does the semantic mapping before sending and
nothing is translated on arrival.

Every event posted from here carries INJECTED_MARK in its source user data,
which is how bridge.py's event tap tells Beamer's own injection apart from
a real hand on the Mac's own keyboard or trackpad: without that the tap
would read a pointer the PC is driving as a push against the Mac's crossing
edge and switch input straight back.

Quartz is imported at module level and guarded the same way clipboard_mac
guards AppKit, so this module stays importable, and its behaviour fakeable,
on a machine without it.
"""

import functools
import logging
import threading
import time
import unicodedata
from typing import Dict, List, Optional, Set, Tuple

import keyboard_layout
from key_codes import KEY_NAME_TO_CODE, US_KEY_CODES

try:
    import Quartz
except ImportError:  # pragma: no cover - exercised only off macOS
    Quartz = None

LOGGER = logging.getLogger("Beamer")

# Stamped into every injected event's kCGEventSourceUserData. Any non-zero
# value would do; this one is recognisable in a log.
INJECTED_MARK = 0xBEA3

MODIFIER_FLAG_NAMES = {
    "cmd": "kCGEventFlagMaskCommand",
    "cmd_r": "kCGEventFlagMaskCommand",
    "shift": "kCGEventFlagMaskShift",
    "shift_r": "kCGEventFlagMaskShift",
    "alt": "kCGEventFlagMaskAlternate",
    "alt_r": "kCGEventFlagMaskAlternate",
    "ctrl": "kCGEventFlagMaskControl",
    "ctrl_r": "kCGEventFlagMaskControl",
    "caps_lock": "kCGEventFlagMaskAlphaShift",
}
# The modifiers that make a keystroke a shortcut rather than text. Shift is
# not one of them: a shifted character arrives as the character it produced.
CHORD_MODIFIERS = {"cmd", "cmd_r", "alt", "alt_r", "ctrl", "ctrl_r"}

BUTTONS = ("left", "right", "middle", "back", "forward")
# How close together, in seconds and pixels, two clicks must be for the
# second to count as a double click. Matches macOS's own default interval;
# a click whose clickState is never set past 1 can never open a file.
DOUBLE_CLICK_SECONDS = 0.5
DOUBLE_CLICK_SLOP_PX = 5

# Wheel units per line of "line"-mode scroll. Both platforms call one wheel
# notch a line, so this is 1; it is named rather than inlined because it is
# the knob to turn if a mouse wheel feels wrong on the Mac.
LINES_PER_NOTCH = 1.0

_mods_down: Set[str] = set()
_held_codes: Dict[str, int] = {}
_buttons_down: Set[str] = set()
_last_click: Dict[str, Tuple[float, int, int, int]] = {}
_warned_names: Set[str] = set()
# One lock over the held-key bookkeeping and the post it describes: a session
# thread injecting while a reconnect's release_all snapshots and clears would
# otherwise press a key that nothing then remembers to release.
_state_lock = threading.RLock()


def _locked(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        with _state_lock:
            return function(*args, **kwargs)

    return wrapper


def _require():
    if Quartz is None:
        raise RuntimeError("Quartz event injection is only available on macOS")
    return Quartz


def current_flags(mods_down: Set[str]) -> int:
    quartz = _require()
    flags = 0
    for name in mods_down:
        attribute = MODIFIER_FLAG_NAMES.get(name)
        if attribute is not None:
            flags |= getattr(quartz, attribute)
    return flags


ARROW_KEYS = {"left", "right", "up", "down"}


def altgr_ctrl(mods_down: Set[str]) -> Optional[str]:
    """The made-up Left Ctrl of a PC's AltGr, when one is held. Windows sends AltGr as Left Ctrl
    and Right Alt, which arrive as Cmd (Ctrl under the positional style) and Right Option, and
    names the key by its character with only shift applied: AltGr+Shift+è comes as é."""
    if "alt_r" not in mods_down:
        return None
    return next((name for name in ("cmd", "ctrl") if name in mods_down), None)


def _script(character: str) -> str:
    return unicodedata.name(character, "").split(" ")[0]


def _by_place(name: str, us: Optional[str], mods_down: Set[str]) -> Optional[int]:
    """The key at `us`, the place the sender's key sits on a US keyboard, for a character this
    layout cannot place: under a chord, which has to reach the app as a shortcut, and for a letter
    of another script than the one this layout types there, so a PC on Russian types this Mac's
    English and one on English types its Russian, as the Mac's own keyboard would. Anything else,
    an é from a French PC say, stays text: the key it sits on types something unrelated here."""
    code = US_KEY_CODES.get(us)
    if code is None:
        return None
    if mods_down & CHORD_MODIFIERS:
        return code
    here = keyboard_layout.char_for(code)
    if name.isalpha() and here and here.isalpha() and _script(name) != _script(here):
        return code
    return None


def plan_key_event(name: str, down: bool, mods_down: Set[str], held: Optional[Dict[str, int]] = None, us: Optional[str] = None) -> Optional[Tuple[int, Optional[str]]]:
    """Pure planning: (key code, unicode string or None) for one key, or None
    when there is nothing to send. Mutates `mods_down` exactly as inject_key
    does, so a test can inspect the bookkeeping. `held` keeps the key each
    character went down on, so its release lets go of that key even if the
    layout changed in between. `us` is where the key sits on a US keyboard,
    sent by a physical keyboard and absent from a phone's."""
    lowered = name.lower()
    if lowered in MODIFIER_FLAG_NAMES:
        # Recorded before the event is built, so the modifier's own event
        # already carries the flag it sets -- which is what a flagsChanged
        # event does on real hardware.
        if down:
            mods_down.add(lowered)
        else:
            mods_down.discard(lowered)
    key_code = KEY_NAME_TO_CODE.get(lowered)
    if key_code is not None:
        return key_code, None
    if len(name) != 1:
        if name not in _warned_names:
            LOGGER.warning("Unknown key name ignored: %r", name)
            _warned_names.add(name)
        return None
    # The key that types it on this Mac's own layout, so a chord like Cmd+C lands on the right
    # key, and a plain "z" from the PC is not posted as the US Z key, which types "y" on a
    # German Mac. keyboard_layout is also what bridge.py reads keys with, so the two directions
    # agree about which key is which.
    if held is not None and name in held:
        # A repeat stays on the key the press went down on, even if the layout changed since, so
        # its release lets go of the key that is actually down.
        return (held[name] if down else held.pop(name)), None
    if altgr_ctrl(mods_down) is not None and us in US_KEY_CODES:
        # The key in the PC key's place under Option, without the Ctrl: the Mac's Option level is
        # its AltGr level, so an Italian PC's AltGr+Shift+è types { on an Italian Mac. inject_key
        # leaves the Ctrl's flag off.
        key_code = US_KEY_CODES[us]
        if held is not None and down:
            held[name] = key_code
        return key_code, None
    key_code = keyboard_layout.code_for(name.lower())
    if key_code is None:
        key_code = _by_place(name, us, mods_down)
        if key_code is not None and name.isupper() and not mods_down:
            # A capital from caps lock: posting the key would type this layout's small letter.
            return 0, keyboard_layout.char_for(key_code).upper()
    if key_code is not None and (name.islower() or not name.isalpha() or mods_down):
        if held is not None and down:
            held[name] = key_code
        return key_code, None
    # A character this layout table cannot place -- an accent, an em dash, a
    # capital with no shift held. Typed as text: it lands correctly in a text
    # field, which is all a character with no chord on it has to do.
    return 0, name


@_locked
def inject_key(name: str, down: bool, us: Optional[str] = None) -> None:
    quartz = _require()
    plan = plan_key_event(name, down, _mods_down, _held_codes, us)
    if plan is None:
        return
    key_code, text = plan
    event = quartz.CGEventCreateKeyboardEvent(None, key_code, bool(down))
    if event is None:
        raise RuntimeError(f"CGEventCreateKeyboardEvent failed for {name!r}")
    if text is None:
        flags = current_flags(_mods_down - {altgr_ctrl(_mods_down)})
        if name.lower() in ARROW_KEYS:
            # A real arrow key carries these two; the Spaces and Mission Control shortcuts
            # (Ctrl+Arrow) ignore an arrow that lacks them.
            flags |= quartz.kCGEventFlagMaskSecondaryFn | quartz.kCGEventFlagMaskNumericPad
        quartz.CGEventSetFlags(event, flags)
    else:
        quartz.CGEventKeyboardSetUnicodeString(event, len(text), text)
        # Flags are deliberately cleared: the string says what to type, and
        # leaving shift set would have some apps type it twice over.
        quartz.CGEventSetFlags(event, 0)
    _post(event)


def clamp_to_displays(x: int, y: int, rects) -> Tuple[int, int]:
    """The nearest point inside a display to (x, y). A move is clamped to the
    display it is already on where it can be, so a pointer on a shorter
    monitor cannot be pushed into the empty space beside a taller one and
    stick there -- which is the same outer-edge rule the return edge uses."""
    if not rects:
        return x, y
    for rect in rects:
        if rect.x <= x <= rect.right and rect.y <= y <= rect.bottom:
            return x, y
    best = None
    for rect in rects:
        cx = min(max(x, rect.x), rect.right)
        cy = min(max(y, rect.y), rect.bottom)
        distance = (cx - x) ** 2 + (cy - y) ** 2
        if best is None or distance < best[0]:
            best = (distance, cx, cy)
    return best[1], best[2]


def plan_click_state(button: str, x: int, y: int, now: float, last_click: Dict) -> int:
    """Pure planning: the clickState for this press -- 2 for a double click,
    3 for a triple -- given where and when the last one landed. Mutates
    `last_click`, as inject_mouse_button does."""
    previous = last_click.get(button)
    state = 1
    if previous is not None:
        when, px, py, previous_state = previous
        if (
            now - when <= DOUBLE_CLICK_SECONDS
            and abs(px - x) <= DOUBLE_CLICK_SLOP_PX
            and abs(py - y) <= DOUBLE_CLICK_SLOP_PX
        ):
            state = min(previous_state + 1, 3)
    last_click[button] = (now, x, y, state)
    return state


MOVE_EVENTS = {
    None: "kCGEventMouseMoved",
    "left": "kCGEventLeftMouseDragged",
    "right": "kCGEventRightMouseDragged",
    "middle": "kCGEventOtherMouseDragged",
    "back": "kCGEventOtherMouseDragged",
    "forward": "kCGEventOtherMouseDragged",
}
BUTTON_EVENTS = {
    "left": ("kCGEventLeftMouseDown", "kCGEventLeftMouseUp", "kCGMouseButtonLeft"),
    "right": ("kCGEventRightMouseDown", "kCGEventRightMouseUp", "kCGMouseButtonRight"),
    "middle": ("kCGEventOtherMouseDown", "kCGEventOtherMouseUp", "kCGMouseButtonCenter"),
    # The side buttons have no CoreGraphics constant; they are other buttons 3 and 4.
    "back": ("kCGEventOtherMouseDown", "kCGEventOtherMouseUp", 3),
    "forward": ("kCGEventOtherMouseDown", "kCGEventOtherMouseUp", 4),
}


def _held_button() -> Optional[str]:
    for button in BUTTONS:
        if button in _buttons_down:
            return button
    return None


def _post(event) -> None:
    quartz = _require()
    quartz.CGEventSetIntegerValueField(event, quartz.kCGEventSourceUserData, INJECTED_MARK)
    quartz.CGEventPost(quartz.kCGHIDEventTap, event)


def _mouse_event(event_name: str, x: int, y: int, button_name: Optional[str]):
    quartz = _require()
    if button_name is None:
        button = 0
    elif isinstance(button_name, int):
        button = button_name
    else:
        button = getattr(quartz, button_name)
    event = quartz.CGEventCreateMouseEvent(
        None, getattr(quartz, event_name), quartz.CGPointMake(float(x), float(y)), button
    )
    if event is None:
        raise RuntimeError(f"CGEventCreateMouseEvent failed for {event_name}")
    quartz.CGEventSetFlags(event, current_flags(_mods_down))
    return event


def inject_mouse_move(dx: int, dy: int) -> None:
    import desktop_mac

    quartz = _require()
    x, y = desktop_mac.cursor_position()
    x, y = clamp_to_displays(x + int(dx), y + int(dy), desktop_mac.monitors())
    held = _held_button()
    event = _mouse_event(MOVE_EVENTS[held], x, y, None if held is None else BUTTON_EVENTS[held][2])
    # The deltas as well as the point: anything reading raw mouse movement --
    # a game, a 3D viewport -- sees nothing without them.
    quartz.CGEventSetIntegerValueField(event, quartz.kCGMouseEventDeltaX, int(dx))
    quartz.CGEventSetIntegerValueField(event, quartz.kCGMouseEventDeltaY, int(dy))
    _post(event)


@_locked
def inject_mouse_button(button: str, down: bool) -> None:
    import desktop_mac

    quartz = _require()
    names = BUTTON_EVENTS.get(button)
    if names is None:
        LOGGER.warning("Unknown mouse button ignored: %r", button)
        return
    down_name, up_name, button_name = names
    x, y = desktop_mac.cursor_position()
    event = _mouse_event(down_name if down else up_name, x, y, button_name)
    if down:
        _buttons_down.add(button)
        state = plan_click_state(button, x, y, time.monotonic(), _last_click)
    else:
        _buttons_down.discard(button)
        previous = _last_click.get(button)
        state = previous[3] if previous is not None else 1
    quartz.CGEventSetIntegerValueField(event, quartz.kCGMouseEventClickState, state)
    _post(event)


def inject_scroll(dy, dx=0.0, mode: str = "line") -> None:
    quartz = _require()
    if mode == "pixel":
        unit = quartz.kCGScrollEventUnitPixel
        wheel_y, wheel_x = int(round(float(dy))), int(round(float(dx)))
    else:
        unit = quartz.kCGScrollEventUnitLine
        wheel_y = int(round(float(dy) * LINES_PER_NOTCH))
        wheel_x = int(round(float(dx) * LINES_PER_NOTCH))
    if not wheel_y and not wheel_x:
        return
    event = quartz.CGEventCreateScrollWheelEvent(None, unit, 2, wheel_y, wheel_x)
    if event is None:
        raise RuntimeError("CGEventCreateScrollWheelEvent failed")
    quartz.CGEventSetFlags(event, current_flags(_mods_down))
    _post(event)


def inject_gesture(name: str) -> None:
    """Windows sends no gestures -- there is no trackpad event it could
    classify -- so this exists only to keep the injector interface the same
    on both machines."""
    LOGGER.warning("Gesture %r ignored: Beamer does not inject gestures on the Mac", name)


@_locked
def release_all() -> None:
    """Let go of every key and button this module is holding. Called when the
    link drops mid-chord, so a Command key held at the moment the PC went
    away does not stay down on the Mac."""
    for name in sorted(_mods_down) + sorted(_held_codes):
        try:
            inject_key(name, down=False)
        except Exception:
            LOGGER.exception("Could not release %r", name)
    for button in sorted(_buttons_down):
        try:
            inject_mouse_button(button, down=False)
        except Exception:
            LOGGER.exception("Could not release the %s mouse button", button)
    _mods_down.clear()
    _held_codes.clear()
    _buttons_down.clear()
