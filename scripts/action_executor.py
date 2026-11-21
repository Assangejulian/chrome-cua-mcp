"""Safe Windows physical-pixel action executor for Phase 1 closed Chrome GUI actions."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import time
from typing import Iterable

import pyperclip

from action_schema import ParsedAction
from roi_utils import Rect, is_point_on_window, is_window_ready, norm_to_screen

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
WHEEL_DELTA = 120

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

VK_MAP = {
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "tab": 0x09,
    "backspace": 0x08,
    "delete": 0x2E,
    "space": 0x20,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "f5": 0x74,
}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUT_UNION)]


def _send_inputs(inputs: list[INPUT]) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    sent = ctypes.windll.user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise OSError(f"SendInput sent {sent}/{len(inputs)} events")


def _to_virtual_abs(screen_x: int, screen_y: int) -> tuple[int, int]:
    user32 = ctypes.windll.user32
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = max(1, user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    vh = max(1, user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    nx = round((screen_x - vx) * 65535 / max(1, vw - 1))
    ny = round((screen_y - vy) * 65535 / max(1, vh - 1))
    return min(max(nx, 0), 65535), min(max(ny, 0), 65535)


def move_mouse_abs(screen_x: int, screen_y: int) -> None:
    ax, ay = _to_virtual_abs(screen_x, screen_y)
    _send_inputs(
        [INPUT(type=INPUT_MOUSE, union=_INPUT_UNION(mi=MOUSEINPUT(ax, ay, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, 0, None)))]
    )


def click_abs(screen_x: int, screen_y: int, button: str = "left", count: int = 1) -> None:
    move_mouse_abs(screen_x, screen_y)
    time.sleep(0.015)
    down_flag, up_flag = (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP) if button == "right" else (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)
    for i in range(count):
        _send_inputs(
            [
                INPUT(type=INPUT_MOUSE, union=_INPUT_UNION(mi=MOUSEINPUT(0, 0, 0, down_flag, 0, None))),
                INPUT(type=INPUT_MOUSE, union=_INPUT_UNION(mi=MOUSEINPUT(0, 0, 0, up_flag, 0, None))),
            ]
        )
        if i + 1 < count:
            time.sleep(0.05)


def scroll_abs(screen_x: int, screen_y: int, direction: str, amount: int) -> None:
    move_mouse_abs(screen_x, screen_y)
    time.sleep(0.015)
    delta = amount * WHEEL_DELTA
    if direction in ("up", "down"):
        wheel_val = delta if direction == "up" else (-delta) & 0xFFFFFFFF
        flag = MOUSEEVENTF_WHEEL
    else:
        wheel_val = delta if direction == "right" else (-delta) & 0xFFFFFFFF
        flag = MOUSEEVENTF_HWHEEL
    _send_inputs([INPUT(type=INPUT_MOUSE, union=_INPUT_UNION(mi=MOUSEINPUT(0, 0, wheel_val, flag, 0, None)))])


def _resolve_vk(key: str) -> int:
    k = key.strip().lower()
    if k in VK_MAP:
        return VK_MAP[k]
    if len(k) == 1 and ("a" <= k <= "z" or "0" <= k <= "9"):
        return ord(k.upper())
    raise ValueError(f"Unsupported hotkey token: {key!r}")


def send_hotkey(keys: Iterable[str]) -> None:
    vks = [_resolve_vk(k) for k in keys]
    downs = [INPUT(type=INPUT_KEYBOARD, union=_INPUT_UNION(ki=KEYBDINPUT(vk, 0, 0, 0, None))) for vk in vks]
    ups = [INPUT(type=INPUT_KEYBOARD, union=_INPUT_UNION(ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, None))) for vk in reversed(vks)]
    _send_inputs(downs + ups)


def paste_text_via_clipboard(text: str) -> None:
    """Safely inject Chinese/Unicode text via clipboard backup -> Ctrl+V -> restore."""
    try:
        prev = pyperclip.paste()
    except Exception:
        prev = None
    pyperclip.copy(text)
    time.sleep(0.02)
    send_hotkey(["ctrl", "v"])
    time.sleep(0.06)
    if prev is not None:
        try:
            pyperclip.copy(prev)
        except Exception:
            pass


def check_corner_failsafe() -> bool:
    """Return True if cursor is pushed into top-left corner (0..4, 0..4) emergency stop zone."""
    pt = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x <= 4 and pt.y <= 4


def execute_action(action: ParsedAction, hwnd: int, expected_roi: Rect, dry_run: bool = False) -> tuple[bool, str]:
    """Verify Chrome window lock and execute one validated Phase 1 action."""
    if check_corner_failsafe():
        return False, "failsafe_corner_triggered"
    if action.action in ("done", "not_found"):
        return True, f"terminal_{action.action}"
    if action.action == "wait":
        if not dry_run:
            time.sleep(action.seconds or 1.0)
        return True, "waited"

    if not is_window_ready(hwnd, expected_roi):
        return False, "window_lock_invalidated"

    if action.coordinate is not None:
        sx, sy = norm_to_screen(action.coordinate[0], action.coordinate[1], expected_roi)
    else:
        sx, sy = (0, 0)

    if dry_run:
        return True, f"dry_run:{action.action}@({sx},{sy})"

    if action.coordinate is not None and not is_point_on_window(hwnd, (sx, sy)):
        return False, "target_covered"

    if action.action == "left_click":
        click_abs(sx, sy, button="left", count=1)
    elif action.action == "double_click":
        click_abs(sx, sy, button="left", count=2)
    elif action.action == "right_click":
        click_abs(sx, sy, button="right", count=1)
    elif action.action == "move":
        move_mouse_abs(sx, sy)
    elif action.action == "scroll":
        scroll_abs(sx, sy, action.direction or "down", action.amount or 3)
    elif action.action == "type":
        if action.coordinate is not None:
            click_abs(sx, sy, button="left", count=1)
            time.sleep(0.05)
        paste_text_via_clipboard(action.text or "")
    elif action.action == "hotkey":
        send_hotkey(action.keys or ())
    return True, f"executed:{action.action}"
