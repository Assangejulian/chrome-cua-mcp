"""Smoked-glass command bar UI (acrylic blur-behind, light type, on-demand status/settings/confirmation), live MCP host, and Win32 CUA effects."""

from __future__ import annotations

from argparse import Namespace
from concurrent.futures import Future, ThreadPoolExecutor
import ctypes
from ctypes import wintypes
import io
import json
import math
import os
from pathlib import Path
import struct
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from typing import Any, Callable, Optional
import wave
import winsound

from PIL import Image, ImageDraw, ImageTk
import pyperclip

from action_schema import ParsedAction
from chrome_cua_mcp import client_config, start_http_mcp_background
from cua_effects import ComputerUseEffects
from cua_engine import (
    MODEL_PRESETS,
    ChromeCUAEngine,
    available_model_options,
    evaluate_policy,
)
from roi_utils import ChromeWindowInfo, find_chrome_windows

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Smoked glass + light type. GLASS must stay pure black: under the acrylic accent policy GDI black composites
# as blurred glass and every other pixel is added on top of it, so light ink stays crisp over any backdrop.
GLASS = "#000000"
FG_DIM = "#3C3C42"
FG = "#F4F4F6"
FG_SOFT = "#A6A6AD"
FG_FAINT = "#6C6C74"
ACCENT = "#6EA8FF"
OK_GREEN = "#5ED18C"
WARN = "#F3B550"
BAD = "#FF6B5E"
HOVER_BG = "#1C1C20"
TRACK = "#1E1E22"
ACRYLIC_TINT_ABGR = 0xD60C0A0A  # 84 % near-black tint over the blurred backdrop

# Motion: every animation is a Tween scaled by this factor; 0 makes each one land on its end state at once
MOTION_SCALE = 0.0 if os.environ.get("CHROME_PILOT_MOTION", "1") == "0" else 1.0

# Semantic aliases used by the behaviour code, tests, and preview scripts
BG_BASE = GLASS
BORDER_DEFAULT = "#2B2B30"
TEXT_PRIMARY = FG
TEXT_SECONDARY = FG_SOFT
TEXT_MUTED = FG_FAINT
CYAN = ACCENT
EMERALD = OK_GREEN
AMBER = WARN
DANGER = BAD

# Resolved against the installed families by _resolve_fonts(); first hit wins
UI_FONT_CANDIDATES = ("Microsoft YaHei UI", "Segoe UI Variable Text", "Segoe UI")
UI_FONT = "Microsoft YaHei UI"

PLACEHOLDER_TEXT = "要 Chrome 做什么？"

RESULT_MESSAGES = {
    "failsafe_corner_triggered": "已急停（光标在左上角安全区）",
    "window_lock_invalidated": "目标窗口位置已改变，请重新执行",
    "frame_changed": "画面已变化，请确认后重跑",
    "target_changed": "目标区域已变化，请重跑",
    "target_covered": "目标区域被其他窗口遮挡",
}

_ART_CACHE: dict[tuple[Any, ...], ImageTk.PhotoImage] = {}
_SS = 4  # supersampling factor for anti-aliased vector art


def _resolve_fonts(root: tk.Misc) -> None:
    """Pick the first installed UI families (CJK-capable first so mixed text shares one set of metrics)."""
    global UI_FONT
    try:
        families = set(tkfont.families(root))
    except tk.TclError:
        return
    UI_FONT = next((f for f in UI_FONT_CANDIDATES if f in families), UI_FONT)


def _track(text: str) -> str:
    """Inscription-style tracked capitals (hair spaces between letters)."""
    return "\u200a".join(text.upper())


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix(a_hex: str, b_hex: str, t: float) -> str:
    """Blend color a toward b by t in [0, 1]."""
    a, b = _hex_to_rgb(a_hex), _hex_to_rgb(b_hex)
    return "#%02X%02X%02X" % tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _cache_photo(key: tuple[Any, ...], img: Image.Image) -> ImageTk.PhotoImage:
    photo = ImageTk.PhotoImage(img)
    if len(_ART_CACHE) > 512:
        _ART_CACHE.clear()
    _ART_CACHE[key] = photo
    return photo


def _render_block(width: int, height: int, radius: int, fill_hex: str, bg_hex: str = GLASS) -> ImageTk.PhotoImage:
    """Supersampled solid ink block with barely-softened corners (the only filled surface in the UI)."""
    key = ("block", width, height, radius, fill_hex, bg_hex)
    if key in _ART_CACHE:
        return _ART_CACHE[key]
    s = _SS
    img = Image.new("RGB", (width * s, height * s), _hex_to_rgb(bg_hex))
    ImageDraw.Draw(img).rounded_rectangle([0, 0, width * s - 1, height * s - 1], radius=max(1, radius * s), fill=_hex_to_rgb(fill_hex))
    return _cache_photo(key, img.resize((width, height), Image.Resampling.LANCZOS))


def _stroke(draw: ImageDraw.ImageDraw, pts: list[tuple[float, float]], width: float, fill: tuple[int, int, int]) -> None:
    """Polyline with round caps/joins (PIL lines are butt-capped)."""
    draw.line(pts, fill=fill, width=max(1, round(width)), joint="curve")
    r = width / 2.0
    for x, y in pts:
        draw.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def _draw_icon(
    draw: ImageDraw.ImageDraw,
    kind: str,
    cx: float,
    cy: float,
    u: float,
    col: tuple[int, int, int],
    bg: tuple[int, int, int],
) -> None:
    """Draw a fine line icon centred at (cx, cy); u is 1/26 of the icon box in supersampled px."""
    w = 1.35 * u
    lw = max(1, round(w))
    if kind == "tune":
        for dy, kx in ((-4.4, 2.6), (0.0, -2.8), (4.4, 0.8)):
            y = cy + dy * u
            _stroke(draw, [(cx - 6.4 * u, y), (cx + 6.4 * u, y)], w, col)
            kr = 1.9 * u
            draw.ellipse([cx + kx * u - kr, y - kr, cx + kx * u + kr, y + kr], fill=bg, outline=col, width=lw)
    elif kind == "minus":
        _stroke(draw, [(cx - 5.2 * u, cy), (cx + 5.2 * u, cy)], w, col)
    elif kind == "close":
        d = 4.6 * u
        _stroke(draw, [(cx - d, cy - d), (cx + d, cy + d)], w, col)
        _stroke(draw, [(cx - d, cy + d), (cx + d, cy - d)], w, col)
    elif kind in {"bell", "bell_off"}:
        top, rim = cy - 6.0 * u, cy + 3.4 * u
        draw.arc([cx - 4.6 * u, top, cx + 4.6 * u, top + 9.2 * u], 180, 360, fill=col, width=lw)
        _stroke(draw, [(cx - 4.6 * u, top + 4.6 * u), (cx - 4.6 * u, rim - 1.2 * u), (cx - 6.2 * u, rim)], w, col)
        _stroke(draw, [(cx + 4.6 * u, top + 4.6 * u), (cx + 4.6 * u, rim - 1.2 * u), (cx + 6.2 * u, rim)], w, col)
        _stroke(draw, [(cx - 6.2 * u, rim), (cx + 6.2 * u, rim)], w, col)
        draw.chord([cx - 2.0 * u, rim + 0.4 * u - 2.0 * u, cx + 2.0 * u, rim + 0.4 * u + 2.0 * u], 0, 180, fill=col)
        if kind == "bell_off":
            a, b = (cx - 6.8 * u, cy - 6.8 * u), (cx + 6.8 * u, cy + 6.8 * u)
            _stroke(draw, [a, b], w + 2.2 * u, bg)
            _stroke(draw, [a, b], w, col)
    elif kind == "refresh":
        r = 5.2 * u
        draw.arc([cx - r, cy - r, cx + r, cy + r], 330, 270, fill=col, width=lw)
        tx, ty = cx + 0.8 * u, cy - r
        _stroke(draw, [(tx - 2.6 * u, ty - 2.8 * u), (tx, ty), (tx - 2.6 * u, ty + 2.8 * u)], w, col)
    elif kind == "expand":
        d = 4.4 * u
        _stroke(draw, [(cx - d, cy + d), (cx + d, cy - d)], w, col)
        _stroke(draw, [(cx - 0.4 * u, cy - d), (cx + d, cy - d), (cx + d, cy + 0.4 * u)], w, col)
    elif kind in {"spark", "spark_off"}:
        pts = []
        for i in range(8):
            ang = math.pi / 4 * i - math.pi / 2
            rr = (6.6 if i % 2 == 0 else 2.2) * u
            pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
        draw.polygon(pts, fill=col)
        if kind == "spark_off":
            a, b = (cx - 6.8 * u, cy - 6.8 * u), (cx + 6.8 * u, cy + 6.8 * u)
            _stroke(draw, [a, b], w + 2.2 * u, bg)
            _stroke(draw, [a, b], w, col)
    elif kind == "link":
        r = 2.6 * u
        for dx in (-5.0, 5.0):
            draw.ellipse([cx + dx * u - r, cy - r, cx + dx * u + r, cy + r], outline=col, width=lw)
        _stroke(draw, [(cx - 2.4 * u, cy), (cx + 2.4 * u, cy)], w, col)
    elif kind in {"chevron", "chevron_up"}:
        d = -1.0 if kind == "chevron_up" else 1.0
        _stroke(draw, [(cx - 3.6 * u, cy - 1.6 * u * d), (cx, cy + 2.0 * u * d), (cx + 3.6 * u, cy - 1.6 * u * d)], w, col)
    elif kind == "warn":
        _stroke(draw, [(cx, cy - 6.4 * u), (cx + 7.0 * u, cy + 5.6 * u), (cx - 7.0 * u, cy + 5.6 * u), (cx, cy - 6.4 * u)], w, col)
        _stroke(draw, [(cx, cy - 2.2 * u), (cx, cy + 1.2 * u)], w, col)
        draw.ellipse([cx - 0.9 * u, cy + 2.8 * u, cx + 0.9 * u, cy + 4.6 * u], fill=col)


def _render_icon(kind: str, size: int, color_hex: str, bg_hex: str = GLASS, icon_scale: float = 1.0) -> ImageTk.PhotoImage:
    """Render a supersampled line icon directly on the page (no plate)."""
    key = ("icon", kind, size, color_hex, bg_hex, icon_scale)
    if key in _ART_CACHE:
        return _ART_CACHE[key]
    s = _SS
    sz = size * s
    img = Image.new("RGB", (sz, sz), _hex_to_rgb(bg_hex))
    u = sz / 26.0 * icon_scale
    _draw_icon(ImageDraw.Draw(img), kind, sz / 2.0, sz / 2.0, u, _hex_to_rgb(color_hex), _hex_to_rgb(bg_hex))
    return _cache_photo(key, img.resize((size, size), Image.Resampling.LANCZOS))


def _render_seal_frame(size: int, color_hex: str, bg_hex: str, phase: Optional[float], point_scale: float = 1.0) -> Image.Image:
    """Status seal: an ink point inside a fine ring; while working (phase in [0, 1)) an arc of the ring turns."""
    s = _SS
    sz = size * s
    c = sz / 2.0
    img = Image.new("RGB", (sz, sz), _hex_to_rgb(bg_hex))
    d = ImageDraw.Draw(img)
    rr = sz * 0.39
    lw = max(1, round(1.2 * s))
    ring = [c - rr, c - rr, c + rr, c + rr]
    d.ellipse(ring, outline=_hex_to_rgb(_mix(bg_hex, color_hex, 0.28 if phase is not None else 0.50)), width=lw)
    if phase is not None:
        start = phase * 360.0 - 90.0
        d.arc(ring, start, start + 120.0, fill=_hex_to_rgb(color_hex), width=lw)
    r0 = sz * 0.165 * point_scale
    d.ellipse([c - r0, c - r0, c + r0, c + r0], fill=_hex_to_rgb(color_hex))
    return img.resize((size, size), Image.Resampling.LANCZOS)


class _AccentPolicy(ctypes.Structure):
    _fields_ = [("state", ctypes.c_int), ("flags", ctypes.c_int), ("color", ctypes.c_uint), ("anim", ctypes.c_int)]


class _WinCompAttrData(ctypes.Structure):
    _fields_ = [("attr", ctypes.c_int), ("data", ctypes.c_void_p), ("size", ctypes.c_size_t)]


def apply_glass_material(win: tk.Misc) -> bool:
    """Give a pure-black Tk toplevel the acrylic blur-behind material (hit-testing stays intact; no colour key).

    Returns False where the accent policy is unavailable or disabled (CHROME_PILOT_GLASS=0); callers then fall
    back to a plain, nearly opaque window.
    """
    if os.environ.get("CHROME_PILOT_GLASS", "1") == "0":
        return False
    try:
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
        accent = _AccentPolicy(4, 2, ACRYLIC_TINT_ABGR, 0)  # ACCENT_ENABLE_ACRYLICBLURBEHIND
        data = _WinCompAttrData(19, ctypes.cast(ctypes.byref(accent), ctypes.c_void_p), ctypes.sizeof(accent))  # WCA_ACCENT_POLICY
        return bool(ctypes.windll.user32.SetWindowCompositionAttribute(wintypes.HWND(hwnd), ctypes.byref(data)))
    except Exception:
        return False


def apply_win11_rounded_corners(root: tk.Misc, small: bool = False) -> None:
    """Ask DWM for rounded corners and a thin dark border."""
    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        pref = ctypes.c_int(3 if small else 2)  # DWMWCP_ROUNDSMALL / DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), 33, ctypes.byref(pref), ctypes.sizeof(pref))
        set_win11_border_color(root, BORDER_DEFAULT)
    except Exception:
        pass


def set_win11_border_color(root: tk.Misc, hex_color: str) -> None:
    """Color the native Windows 11 1px DWM window border (DWMWA_BORDER_COLOR=34)."""
    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        r, g, b = _hex_to_rgb(hex_color)
        colorref = wintypes.DWORD((b << 16) | (g << 8) | r)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), 34, ctypes.byref(colorref), ctypes.sizeof(colorref))
    except Exception:
        pass


def format_human_action(parsed: ParsedAction) -> str:
    """Turn a Phase-1 ParsedAction JSON into a short natural Chinese summary."""
    act = parsed.action
    if act == "left_click":
        return f"左键点击位置 {list(parsed.coordinate or ())}"
    if act == "double_click":
        return f"双击位置 {list(parsed.coordinate or ())}"
    if act == "right_click":
        return f"右键点击位置 {list(parsed.coordinate or ())}"
    if act == "move":
        return f"移动光标到 {list(parsed.coordinate or ())}"
    if act == "type":
        snippet = (parsed.text or "")[:18]
        return f"输入文字「{snippet}」"
    if act == "scroll":
        d_map = {"down": "向下", "up": "向上", "left": "向左", "right": "向右"}
        return f"{d_map.get(parsed.direction or 'down', '')}滚动 {parsed.amount or 3} 格"
    if act == "hotkey":
        return f"按下快捷键 {' + '.join(parsed.keys or ())}"
    if act == "drag":
        return f"拖拽 {list(parsed.coordinate or ())} → {list(parsed.end_coordinate or ())}"
    if act == "not_found":
        return parsed.reason or "当前画面未找到目标"
    return act


class SoundCuePlayer:
    """Pure-stdlib async glass-chime WAV cues (or custom assets/sounds/*.wav) for connect/done/alert/error events."""

    def __init__(self) -> None:
        presets: dict[str, list[tuple[float, float]]] = {
            "connect": [(784.0, 0.14)],
            "done": [(1046.5, 0.09), (1318.51, 0.10), (1567.98, 0.30)],
            "alert": [(880.0, 0.10), (0.0, 0.05), (880.0, 0.14)],
            "error": [(523.25, 0.11), (392.0, 0.22)],
        }
        self._clips: dict[str, bytes] = {kind: self._load_or_synth(kind, notes) for kind, notes in presets.items()}

    @classmethod
    def _load_or_synth(cls, kind: str, notes: list[tuple[float, float]]) -> bytes:
        custom = PROJECT_ROOT / "assets" / "sounds" / f"{kind}.wav"
        try:
            if custom.is_file():
                data = custom.read_bytes()
                if data[:4] == b"RIFF":
                    return data
        except Exception:
            pass
        return cls._synth(notes)

    @staticmethod
    def _synth(notes: list[tuple[float, float]], sample_rate: int = 44100, amp: float = 0.36) -> bytes:
        frames = bytearray()
        attack_len = max(1, int(sample_rate * 0.008))
        fade_out_len = max(1, int(sample_rate * 0.025))
        for freq, dur_s in notes:
            total = max(1, int(sample_rate * dur_s))
            dur_safe = max(dur_s, 0.05)
            for i in range(total):
                if freq <= 0.0:
                    sample = 0
                else:
                    t = i / sample_rate
                    env = min(1.0, i / attack_len)
                    if total - i < fade_out_len:
                        env *= max(0.0, (total - i) / fade_out_len)
                    decay = math.exp(-3.2 * t / dur_safe)
                    wave_mix = (
                        0.72 * math.sin(2.0 * math.pi * freq * t)
                        + 0.20 * math.sin(4.0 * math.pi * freq * t)
                        + 0.08 * math.sin(6.0 * math.pi * freq * t)
                    )
                    val = wave_mix * amp * env * decay
                    sample = max(-32767, min(32767, int(val * 32767)))
                frames.extend(struct.pack("<h", sample))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(bytes(frames))
        return buf.getvalue()

    def play(self, kind: str, enabled: bool = True) -> None:
        if not enabled:
            return
        data = self._clips.get(kind)
        if not data:
            return
        try:
            winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_ASYNC)
        except Exception:
            pass



# ---------------- Line-and-ink controls (no pills, no plates) ----------------

class Tween:
    """One eased 0→1 run on Tk's event loop. Time-based (a dropped frame shortens nothing and delays nothing);
    starting a new run on the same Tween replaces the old one, so interrupted motion never queues up."""

    FRAME_MS = 12

    def __init__(self, widget: tk.Misc) -> None:
        self._widget = widget
        self._after: Optional[str] = None
        self.frames = 0

    @property
    def running(self) -> bool:
        return self._after is not None

    def run(self, duration_ms: float, on_frame: Callable[[float], None], on_done: Optional[Callable[[], None]] = None) -> None:
        self.cancel()
        duration = duration_ms / 1000.0 * MOTION_SCALE
        self.frames = 0
        if duration <= 0.0:
            on_frame(1.0)
            if on_done:
                on_done()
            return
        t0 = time.perf_counter()

        def step() -> None:
            self._after = None
            k = min(1.0, (time.perf_counter() - t0) / duration)
            self.frames += 1
            try:
                on_frame(1.0 - (1.0 - k) ** 3)  # ease-out cubic: quick to respond, soft to land
                if k < 1.0:
                    self._after = self._widget.after(self.FRAME_MS, step)
                elif on_done:
                    on_done()
            except tk.TclError:
                pass

        step()

    def cancel(self) -> None:
        if self._after is not None:
            try:
                self._widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None


class InkButton(tk.Canvas):
    """Primary action as a solid light block; ghost=True renders a bare text action underlined on hover.

    The painted colour eases toward its target (rest / hover / a new role such as 执行→停止) instead of snapping.
    """

    def __init__(
        self,
        parent: tk.Widget,
        text: str,
        command: Callable[[], None],
        width: int = 64,
        height: int = 28,
        radius: int = 7,
        fill_hex: str = FG,
        hover_hex: str = ACCENT,
        fg: str = GLASS,
        bg_hex: str = GLASS,
        font_size: int = 9,
        ghost: bool = False,
    ) -> None:
        font = tkfont.Font(root=parent, family=UI_FONT, size=font_size, weight="normal" if ghost else "bold")
        width = max(width, font.measure(text) + 24)
        height = max(height, font.metrics("linespace") + 6)
        super().__init__(parent, width=width, height=height, bg=bg_hex, highlightthickness=0, bd=0, cursor="hand2")
        self._cw, self._ch, self._r = width, height, radius
        self._fill, self._hover, self._fg, self._bg = fill_hex, hover_hex, fg, bg_hex
        self._ghost = ghost
        self._cmd = command
        self._hovered = False
        self._pressed = False
        self._shown = fill_hex
        self._tween = Tween(self)
        self._img_id = self.create_image(0, 0, anchor="nw")
        self._txt_id = self.create_text(width // 2, height // 2, text=text, fill=fg, font=font)
        self._rule_id = self.create_line(0, 0, 0, 0, fill=fill_hex, state="hidden")
        self._redraw()
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _on_press(self, _e: object) -> None:
        self._pressed = True
        self._redraw()

    def _on_release(self, e: tk.Event[Any]) -> None:
        was = self._pressed
        self._pressed = False
        self._redraw()
        if was and 0 <= e.x <= self._cw and 0 <= e.y <= self._ch:
            self._cmd()

    def configure_style(
        self,
        text: Optional[str] = None,
        fill_hex: Optional[str] = None,
        hover_hex: Optional[str] = None,
        fg: Optional[str] = None,
    ) -> None:
        if text is not None:
            self.itemconfig(self._txt_id, text=text)
        if fill_hex is not None:
            self._fill = fill_hex
        if hover_hex is not None:
            self._hover = hover_hex
        if fg is not None:
            self._fg = fg
        self._ease_color(170)

    def _set_hover(self, state: bool) -> None:
        if state != self._hovered:
            self._hovered = state
            self._ease_color(120)

    def _ease_color(self, ms: float) -> None:
        start, goal = self._shown, (self._hover if self._hovered else self._fill)
        if start == goal:
            self._redraw()
            return

        def frame(k: float) -> None:
            self._shown = goal if k >= 1.0 else _mix(start, goal, round(k * 6) / 6)
            self._redraw()

        self._tween.run(ms, frame)

    def _redraw(self) -> None:
        col = self._shown
        dy = 1 if self._pressed else 0
        self.coords(self._txt_id, self._cw // 2, self._ch // 2 + dy)
        if self._ghost:
            self.itemconfig(self._img_id, state="hidden")
            self.itemconfig(self._txt_id, fill=col)
            bbox = self.bbox(self._txt_id)
            if self._hovered and bbox:
                self.coords(self._rule_id, bbox[0] + 1, bbox[3] - 1, bbox[2] - 1, bbox[3] - 1)
                self.itemconfig(self._rule_id, fill=col, state="normal")
            else:
                self.itemconfig(self._rule_id, state="hidden")
            return
        photo = _render_block(self._cw, self._ch, self._r, _mix(col, GLASS, 0.14) if self._pressed else col, bg_hex=self._bg)
        self._photo_ref = photo
        self.itemconfig(self._img_id, image=photo, state="normal")
        self.coords(self._img_id, 0, dy)
        self.itemconfig(self._txt_id, fill=self._fg)


class Tooltip:
    """Hover detail for what the layout leaves unsaid: one short line under the widget after a pause."""

    DELAY_MS = 450

    def __init__(self, widget: tk.Widget, text: str | Callable[[], str]) -> None:
        self._widget, self._text = widget, text
        self._after: Optional[str] = None
        self._tip: Optional[tk.Toplevel] = None
        self._shown = ""
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Motion>", self._on_motion, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def _current(self) -> str:
        try:
            return str(self._text() if callable(self._text) else self._text or "")
        except Exception:
            return ""

    def _schedule(self, _e: object = None) -> None:
        self._cancel()
        self._after = self._widget.after(self.DELAY_MS, self._show)

    def _on_motion(self, _e: object = None) -> None:
        if self._tip is not None:
            if self._current() != self._shown:
                self.hide()
                self._schedule()
        elif self._after is None:
            self._schedule()

    def _cancel(self) -> None:
        if self._after is not None:
            try:
                self._widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _show(self) -> None:
        self._after = None
        text = self._current()
        if not text:
            return
        try:
            tip = tk.Toplevel(self._widget)
            tip.overrideredirect(True)
            tip.attributes("-topmost", True)
            tip.configure(bg=BORDER_DEFAULT)
            tk.Label(tip, text=text, bg="#151517", fg=FG_SOFT, font=(UI_FONT, 8), padx=8, pady=4, justify="left", wraplength=380).pack(padx=1, pady=1)
            tip.geometry(f"+{self._widget.winfo_rootx()}+{self._widget.winfo_rooty() + self._widget.winfo_height() + 4}")
        except tk.TclError:
            return
        self._tip, self._shown = tip, text
        try:
            tip.attributes("-alpha", 0.0)
            Tween(tip).run(90, lambda k: tip.attributes("-alpha", k))
        except tk.TclError:
            pass

    def hide(self, _e: object = None) -> None:
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


class IconButton(tk.Canvas):
    """Bare line icon; its ink eases between dimmed, rest, hover and the accent that marks an active toggle."""

    def __init__(
        self,
        parent: tk.Widget,
        kind: str,
        command: Callable[[], None],
        size: int = 26,
        bg_hex: str = GLASS,
        fg: str = FG_FAINT,
        hover_fg: str = FG,
        icon_scale: float = 1.0,
        tip: str | Callable[[], str] = "",
    ) -> None:
        super().__init__(parent, width=size, height=size, bg=bg_hex, highlightthickness=0, bd=0, cursor="hand2")
        self._kind, self._size, self._bg = kind, size, bg_hex
        self._fg, self._hover_fg = fg, hover_fg
        self._scale = icon_scale
        self._hovered = False
        self._active = False
        self._dim = False
        self._pressed = False
        self._shown = fg
        self._tween = Tween(self)
        self._cmd = command
        self._img_id = self.create_image(0, 0, anchor="nw")
        self._redraw()
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonPress-1>", lambda _e: self._set_pressed(True))
        self.bind("<ButtonRelease-1>", lambda _e: self._set_pressed(False))
        self.bind("<Button-1>", lambda _e: self._cmd(), add="+")
        if tip:
            Tooltip(self, tip)

    def set_dim(self, dim: bool) -> None:
        if bool(dim) != self._dim:
            self._dim = bool(dim)
            self._ease_color(180)

    def set_kind(self, kind: str) -> None:
        if kind != self._kind:
            self._kind = kind
            self._redraw()

    def set_active(self, active: bool) -> None:
        if bool(active) != self._active:
            self._active = bool(active)
            self._ease_color(160)

    def _set_hover(self, state: bool) -> None:
        if state != self._hovered:
            self._hovered = state
            self._ease_color(110)

    def _set_pressed(self, state: bool) -> None:
        self._pressed = state
        try:
            self._redraw()
        except tk.TclError:
            pass

    def _ease_color(self, ms: float) -> None:
        start = self._shown
        goal = self._hover_fg if self._hovered else (ACCENT if self._active else (FG_DIM if self._dim else self._fg))
        if start == goal:
            return

        def frame(k: float) -> None:
            self._shown = goal if k >= 1.0 else _mix(start, goal, round(k * 5) / 5)
            self._redraw()

        self._tween.run(ms, frame)

    def _redraw(self) -> None:
        photo = _render_icon(self._kind, self._size, self._shown, bg_hex=self._bg, icon_scale=self._scale * (0.88 if self._pressed else 1.0))
        self._photo_ref = photo
        self.itemconfig(self._img_id, image=photo)


class StatusSeal(tk.Canvas):
    """Ink point in a fine ring; an arc of the ring turns while the agent is working."""

    FRAMES = 24
    INTERVAL_MS = 55

    def __init__(self, parent: tk.Widget, size: int = 20, color: str = OK_GREEN, bg_hex: str = GLASS, cursor: str = "") -> None:
        super().__init__(parent, width=size, height=size, bg=bg_hex, highlightthickness=0, bd=0, cursor=cursor)
        self._size, self._bg, self._color = size, bg_hex, color
        self._animated = False
        self._frame = 0
        self._after: Optional[str] = None
        self._frames: dict[str, list[ImageTk.PhotoImage]] = {}
        self._pulse_frames: dict[str, list[ImageTk.PhotoImage]] = {}
        self._pulse_tween = Tween(self)
        self._img_id = self.create_image(0, 0, anchor="nw")
        self._show()

    def _frames_for(self, color: str) -> list[ImageTk.PhotoImage]:
        """Index 0 is the resting seal; 1..FRAMES are the turning arc."""
        if color not in self._frames:
            if len(self._frames) > 8:
                self._frames.clear()
            phases: list[Optional[float]] = [None] + [i / self.FRAMES for i in range(self.FRAMES)]
            self._frames[color] = [ImageTk.PhotoImage(_render_seal_frame(self._size, color, self._bg, p)) for p in phases]
        return self._frames[color]

    def _show(self) -> None:
        frames = self._frames_for(self._color)
        self.itemconfig(self._img_id, image=frames[1 + self._frame % self.FRAMES if self._animated else 0])

    PULSE = (1.25, 1.5, 1.62, 1.5, 1.3, 1.12, 1.0)

    def set_color(self, color: str) -> None:
        if color != self._color:
            self._color = color
            self._show()
            self.pulse()

    def pulse(self) -> None:
        """One soft swell of the point: the state has changed. Skipped while the working arc is turning."""
        if self._animated or MOTION_SCALE <= 0.0:
            return
        color = self._color
        if color not in self._pulse_frames:
            if len(self._pulse_frames) > 8:
                self._pulse_frames.clear()
            self._pulse_frames[color] = [ImageTk.PhotoImage(_render_seal_frame(self._size, color, self._bg, None, k)) for k in self.PULSE]
        frames = self._pulse_frames[color]

        def frame(k: float) -> None:
            if not self._animated and self._color == color:
                self.itemconfig(self._img_id, image=frames[min(len(frames) - 1, int(k * len(frames)))])

        self._pulse_tween.run(300, frame)

    def set_animated(self, animated: bool) -> None:
        animated = bool(animated)
        if animated == self._animated:
            return
        self._animated = animated
        if animated:
            self._tick()
        else:
            self.stop()
            self._show()

    def _tick(self) -> None:
        self._after = None
        if not self._animated:
            return
        self._frame = (self._frame + 1) % self.FRAMES
        try:
            self._show()
            self._after = self.after(self.INTERVAL_MS, self._tick)
        except tk.TclError:
            self._animated = False

    def stop(self) -> None:
        if self._after is not None:
            try:
                self.after_cancel(self._after)
            except Exception:
                pass
            self._after = None


class ProgressLine(tk.Canvas):
    """Hairline along the top edge: invisible at rest, an accent run with task progress, a pigment for the outcome."""

    HEIGHT = 3
    FRAMES = 40
    INTERVAL_MS = 50

    def __init__(self, parent: tk.Widget, width: int, bg_hex: str = GLASS) -> None:
        super().__init__(parent, width=width, height=self.HEIGHT, bg=bg_hex, highlightthickness=0, bd=0)
        self._bar_w = width
        self._bg = bg_hex
        self._state = "idle"
        self._progress: Optional[float] = None
        self._shown_progress = 0.0
        self._frame = 0
        self._after: Optional[str] = None
        self._cache: dict[tuple[Any, ...], ImageTk.PhotoImage] = {}
        self._fade = Tween(self)
        self._img_id = self.create_image(0, 0, anchor="nw")
        self._render()

    @property
    def state(self) -> str:
        return self._state

    def resize(self, width: int) -> None:
        width = max(40, int(width))
        if width != self._bar_w:
            self._bar_w = width
            self._cache.clear()
            self.configure(width=width)
            self._render()

    def _row(self, state: str, progress: Optional[float], phase: float) -> list[tuple[int, int, int]]:
        w = self._bar_w
        bg, track = _hex_to_rgb(self._bg), _hex_to_rgb(TRACK)
        if state == "idle":
            return [bg] * w
        if state in {"done", "error"}:
            return [_hex_to_rgb(OK_GREEN if state == "done" else BAD)] * w
        if state == "confirm":
            return [_hex_to_rgb(_mix(self._bg, WARN, 0.45 + 0.55 * (0.5 - 0.5 * math.cos(2.0 * math.pi * phase))))] * w
        accent = _hex_to_rgb(ACCENT)
        if progress is not None:
            edge = progress * w
            glint = (phase * 1.3 - 0.15) * max(edge, 1.0)
            row = []
            for x in range(w):
                if x > edge:
                    row.append(track)
                else:
                    k = 0.72 + 0.28 * math.exp(-((x - glint) ** 2) / (2.0 * (w * 0.06) ** 2))
                    row.append(tuple(round(c * k) for c in accent))
            return row
        head, span = (phase * 1.5 - 0.25) * w, w * 0.16
        return [tuple(round(t + (a - t) * max(0.0, 1.0 - abs(x - head) / span)) for t, a in zip(track, accent)) for x in range(w)]

    def _frame_image(self, state: str, progress: Optional[float], frame: int) -> ImageTk.PhotoImage:
        animated = state in {"running", "confirm"}
        key = (state, None if progress is None else round(progress, 3), frame if animated else 0)
        if key in self._cache:
            return self._cache[key]
        img = Image.new("RGB", (self._bar_w, 1))
        img.putdata(self._row(state, progress, frame / self.FRAMES))
        photo = ImageTk.PhotoImage(img.resize((self._bar_w, self.HEIGHT), Image.Resampling.NEAREST))
        if len(self._cache) > 300:
            self._cache.clear()
        self._cache[key] = photo
        return photo

    def _render(self) -> None:
        prog = self._progress
        if prog is not None:
            self._shown_progress += (prog - self._shown_progress) * 0.3
            if abs(prog - self._shown_progress) < 0.004:
                self._shown_progress = prog
            prog = round(self._shown_progress, 3)
        photo = self._frame_image(self._state, prog, self._frame)
        self._photo_ref = photo
        self.itemconfig(self._img_id, image=photo)

    def set_state(self, state: str, progress: Optional[float] = None) -> None:
        if progress is not None:
            progress = min(1.0, max(0.0, float(progress)))
            if self._progress is None:
                self._shown_progress = max(0.0, progress - 0.15)
        if state == self._state and progress == self._progress:
            return
        leaving = {"running": ACCENT, "done": OK_GREEN, "error": BAD, "confirm": WARN}.get(self._state) if state == "idle" else None
        self._fade.cancel()
        self._state = state
        self._progress = progress if state == "running" else None
        self._render()
        if leaving is not None:
            def frame(k: float) -> None:
                shade = _mix(leaving, self._bg, round(k * 10) / 10)
                key = ("fade", shade)
                if key not in self._cache:
                    self._cache[key] = ImageTk.PhotoImage(Image.new("RGB", (self._bar_w, self.HEIGHT), _hex_to_rgb(shade)))
                if self._state == "idle":
                    self.itemconfig(self._img_id, image=self._cache[key])

            self._fade.run(380, frame)
        if state in {"running", "confirm"}:
            if self._after is None:
                self._after = self.after(self.INTERVAL_MS, self._tick)
        else:
            self.stop()

    def _tick(self) -> None:
        self._after = None
        if self._state not in {"running", "confirm"}:
            return
        self._frame = (self._frame + 1) % self.FRAMES
        try:
            self._render()
            self._after = self.after(self.INTERVAL_MS, self._tick)
        except tk.TclError:
            pass

    def stop(self) -> None:
        if self._after is not None:
            try:
                self.after_cancel(self._after)
            except Exception:
                pass
            self._after = None


class PopupMenu:
    """Glass list under an anchor. Rows are (key, label) choices, (None, label) notes, or (None, "") hairlines.

    Closes on a choice, Esc, focus loss, a click anywhere else in the app (see ChromePilotPanel._on_any_click),
    or a second click on its own anchor (see toggled_off).
    """

    current: Optional["PopupMenu"] = None
    _focus_lost: tuple[Any, float] = (None, 0.0)

    def __init__(self, anchor: tk.Widget, rows: list[tuple[Any, str]], active_key: Any, on_choose: Callable[[Any], None], align: str = "left") -> None:
        PopupMenu.dismiss()
        self.anchor = anchor
        self._on_choose = on_choose
        self.opened_at = time.monotonic()
        self.rows: dict[Any, tk.Canvas] = {}
        self._plates: list[ImageTk.PhotoImage] = []
        font = tkfont.Font(root=anchor, family=UI_FONT, size=9)
        note_font = tkfont.Font(root=anchor, family=UI_FONT, size=8)
        pad_x, check_w = 12, 22
        width = max([font.measure(lbl) + 2 * pad_x + check_w for key, lbl in rows if key is not None]
                    + [note_font.measure(lbl) + 2 * pad_x for key, lbl in rows if key is None and lbl] + [120])
        row_h = font.metrics("linespace") + 10
        win = tk.Toplevel(anchor.winfo_toplevel())
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=GLASS)
        self.win = win
        body = tk.Frame(win, bg=GLASS)
        body.pack(padx=1, pady=5)
        for key, label in rows:
            if key is None and not label:
                tk.Frame(body, bg=TRACK, height=1, width=width).pack(pady=4)
            elif key is None:
                tk.Label(body, text=label, bg=GLASS, fg=FG_FAINT, font=note_font, anchor="w", padx=pad_x, pady=2, bd=0).pack(fill="x")
            else:
                item = tk.Canvas(body, width=width, height=row_h, bg=GLASS, highlightthickness=0, bd=0, cursor="hand2")
                plate = _render_block(width - 8, row_h - 2, 6, HOVER_BG)
                self._plates.append(plate)
                plate_id = item.create_image(4, 1, anchor="nw", image=plate, state="hidden")
                item.hovered = False  # type: ignore[attr-defined]
                item.create_text(pad_x, row_h // 2, anchor="w", text=label, fill=FG if key == active_key else FG_SOFT, font=font)
                if key == active_key:
                    cx, cy = width - pad_x - 6, row_h // 2
                    item.create_line(cx - 5, cy, cx - 1, cy + 4, cx + 6, cy - 4, fill=ACCENT, width=2, capstyle="round", joinstyle="round")
                item.bind("<Enter>", lambda _e, c=item, i=plate_id: self._hover_row(c, i, True))
                item.bind("<Leave>", lambda _e, c=item, i=plate_id: self._hover_row(c, i, False))
                item.bind("<Button-1>", lambda _e, k=key: self.choose(k))
                item.pack()
                self.rows[key] = item
        win.update_idletasks()
        x = anchor.winfo_rootx() if align == "left" else anchor.winfo_rootx() + anchor.winfo_width() - win.winfo_reqwidth()
        x = max(4, min(x, win.winfo_screenwidth() - win.winfo_reqwidth() - 4))
        y = anchor.winfo_rooty() + anchor.winfo_height() + 6
        win.attributes("-alpha", 0.0)
        win.geometry(f"+{x}+{y - 6}")
        win.update_idletasks()
        self.glass = apply_glass_material(win)
        apply_win11_rounded_corners(win, small=True)

        def settle(k: float) -> None:
            win.attributes("-alpha", k)
            win.geometry(f"+{x}+{y - round(6 * (1.0 - k))}")

        self.tween = Tween(win)
        self.tween.run(130, settle)
        win.bind("<Escape>", lambda _e: self.close())
        win.bind("<FocusOut>", lambda _e: self._on_focus_out())
        try:
            win.focus_force()
        except tk.TclError:
            pass
        PopupMenu.current = self

    @staticmethod
    def _hover_row(item: tk.Canvas, plate_id: int, on: bool) -> None:
        item.hovered = on  # type: ignore[attr-defined]
        item.itemconfig(plate_id, state="normal" if on else "hidden")

    def choose(self, key: Any) -> None:
        self.close()
        self._on_choose(key)

    def _on_focus_out(self) -> None:
        if time.monotonic() - self.opened_at > 0.2:
            PopupMenu._focus_lost = (self.anchor, time.monotonic())
            self.close()

    @classmethod
    def toggled_off(cls, anchor: tk.Widget) -> bool:
        """True when this click on `anchor` is the second one: its menu is open, or the click's own focus change just closed it.

        A click on the anchor takes focus from the menu first, so by the time the anchor sees the click the menu is
        already gone; without the second test that click would open it again.
        """
        if cls.current is not None and cls.current.anchor is anchor:
            cls.dismiss()
            return True
        lost_anchor, lost_at = cls._focus_lost
        cls._focus_lost = (None, 0.0)
        return lost_anchor is anchor and time.monotonic() - lost_at < 0.3

    def close(self) -> None:
        if PopupMenu.current is self:
            PopupMenu.current = None
        self.tween.cancel()
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        closed = getattr(self.anchor, "on_menu_closed", None)
        if closed is not None:
            closed()

    @classmethod
    def dismiss(cls) -> None:
        if cls.current is not None:
            cls.current.close()


class Dropdown(tk.Canvas):
    """The current value and a chevron; a click opens the choices. The value names itself, so there is no label."""

    def __init__(
        self,
        parent: tk.Widget,
        rows: Callable[[], list[tuple[Any, str]]],
        active_key: Any,
        on_choose: Callable[[Any], None],
        display: Optional[Callable[[Any], Optional[str]]] = None,
        tip: str | Callable[[], str] = "",
        font_size: int = 9,
        bg_hex: str = GLASS,
    ) -> None:
        self._font = tkfont.Font(root=parent, family=UI_FONT, size=font_size)
        self._ch = self._font.metrics("linespace") + 8
        super().__init__(parent, width=40, height=self._ch, bg=bg_hex, highlightthickness=0, bd=0, cursor="hand2")
        self._rows, self._active, self._on_choose, self._display, self._bg = rows, active_key, on_choose, display, bg_hex
        self._hovered = False
        self._open = False
        self._txt_id = self.create_text(0, self._ch // 2, anchor="w", font=self._font)
        self._chev_id = self.create_image(0, self._ch // 2, anchor="w")
        self._redraw()
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", lambda _e: self.toggle())
        if tip:
            Tooltip(self, tip)

    def _label(self) -> str:
        shown = self._display(self._active) if self._display else None
        if shown is None:
            shown = next((lbl for key, lbl in self._rows() if key == self._active), str(self._active))
        return shown

    def set_value(self, key: Any) -> None:
        if key != self._active:
            self._active = key
            self._redraw()

    def _set_hover(self, state: bool) -> None:
        if state != self._hovered:
            self._hovered = state
            self._redraw()

    def _redraw(self) -> None:
        text = self._label()
        tw = self._font.measure(text)
        lit = self._hovered or self._open
        self.itemconfig(self._txt_id, text=text, fill=FG if lit else FG_SOFT)
        self._chev = _render_icon("chevron_up" if self._open else "chevron", 12, FG_SOFT if lit else FG_FAINT, bg_hex=self._bg, icon_scale=1.5)
        self.itemconfig(self._chev_id, image=self._chev)
        self.coords(self._chev_id, tw + 4, self._ch // 2 + 1)
        if int(self.cget("width")) != tw + 4 + 12:  # re-configuring on every hover re-enters the widget and starves the tooltip
            self.configure(width=tw + 4 + 12)

    def toggle(self) -> None:
        if not PopupMenu.toggled_off(self):
            self.open()

    def open(self) -> PopupMenu:
        menu = PopupMenu(self, self._rows(), self._active, self._choose)
        self._open = True
        self._redraw()
        return menu

    def on_menu_closed(self) -> None:
        self._open = False
        try:
            self._redraw()
        except tk.TclError:
            pass

    def _choose(self, key: Any) -> None:
        self.set_value(key)
        self._on_choose(key)


def draw_target_marker(draw: ImageDraw.ImageDraw, px: int, py: int) -> None:
    """Quiet target ring for confirmation crops: white on a dark edge, readable on any page (no red cross-hair)."""
    for radius, color, width in ((19, (0, 0, 0), 5), (18, (255, 255, 255), 2)):
        draw.ellipse([(px - radius, py - radius), (px + radius, py + radius)], outline=color, width=width)


def _at_mark(action: str, crop: Any) -> str:
    """With the target crop on screen, 'the marked spot' says more than raw coordinates."""
    if crop is None or "位置 [" not in action:
        return action
    return action.split("位置 [")[0] + "标记处"


def _brief_reason(reason: str) -> str:
    """Drop the instruction clause the confirm buttons already express ('…，执行前需人工确认。')."""
    for sep in ("，执行前需人工确认", "，需人工确认"):
        reason = reason.split(sep)[0]
    return reason.rstrip("。")


# ---------------- Main Panel ----------------

class ChromePilotPanel:
    """Smoked-glass floating command bar for Chrome Pilot + CUA MCP."""

    def __init__(
        self,
        root: tk.Tk,
        args: Optional[Namespace] = None,
        enable_mcp: bool = True,
        sound_enabled: bool = True,
        effects_enabled: bool = True,
    ) -> None:
        self.root = root
        self.args = args or Namespace(port=18086, mcp_port=18390, max_image_tokens=1024, prompt_style="m1", quant="Q4_K_M")
        vlm_port = int(getattr(self.args, "port", 18086))
        mcp_port = int(getattr(self.args, "mcp_port", 18390))
        self._ui_thread_id = threading.get_ident()
        self._closed = False
        self.sound_enabled = bool(sound_enabled)
        self.effects_enabled = bool(effects_enabled)
        self.sound = SoundCuePlayer()
        self.effects = ComputerUseEffects(enabled=self.effects_enabled)
        self._border_after_id: Optional[str] = None
        self._timer_after_id: Optional[str] = None

        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pilot-ui")
        self.engine = ChromeCUAEngine(
            vlm_port=vlm_port,
            mcp_port=mcp_port,
            status_listener=self._on_engine_status,
            desktop_hide_hook=self._set_panel_hidden_for_io,
        )
        self.mcp_server = None
        self.mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"
        if enable_mcp:
            try:
                self.mcp_server, _th, self.mcp_url = start_http_mcp_background(self.engine, port=mcp_port)
            except OSError:
                self.mcp_url = f"端口 {mcp_port} 已被占用"

        self.busy = False
        self.collapsed = False
        self.settings_open = False
        self.desktop_windows: list[ChromeWindowInfo] = []
        self.selected_hwnd: int = 0
        self.pending_desktop: Optional[dict[str, Any]] = None
        self.pending_managed: Optional[dict[str, Any]] = None
        self._preview_photo: Optional[ImageTk.PhotoImage] = None
        self._pline_progress: Optional[float] = None
        self._has_placeholder = True
        self._drag_origin = (0, 0)
        self._status_after: Optional[str] = None
        self._status_full = ""
        self._status_tone = TEXT_SECONDARY
        self._status_visible = False
        self._status_truncated = False
        self._status_meta_len = 0
        self._meta_parts: tuple[str, str] = ("", "")
        self._meta_elapsed = ""
        self._confirm_visible = False
        self._layout_key: Optional[tuple[bool, bool, bool]] = None
        self._hover_after: Optional[str] = None

        self.target_mode = "managed"
        self.exec_mode = "auto"
        models = available_model_options()
        pref = f"M3.5 · {getattr(self.args, 'quant', 'Q4_K_M')}"
        self.model_choice = pref if pref in models else (models[0] if models else "M3.5 · Q4_K_M")

        self._setup_window()
        self._build_ui()
        self._refresh_desktop_windows(silent=True)
        if self.desktop_windows and self.selected_hwnd:
            self.target_mode = "desktop"
            try:
                self.engine.set_target_mode("desktop", self.selected_hwnd)
            except Exception:
                self.target_mode = "managed"
        self._sync_drawer_styles()
        if enable_mcp and self.mcp_server is None:
            self.root.after(20, lambda: self.show_status(self.mcp_url, AMBER, ttl_ms=6000))
        self.root.update_idletasks()
        try:
            self.engine.panel_hwnd = int(ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id())
        except Exception:
            self.engine.panel_hwnd = int(self.root.winfo_id() or 0)
        self._center_top()
        apply_win11_rounded_corners(self.root)
        self.glass = apply_glass_material(self.root)
        self._alpha_rest = 1.0 if self.glass else 0.96  # no acrylic on this system: a plain, nearly opaque dark bar
        self._enter()

    PANEL_W = 560
    SHELL_PADX = 14

    def _setup_window(self) -> None:
        self.root.title("Chrome Pilot")
        _ART_CACHE.clear()  # cached PhotoImages belong to the Tk root that made them
        _resolve_fonts(self.root)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG_BASE)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _center_top(self) -> None:
        w = max(self.PANEL_W, self.root.winfo_reqwidth())
        sw = self.root.winfo_screenwidth()
        self.root.geometry(f"+{max(0, (sw - w) // 2)}+22")

    def _start_drag(self, event: tk.Event[Any]) -> None:
        self._drag_origin = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _on_drag(self, event: tk.Event[Any]) -> None:
        nx = event.x_root - self._drag_origin[0]
        ny = event.y_root - self._drag_origin[1]
        self.root.geometry(f"+{nx}+{ny}")

    def _bind_drag(self, widget: tk.Widget) -> None:
        widget.bind("<ButtonPress-1>", self._start_drag)
        widget.bind("<B1-Motion>", self._on_drag)

    STALE_S = 120.0
    AUTO_DONE_S = 12.0

    def _build_ui(self) -> None:
        inner_w = self.PANEL_W - 2 * self.SHELL_PADX
        # One text axis: the seal sits in the margin, every line of text below starts where the command starts
        self._text_x = 22 + 10
        self._col_w = inner_w - self._text_x

        # Progress hairline on the very top edge (invisible at rest)
        self.pline = ProgressLine(self.root, width=self.PANEL_W)
        self.pline.pack(fill="x", side="top")
        self._bind_drag(self.pline)

        self.shell = tk.Frame(self.root, bg=GLASS, padx=self.SHELL_PADX, pady=9, width=self.PANEL_W)
        self.shell.pack(fill="both", expand=True)

        # ---- Collapsed: seal and a word ----
        self.mini_bar = tk.Frame(self.shell, bg=GLASS, cursor="hand2")
        self.mini_orb = StatusSeal(self.mini_bar, size=18, cursor="hand2")
        self.mini_orb.pack(side="left", padx=(0, 7))
        self.mini_label = tk.Label(self.mini_bar, text=_track("Chrome Pilot"), bg=GLASS, fg=FG_SOFT, font=(UI_FONT, 8), cursor="hand2")
        self.mini_label.pack(side="left")
        mini_expand = IconButton(self.mini_bar, "expand", self.toggle_collapse, size=24)
        mini_expand.pack(side="left", padx=(8, 0))
        for w in (self.mini_bar, self.mini_label, self.mini_orb):
            w.bind("<Button-1>", lambda _e: self.toggle_collapse())

        self.main_wrap = tk.Frame(self.shell, bg=GLASS)
        self.main_wrap.pack(fill="x", expand=True)
        tk.Frame(self.main_wrap, bg=GLASS, width=inner_w, height=1).pack(fill="x")

        # ---- The line: seal, command, action. This is all that is always there. ----
        self.cmd_row = tk.Frame(self.main_wrap, bg=GLASS)
        self.cmd_row.pack(fill="x")

        self.orb = StatusSeal(self.cmd_row, size=22, cursor="fleur")
        self.orb.pack(side="left", padx=(0, 10))
        self._bind_drag(self.orb)

        btn_w = max(56, tkfont.Font(root=self.root, family=UI_FONT, size=9, weight="bold").measure("执行") + 24)
        icon_w = 26
        self._in_w = inner_w - 22 - 10 - 14 - btn_w - 8 - 3 * icon_w
        entry_font = tkfont.Font(root=self.root, family=UI_FONT, size=11)
        entry_h = entry_font.metrics("linespace")
        self._in_h = max(34, entry_h + 7)
        self._rule_y = self._in_h - 3
        self._rule_frac = 0.0
        self._rule_after: Optional[str] = None
        self.input_canvas = tk.Canvas(self.cmd_row, width=self._in_w, height=self._in_h, bg=GLASS, highlightthickness=0, bd=0)
        self.input_canvas.pack(side="left", padx=(0, 14))
        self.input_canvas.create_line(0, self._rule_y, self._in_w, self._rule_y, fill=FG_DIM)
        self._focus_rule_id = self.input_canvas.create_line(0, self._rule_y, 0, self._rule_y, fill=ACCENT, state="hidden")

        self.prompt_var = tk.StringVar()
        self.entry = tk.Entry(
            self.input_canvas,
            textvariable=self.prompt_var,
            bg=GLASS,
            fg=FG_FAINT,
            disabledbackground=GLASS,
            disabledforeground=FG_FAINT,
            insertbackground=FG,
            insertwidth=1,
            selectbackground="#2F4E7E",
            selectforeground=FG,
            bd=0,
            highlightthickness=0,
            font=entry_font,
        )
        self.input_canvas.create_window(0, (self._rule_y - 2 - entry_h) // 2, anchor="nw", width=self._in_w, height=entry_h, window=self.entry)
        self.entry.insert(0, PLACEHOLDER_TEXT)
        self.entry.bind("<FocusIn>", self._on_entry_focus_in)
        self.entry.bind("<FocusOut>", self._on_entry_focus_out)
        self.entry.bind("<Return>", lambda _e: self.on_submit())
        self.entry.bind("<Escape>", lambda _e: self._on_escape())

        # The one solid surface: a light action block with dark type
        self.run_btn = InkButton(self.cmd_row, "执行", self.on_submit, width=btn_w, height=28)
        self.run_btn.pack(side="left", padx=(0, 8))

        # Window controls stay dim until the pointer is over the bar
        self.gear_btn = IconButton(self.cmd_row, "tune", self.toggle_settings, size=icon_w, tip="设置")
        self.min_btn = IconButton(self.cmd_row, "minus", self.toggle_collapse, size=icon_w, tip="收起")
        self.close_btn = IconButton(self.cmd_row, "close", self.close, size=icon_w, hover_fg=BAD, tip="退出")
        for btn in (self.gear_btn, self.min_btn, self.close_btn):
            btn.pack(side="left")
            btn.set_dim(True)

        # ---- Status: a single line on the text axis, present only while there is something to say ----
        # Everything on demand lives in a tray whose height eases open and shut; its content is always the truth
        # (a Tk frame cannot request a height of 0, so the tray is unpacked whenever it is fully shut)
        self.tray = tk.Frame(self.main_wrap, bg=GLASS, height=1)
        self.tray.pack_propagate(False)
        self.tray_inner = tk.Frame(self.tray, bg=GLASS)
        self.tray_inner.place(x=0, y=0, relwidth=1.0)
        self._tray_h = 0
        self._tray_goal = 0
        self._tray_tween = Tween(self.root)
        self._status_tween = Tween(self.root)
        self._alpha_tween = Tween(self.root)

        self.status_frame = tk.Frame(self.tray_inner, bg=GLASS)
        self._status_font = tkfont.Font(root=self.root, family=UI_FONT, size=9)
        self._meta_font = tkfont.Font(root=self.root, family=UI_FONT, size=8)
        self.meta_label = tk.Label(self.status_frame, text="", bg=GLASS, fg=FG_FAINT, font=self._meta_font, bd=0, padx=0)
        self.meta_label.pack(side="right")
        self.status_label = tk.Label(self.status_frame, text="", bg=GLASS, fg=FG, anchor="w", font=self._status_font, bd=0, padx=0)
        self.status_label.pack(side="left", padx=(self._text_x, 0))
        self.status_label.bind("<Button-1>", lambda _e: self._dismiss_status())
        Tooltip(self.status_label, lambda: self._status_full if self._status_truncated else "")
        self._bind_drag(self.status_frame)
        self._bind_drag(self.meta_label)

        # ---- Confirmation (built once, shown only when an action is actually waiting for the human) ----
        # A pigment bar in the margin, why + what on the text axis, the target crop on the right, the two answers below.
        self.confirm_frame = tk.Frame(self.tray_inner, bg=GLASS)
        self.confirm_bar = tk.Frame(self.confirm_frame, bg=WARN, width=2)
        self.confirm_bar.pack(side="left", fill="y", padx=(10, self._text_x - 12))
        confirm_body = tk.Frame(self.confirm_frame, bg=GLASS)
        confirm_body.pack(side="left", fill="both", expand=True)
        self.preview_label = tk.Label(confirm_body, bg=GLASS, bd=0, highlightthickness=0)
        self._confirm_text = tk.Frame(confirm_body, bg=GLASS)
        self._confirm_text.pack(side="left", fill="both", expand=True)
        head = tk.Frame(self._confirm_text, bg=GLASS)
        head.pack(fill="x", anchor="n")
        self.confirm_icon = tk.Label(head, bg=GLASS, bd=0)
        self.confirm_title = tk.Label(head, text="", bg=GLASS, fg=FG, anchor="nw", font=(UI_FONT, 10, "bold"), justify="left", bd=0, padx=0)
        self.confirm_title.pack(side="left", fill="x", expand=True)
        self.confirm_detail = tk.Label(self._confirm_text, text="", bg=GLASS, fg=FG_SOFT, anchor="nw", font=(UI_FONT, 9), justify="left", bd=0, padx=0)
        btn_row = tk.Frame(self._confirm_text, bg=GLASS)
        btn_row.pack(side="bottom", anchor="w", pady=(10, 0))
        self.confirm_yes = InkButton(btn_row, "确认执行", self.on_confirm_yes, width=84, height=28)
        self.confirm_yes.pack(side="left")
        self.confirm_no = InkButton(btn_row, "取消", self.on_confirm_cancel, width=52, height=28, fill_hex=FG_SOFT, hover_hex=FG, ghost=True)
        self.confirm_no.pack(side="left", padx=(8, 0))

        # ---- Settings: one line of self-naming dropdowns and three icon switches ----
        self.settings_frame = tk.Frame(self.tray_inner, bg=GLASS)
        self._build_settings_drawer()

        self.root.bind("<Enter>", self._on_pointer_cross, add="+")
        self.root.bind("<Leave>", self._on_pointer_cross, add="+")
        self.root.bind("<Button-1>", self._on_any_click, add="+")

    MODE_SHORT = {"auto": "高危时确认", "confirm": "每步确认", "preview": "仅预览"}

    def _build_settings_drawer(self) -> None:
        col = tk.Frame(self.settings_frame, bg=GLASS)
        col.pack(fill="x", padx=(self._text_x, 0))
        r1 = tk.Frame(col, bg=GLASS)
        r1.pack(fill="x")
        self._mcp_peer_text = ""
        name, quant = self._split_model(self.model_choice)

        self.target_dd = Dropdown(
            r1, self._target_rows, self._target_key(), self._choose_target,
            display=lambda key: "后台托管" if key == "managed" else ("全桌面" if isinstance(key, tuple) and key[0] == "os" else "本机 Chrome"),
            tip=self._target_tip,
        )
        self.mode_dd = Dropdown(
            r1,
            lambda: [("auto", "自动执行，高危时先确认"), ("confirm", "每一步都先确认"), ("preview", "仅预览，不操作")],
            self.exec_mode, self._set_exec_mode, display=self.MODE_SHORT.get,
        )
        self.model_dd = Dropdown(r1, self._model_rows, name, self._choose_model_name, tip="本地模型")
        self.quant_dd = Dropdown(r1, self._quant_rows, quant, self._choose_quant, display=lambda q: str(q).split("_")[0], tip="量化")
        dropdowns = (self.target_dd, self.mode_dd, self.model_dd, self.quant_dd)
        for dd in dropdowns:
            dd.pack(side="left", padx=(0, 18))

        # At large text scaling the three switches drop to their own line instead of widening the window
        fits = sum(int(dd.cget("width")) + 18 for dd in dropdowns) + 3 * 24 + 24 <= self._col_w
        icons = r1 if fits else tk.Frame(col, bg=GLASS)
        if not fits:
            icons.pack(fill="x", pady=(2, 0))
        side = "right" if fits else "left"
        self.mcp_btn = IconButton(icons, "link", self._open_mcp_menu, size=24, fg=FG_FAINT, tip=lambda: "接入上层模型" + (f"（已接入 {self._mcp_peer_text}）" if self._mcp_peer_text else ""))
        self.effects_btn = IconButton(icons, "spark", self._toggle_effects, size=24, fg=FG_FAINT, tip=lambda: f"屏幕特效：{'开' if self.effects_enabled else '关'}")
        self.sound_btn = IconButton(icons, "bell", self._toggle_sound, size=24, fg=FG_FAINT, tip=lambda: f"提示音：{'开' if self.sound_enabled else '关'}")
        for btn in ((self.mcp_btn, self.effects_btn, self.sound_btn) if fits else (self.sound_btn, self.effects_btn, self.mcp_btn)):
            btn.pack(side=side)

    @staticmethod
    def _split_model(label: str) -> tuple[str, str]:
        name, _sep, quant = label.partition(" · ")
        return name, quant or "Q4_K_M"

    def _model_options(self) -> list[str]:
        return available_model_options() or ["M3.5 · Q4_K_M"]

    def _model_rows(self) -> list[tuple[Any, str]]:
        return [(n, n) for n in dict.fromkeys(self._split_model(o)[0] for o in self._model_options())]

    def _quant_rows(self) -> list[tuple[Any, str]]:
        name = self._split_model(self.model_choice)[0]
        return [(q, q) for n, q in map(self._split_model, self._model_options()) if n == name]

    def _choose_model_name(self, name: str) -> None:
        quants = [q for n, q in map(self._split_model, self._model_options()) if n == name]
        current = self._split_model(self.model_choice)[1]
        self._set_model(f"{name} · {current if current in quants else (quants[0] if quants else current)}")

    def _choose_quant(self, quant: str) -> None:
        self._set_model(f"{self._split_model(self.model_choice)[0]} · {quant}")

    def _target_key(self) -> Any:
        if self.target_mode == "os":
            return ("os", self.selected_hwnd)
        return ("desktop", self.selected_hwnd) if self.target_mode == "desktop" and self.selected_hwnd else "managed"

    def _target_tip(self) -> str:
        if self.target_mode == "os":
            return f"全桌面模式：{self.engine.active_title or '主屏'}"
        if self.target_mode != "desktop":
            return "独立的后台 Chrome，不动你的窗口"
        cur = next((w for w in self.desktop_windows if w.hwnd == self.selected_hwnd), None)
        return cur.title if cur is not None and cur.title else "你正在用的、已登录的 Chrome"

    def _target_rows(self) -> list[tuple[Any, str]]:
        """Background Chrome, OS full-screen mode, and open desktop Chrome windows."""
        self._refresh_desktop_windows(silent=True)
        rows: list[tuple[Any, str]] = [("managed", "后台托管 Chrome"), (("os", 0), "全桌面主屏（任意应用）"), (None, "")]
        if not self.desktop_windows:
            return rows + [(None, "未找到桌面 Chrome 窗口")]
        rows.append((None, "本机 Chrome 窗口"))
        for w in self.desktop_windows:
            title = w.title or "Google Chrome"
            rows.append((("desktop", w.hwnd), title if len(title) <= 34 else title[:33].rstrip() + "…"))
        return rows

    def _choose_target(self, key: Any) -> None:
        if key == "managed":
            self._set_target_mode("managed")
        elif isinstance(key, tuple) and key[0] == "os":
            self.selected_hwnd = int(key[1])
            self._set_target_mode("os")
        else:
            self.selected_hwnd = int(key[1])
            self._set_target_mode("desktop")

    def _open_mcp_menu(self) -> None:
        """Technical details live here, on demand: endpoint, who is connected, and the two config hand-overs."""
        if PopupMenu.toggled_off(self.mcp_btn):
            return
        ok = self.mcp_server is not None
        rows: list[tuple[Any, str]] = [(None, self.mcp_url.removeprefix("http://"))]
        if ok:
            rows.append((None, f"已接入 {self._mcp_peer_text}" if self._mcp_peer_text else "等待上层模型接入"))
        rows += [(None, ""), ("http", "复制 HTTP 配置"), ("stdio", "复制 Stdio 配置")]
        PopupMenu(self.mcp_btn, rows, None, self._copy_mcp, align="right")

    def _toggle_sound(self) -> None:
        self.set_sound_enabled(not self.sound_enabled)
        if self.sound_enabled:
            self.sound.play("done", True)

    def _toggle_effects(self) -> None:
        self.set_effects_enabled(not self.effects_enabled)

    def _on_any_click(self, event: tk.Event[Any]) -> None:
        """A click anywhere else in the bar closes an open dropdown (the opening click itself is spared)."""
        menu = PopupMenu.current
        if menu is not None and event.widget is not menu.anchor and time.monotonic() - menu.opened_at > 0.15:
            menu.close()

    def _sync_drawer_styles(self) -> None:
        self.target_dd._active = self._target_key()
        self.target_dd._redraw()
        self.mode_dd.set_value(self.exec_mode)
        name, quant = self._split_model(self.model_choice)
        self.model_dd.set_value(name)
        self.quant_dd.set_value(quant)
        self.sound_btn.set_kind("bell" if self.sound_enabled else "bell_off")
        self.effects_btn.set_kind("spark" if self.effects_enabled else "spark_off")

    def show_confirm_card(self, summary: str, crop_img: Optional[Image.Image], warning: bool = False, detail: str = "") -> None:
        """Ask the human, in place of the status line: why it stopped, what will happen, where — then the two answers."""
        self._set_dot(AMBER)
        self._cancel_status_timer()
        self._status_full, self._status_visible = "", False
        PopupMenu.dismiss()
        self.confirm_bar.configure(bg=WARN if warning else FG_DIM)
        wrap = self._col_w - (26 if warning else 0)
        if crop_img is not None:
            thumb = self._framed_thumb(crop_img, warning)
            self._preview_photo = ImageTk.PhotoImage(thumb)
            self.preview_label.configure(image=self._preview_photo, text="")
            self.preview_label.pack(side="right", anchor="n", padx=(14, 0), before=self._confirm_text)
            wrap -= thumb.size[0] + 14
        else:
            self.preview_label.pack_forget()
        self.confirm_title.configure(text=_brief_reason(summary), fg=WARN if warning else FG, wraplength=max(120, wrap))
        if detail:
            self.confirm_detail.configure(text=" ".join(detail.split()), wraplength=max(120, wrap + (26 if warning else 0)))
            self.confirm_detail.pack(fill="x", pady=(3, 0), after=self.confirm_title.master)
        else:
            self.confirm_detail.pack_forget()
        if warning:
            self._confirm_icon_photo = _render_icon("warn", 20, WARN)
            self.confirm_icon.configure(image=self._confirm_icon_photo)
            self.confirm_icon.pack(side="left", anchor="n", padx=(0, 6), before=self.confirm_title)
            self.confirm_yes.configure_style(fill_hex=WARN, hover_hex="#FFCE85")
        else:
            self.confirm_icon.pack_forget()
            self.confirm_yes.configure_style(fill_hex=FG, hover_hex=ACCENT)
        self._confirm_visible = True
        self._relayout()

    @staticmethod
    def _framed_thumb(crop_img: Image.Image, warning: bool, max_size: tuple[int, int] = (168, 94), radius: int = 8) -> Image.Image:
        """Small rounded plate of the target area with one hairline edge."""
        thumb = crop_img.convert("RGB").copy()
        thumb.thumbnail(max_size, Image.Resampling.LANCZOS)
        s = _SS
        w, h = thumb.size
        box = [0, 0, w * s - 1, h * s - 1]
        out = Image.new("RGB", (w * s, h * s), _hex_to_rgb(GLASS))
        mask = Image.new("L", (w * s, h * s), 0)
        ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius * s, fill=255)
        out.paste(thumb.resize((w * s, h * s), Image.Resampling.BICUBIC), (0, 0), mask)
        ImageDraw.Draw(out).rounded_rectangle(box, radius=radius * s, outline=_hex_to_rgb(_mix(GLASS, WARN, 0.6) if warning else "#4C4C54"), width=s)
        return out.resize((w, h), Image.Resampling.LANCZOS)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        if busy:
            self.run_btn.configure_style(text="停止", fill_hex=BAD, hover_hex="#FF9087")
            self.entry.configure(state="disabled")
        else:
            self.run_btn.configure_style(text="执行", fill_hex=FG, hover_hex=ACCENT)
            self.entry.configure(state="normal")

    def _copy_mcp(self, kind: str) -> None:
        if kind == "http":
            port = int(getattr(self.args, "mcp_port", 18390))
            pyperclip.copy(client_config("http", url=self.mcp_url if self.mcp_server is not None else f"http://127.0.0.1:{port}/mcp"))
        else:
            pyperclip.copy(client_config("stdio"))
        self.show_status("已复制", TEXT_SECONDARY, ttl_ms=2000)

    def _refresh_desktop_windows(self, silent: bool = False) -> None:
        managed_hwnd = getattr(getattr(self, "engine", None), "chrome", None)
        managed_id = managed_hwnd.hwnd if managed_hwnd is not None else 0
        self.desktop_windows = [w for w in find_chrome_windows() if not managed_id or w.hwnd != managed_id]
        if self.target_mode == "os":
            return
        if not self.desktop_windows:
            self.selected_hwnd = 0
            if not silent:
                self.show_status("未找到桌面 Chrome", AMBER, ttl_ms=3500)
            return
        cur = next((w for w in self.desktop_windows if w.hwnd == self.selected_hwnd), self.desktop_windows[0])
        self.selected_hwnd = cur.hwnd
        if self.target_mode == "desktop":
            self.engine.desktop_hwnd = self.selected_hwnd

    def _relayout(self) -> None:
        """Fixed reading order under the command line: status, confirmation, settings. A pending confirmation replaces
        the status line. The tray eases to the new height; when it closes completely the old content rides up with it."""
        key = (self._status_visible and not self._confirm_visible, self._confirm_visible, self.settings_open)
        frames = (self.status_frame, self.confirm_frame, self.settings_frame)
        pads = ((4, 0), (12, 3), (10, 1))

        def repack() -> None:
            for frame in frames:
                frame.pack_forget()
            for frame, show, pady in zip(frames, self._layout_key or key, pads):
                if show:
                    frame.pack(fill="x", pady=pady)

        closing = not any(key) and self._layout_key is not None and any(self._layout_key) and MOTION_SCALE > 0.0
        changed = key != self._layout_key
        self._layout_key = key
        if changed and not closing:
            repack()
        self.tray_inner.update_idletasks()
        start, goal = self._tray_h, (self.tray_inner.winfo_reqheight() if any(key) else 0)
        if not changed and goal == self._tray_goal:
            return
        self._tray_goal = goal

        def frame(k: float) -> None:
            self._tray_h = round(start + (goal - start) * k)
            if self._tray_h > 0:
                self.tray.configure(height=self._tray_h)
                if not self.tray.winfo_manager():
                    self.tray.pack(fill="x")
            else:
                self.tray.pack_forget()

        def done() -> None:
            if closing:
                repack()

        # A question for the human opens fastest; nothing here ever delays a click (content is live from frame one)
        self._tray_tween.run(120 if key[1] else 170, frame, done)

    def _on_pointer_cross(self, _e: object = None) -> None:
        if self._hover_after is None and not self._closed:
            self._hover_after = self.root.after(80, self._sync_hover)

    def _sync_hover(self) -> None:
        self._hover_after = None
        if self._closed:
            return
        try:
            px, py = self.root.winfo_pointerxy()
            x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
            inside = x <= px < x + self.root.winfo_width() and y <= py < y + self.root.winfo_height()
            for btn in (self.gear_btn, self.min_btn, self.close_btn):
                btn.set_dim(not inside)
        except tk.TclError:
            pass

    def _render_status(self) -> None:
        """Fit the message to one line beside the meta note; the full text stays available on hover."""
        meta = " · ".join(p for p in (*self._meta_parts, self._meta_elapsed) if p)
        self.meta_label.configure(text=meta)
        avail = self._col_w - (self._meta_font.measure(meta) + 18 if meta else 0)
        text, cut = self._status_full, False
        if self._status_font.measure(text) > avail:
            cut = True
            while text and self._status_font.measure(text + "…") > avail:
                text = text[:-1]
            text = text.rstrip() + "…"
        self._status_truncated = cut
        self._status_meta_len = len(meta)
        self.status_label.configure(text=text, fg={DANGER: BAD, AMBER: WARN}.get(self._status_tone, FG))

    def _cancel_status_timer(self) -> None:
        if self._status_after is not None:
            try:
                self.root.after_cancel(self._status_after)
            except Exception:
                pass
            self._status_after = None

    def show_status(self, text: str, dot_color: str = TEXT_SECONDARY, ttl_ms: Optional[int] = -1, keep_meta: bool = False) -> None:
        """Say one thing on the status line.

        ttl_ms=-1 picks by tone: working / needs-you / failed lines stay until replaced or clicked away,
        results leave after 5 s, plain notices after 3.5 s. ttl_ms=None always stays.
        """
        self._set_dot(dot_color)
        self._cancel_status_timer()
        if not keep_meta:
            self._meta_parts, self._meta_elapsed = ("", ""), ""
        changed = " ".join(str(text).split()) != self._status_full
        self._status_full = " ".join(str(text).split())
        self._status_tone = dot_color
        self._status_visible = bool(self._status_full)
        self._render_status()
        self._relayout()
        if changed and self._status_visible:
            ink = str(self.status_label.cget("fg"))
            self._status_tween.run(160, lambda k: self.status_label.configure(fg=_mix(GLASS, ink, 0.2 + 0.8 * k)))
        if ttl_ms == -1:
            ttl_ms = None if dot_color in {CYAN, AMBER, DANGER} else (5000 if dot_color == EMERALD else 3500)
        if ttl_ms and self._status_visible:
            self._status_after = self.root.after(ttl_ms, self._clear_status)

    def _clear_status(self) -> None:
        """Fold the status line away and return to the resting bar."""
        self._cancel_status_timer()
        if self._closed:
            return
        self._status_full, self._status_visible, self._status_truncated = "", False, False
        self._meta_parts, self._meta_elapsed = ("", ""), ""
        try:
            self._relayout()
            if not (self.busy or self.pending_desktop or self.pending_managed or self.engine.task_state == "running"):
                self._set_dot(EMERALD)
                self.mini_label.configure(text=_track("Chrome Pilot"))
        except tk.TclError:
            pass

    def _dismiss_status(self) -> None:
        if not self.busy and self.engine.task_state != "running":
            self._clear_status()

    def _tick_elapsed(self) -> None:
        self._timer_after_id = None
        if self._closed or self.engine.task_state != "running" or self.engine.task_started_at <= 0.0:
            return
        now = time.monotonic()
        if not self.busy and not getattr(self.engine, "task_explicit", False) and (now - self.engine.task_last_activity) >= self.AUTO_DONE_S:
            self.engine.complete_auto_task(message="任务已完成", play_sound=True)
            return
        if not self.busy and now - self.engine.task_last_activity > self.STALE_S:
            self._quiet_stale_task()
            return
        try:
            self._meta_elapsed = f"{max(0.0, now - self.engine.task_started_at):.1f}s"
            meta = " · ".join(p for p in (*self._meta_parts, self._meta_elapsed) if p)
            if len(meta) != self._status_meta_len:
                self._render_status()
            else:
                self.meta_label.configure(text=meta)
        except tk.TclError:
            return
        self._schedule_elapsed_tick()

    def _quiet_stale_task(self) -> None:
        """The harness has been silent for STALE_S: stop presenting its task as live (view only; engine state is untouched)."""
        self._status_full, self._status_visible, self._status_truncated = "", False, False
        self._meta_parts, self._meta_elapsed = ("", ""), ""
        self._cancel_status_timer()
        try:
            self._relayout()
            self._set_dot(EMERALD)
            self.mini_label.configure(text=_track("Chrome Pilot"))
            self.pline.set_state("idle")
            self.orb.set_animated(False)
            self.mini_orb.set_animated(False)
            set_win11_border_color(self.root, BORDER_DEFAULT)
            self.effects.deactivate()
        except tk.TclError:
            pass

    def apply_telemetry_snapshot(
        self,
        st: dict[str, Any],
        message: Optional[str] = None,
        dot_color: Optional[str] = None,
    ) -> None:
        """Render a task snapshot: message on the left, `HARNESS · 3/5 · 12.4s` on the right, nothing at all when idle."""
        if self._closed:
            return
        tmode = st.get("target_mode")
        if tmode in {"managed", "desktop", "os"} and hasattr(self, "target_dd"):
            dhwnd = int(st.get("desktop_hwnd") or 0)
            if tmode != self.target_mode or (tmode in {"desktop", "os"} and dhwnd != self.selected_hwnd):
                self.target_mode = str(tmode)
                if tmode in {"desktop", "os"}:
                    self.selected_hwnd = dhwnd
                try:
                    self.target_dd.set_value(self._target_key())
                except Exception:
                    pass
        active_harness = str(st.get("active_harness") or "")
        harness_mode = str(st.get("harness_mode") or "none")
        clients = int(st.get("mcp_clients", 0))
        tstate = str(st.get("task_state") or "idle")
        step = int(st.get("task_step", 0))
        total = int(st.get("task_total_steps", 0))
        elapsed_s = float(st.get("task_elapsed_s", 0.0))
        step_label = str(st.get("task_step_label") or "")

        if harness_mode == "multi" or clients > 1:
            harness_txt = f"{clients} 接入"
        else:
            harness_txt = _track(active_harness) if active_harness else ""
        self._mcp_peer_text = harness_txt
        self.mcp_btn.set_active(bool(harness_txt))
        name = harness_txt or _track("Chrome Pilot")
        elapsed_txt = f"{elapsed_s:.1f}s" if elapsed_s > 0 else ""

        if tstate != "running":
            self._pline_progress = None
            self._stop_elapsed_tick()
        ttl: Optional[int] = None
        if tstate == "running":
            step_txt = f"{step}/{total}" if total > 0 else (f"{step} 步" if step > 0 else "")
            tone, mini_txt = CYAN, " · ".join(p for p in (name, step_txt) if p)
            elapsed_txt = f"{elapsed_s:.1f}s"
            self._pline_progress = (min(step, total) / total) if total > 0 else None
            if self.pline.state == "running":
                self.pline.set_state("running", self._pline_progress)
            if self.engine.task_state == "running" and self.engine.task_started_at > 0.0:
                self._schedule_elapsed_tick()
        elif tstate == "done":
            step_txt, tone, mini_txt, ttl = (f"{step} 步" if step > 0 else ""), EMERALD, f"{name} · 完成", 5000
        elif tstate == "confirm":
            step_txt, tone, mini_txt = "", AMBER, f"{name} · 待确认"
        elif tstate == "error":
            step_txt, tone, mini_txt = "", DANGER, f"{name} · 已中断"
        else:
            harness_txt, step_txt, elapsed_txt, tone, mini_txt, ttl = "", "", "", EMERALD, name, 3500

        display_msg = message if message is not None else (step_label or str(st.get("last_message") or ""))
        if tstate == "idle" and display_msg in {"", "就绪"}:
            self._clear_status()
        else:
            self._meta_parts, self._meta_elapsed = (harness_txt, step_txt), elapsed_txt
            self.show_status(display_msg, dot_color or tone, ttl_ms=ttl, keep_meta=True)
        self.mini_label.configure(text=mini_txt)
        if tstate in {"running", "done", "confirm", "error"}:
            self._apply_pline_for({"running": CYAN, "done": EMERALD, "confirm": AMBER, "error": DANGER}[tstate])

    def _hide_confirm(self) -> None:
        self.pending_desktop = None
        self.pending_managed = None
        self._confirm_visible = False
        self._relayout()

    def toggle_settings(self) -> None:
        PopupMenu.dismiss()
        self.settings_open = not self.settings_open
        self.gear_btn.set_active(self.settings_open)
        self._relayout()

    def _set_target_mode(self, mode: str) -> None:
        self.target_mode = mode
        self._refresh_desktop_windows(silent=True)
        try:
            hwnd_arg = self.selected_hwnd if (mode == "desktop" and self.selected_hwnd) or mode == "os" else None
            self.engine.set_target_mode(mode, hwnd_arg)
        except Exception:
            self.engine.target_mode = mode
        self._sync_drawer_styles()

    def _set_model(self, model_label: str) -> None:
        self.model_choice = model_label
        self.engine.grounder.set_model(model_label)
        self._sync_drawer_styles()

    def set_sound_enabled(self, enabled: bool) -> None:
        self.sound_enabled = bool(enabled)
        self._sync_drawer_styles()

    def set_effects_enabled(self, enabled: bool) -> None:
        self.effects_enabled = bool(enabled)
        self.effects.set_enabled(self.effects_enabled)
        self._sync_drawer_styles()
        if self.effects_enabled and not self.busy and self.engine.task_state != "running":
            self.effects.preview_brief(1.6)

    def _animate_focus_rule(self, target: float) -> None:
        """Ink the command rule from the left on focus, withdraw it on blur."""
        if self._rule_after is not None:
            try:
                self.root.after_cancel(self._rule_after)
            except Exception:
                pass
            self._rule_after = None

        def step() -> None:
            self._rule_after = None
            if self._closed:
                return
            self._rule_frac += (target - self._rule_frac) * 0.34
            if abs(target - self._rule_frac) < 0.012:
                self._rule_frac = target
            try:
                x1 = round(self._in_w * self._rule_frac)
                self.input_canvas.coords(self._focus_rule_id, 0, self._rule_y, x1, self._rule_y)
                self.input_canvas.itemconfig(self._focus_rule_id, state="normal" if x1 > 0 else "hidden")
                if self._rule_frac != target:
                    self._rule_after = self.root.after(16, step)
            except tk.TclError:
                pass

        step()

    def _on_entry_focus_in(self, _e: object) -> None:
        self._animate_focus_rule(1.0)
        self._clear_placeholder(_e)

    def _on_entry_focus_out(self, _e: object) -> None:
        self._animate_focus_rule(0.0)
        self._restore_placeholder(_e)

    def _set_panel_hidden_for_io(self, hidden: bool) -> None:
        if self._closed:
            return

        def _apply() -> None:
            if self._closed:
                return
            try:
                if hidden:
                    self.root.withdraw()
                else:
                    self.root.deiconify()
                    if self.glass:
                        apply_glass_material(self.root)
            except tk.TclError:
                pass

        if threading.get_ident() == self._ui_thread_id:
            _apply()
            return
        done = threading.Event()

        def _run_and_signal() -> None:
            try:
                _apply()
            finally:
                done.set()

        try:
            self.root.after(0, _run_and_signal)
            done.wait(timeout=0.5)
        except tk.TclError:
            pass

    def _set_exec_mode(self, mode: str) -> None:
        self.exec_mode = mode
        self._sync_drawer_styles()
        if self.pending_desktop or self.pending_managed:
            self._hide_confirm()
            self._flash_border(BORDER_DEFAULT, 0)
            self._clear_status()

    def _clear_placeholder(self, _e: object) -> None:
        if self._has_placeholder:
            self.entry.delete(0, "end")
            self.entry.configure(fg=TEXT_PRIMARY)
            self._has_placeholder = False

    def _restore_placeholder(self, _e: object) -> None:
        if not self.entry.get().strip():
            self._has_placeholder = True
            self.entry.delete(0, "end")
            self.entry.insert(0, PLACEHOLDER_TEXT)
            self.entry.configure(fg=TEXT_MUTED)

    def _get_instruction(self) -> str:
        if self._has_placeholder:
            return ""
        return self.prompt_var.get().strip()

    def _set_dot(self, color: str) -> None:
        self.orb.set_color(color)
        self.mini_orb.set_color(color)

    def _on_escape(self) -> None:
        if PopupMenu.current is not None:
            PopupMenu.dismiss()
            return
        if self.busy:
            self.engine.request_abort()
            self.effects.deactivate()
            self.show_status("正在中止…", DANGER)
            return
        if self.pending_desktop or self.pending_managed:
            self.on_confirm_cancel()
            return
        if self.settings_open:
            self.toggle_settings()

    def _flash_border(self, color: str, duration_ms: int = 2400) -> None:
        if self._closed:
            return
        if self._border_after_id is not None:
            try:
                self.root.after_cancel(self._border_after_id)
            except Exception:
                pass
            self._border_after_id = None
        set_win11_border_color(self.root, color)
        self._apply_pline_for(color)
        if duration_ms <= 0:
            return

        def _restore() -> None:
            self._border_after_id = None
            # A question that is still waiting for the human keeps its ochre pline and border
            if not self._closed and not (color == AMBER and (self.pending_desktop or self.pending_managed)):
                set_win11_border_color(self.root, BORDER_DEFAULT)
                self._apply_pline_for(BORDER_DEFAULT)

        self._border_after_id = self.root.after(duration_ms, _restore)

    def _apply_pline_for(self, color: str) -> None:
        """Map the semantic border color onto the Greek-key pline + seal motion."""
        state = {CYAN: "running", EMERALD: "done", AMBER: "confirm", DANGER: "error"}.get(color, "idle")
        if state == "idle" and (self.busy or self.engine.task_state == "running"):
            state = "running"
        progress = self._pline_progress if state == "running" else None
        try:
            self.pline.set_state(state, progress)
            animate = state in {"running", "confirm"}
            self.orb.set_animated(animate)
            self.mini_orb.set_animated(animate)
        except tk.TclError:
            pass

    def _schedule_elapsed_tick(self) -> None:
        if self._closed or self._timer_after_id is not None:
            return
        self._timer_after_id = self.root.after(250, self._tick_elapsed)

    def _stop_elapsed_tick(self) -> None:
        if self._timer_after_id is not None:
            try:
                self.root.after_cancel(self._timer_after_id)
            except Exception:
                pass
            self._timer_after_id = None

    def _enter(self) -> None:
        """Arrive: fade in while settling 10 px into place."""
        x, y = self.root.winfo_x(), self.root.winfo_y()
        self.root.attributes("-alpha", 0.0)
        last = [f"+{x}+{y}"]

        def frame(k: float) -> None:
            self.root.attributes("-alpha", self._alpha_rest * k)
            if self.root.geometry().endswith(last[0]):  # stop steering the moment anything else moves the window
                last[0] = f"+{x}+{y - round(10 * (1.0 - k))}"
                self.root.geometry(last[0])

        self._alpha_tween.run(220, frame)

    def toggle_collapse(self) -> None:
        """Swap bar and capsule behind a short dip in opacity so the change of width never shows as a jump."""
        PopupMenu.dismiss()
        self.collapsed = not self.collapsed

        def swap() -> None:
            if self.collapsed:
                self.main_wrap.pack_forget()
                self.mini_bar.pack()
                self.root.update_idletasks()
                self.pline.resize(self.mini_bar.winfo_reqwidth() + 2 * self.SHELL_PADX)
            else:
                self.mini_bar.pack_forget()
                self.main_wrap.pack(fill="x", expand=True)
                self.pline.resize(self.PANEL_W)
            self._alpha_tween.run(150, lambda k: self.root.attributes("-alpha", self._alpha_rest * (0.15 + 0.85 * k)))

        self._alpha_tween.run(70, lambda k: self.root.attributes("-alpha", self._alpha_rest * (1.0 - 0.85 * k)), swap)

    def _on_engine_status(self, st: dict[str, Any]) -> None:
        if self._closed:
            return
        kind = str(st.get("event_kind") or "status")
        play_sound = bool(st.get("play_sound", False))
        msg = str(st.get("last_message") or "")
        step_label = str(st.get("task_step_label") or "")
        if st.get("task_state") == "running" and step_label and kind in {"step_running", "task_start", "task_progress"}:
            display_msg = step_label
        elif msg and msg != "就绪":
            display_msg = msg
        else:
            display_msg = step_label or "就绪"
        try:
            self.root.after(0, lambda s=st, k=kind, p=play_sound, m=display_msg: self._handle_engine_event_ui(s, k, p, m))
        except tk.TclError:
            pass

    def _handle_engine_event_ui(self, st: dict[str, Any], kind: str, play_sound: bool, display_msg: str) -> None:
        if self._closed:
            return
        # Engine bookkeeping outside any task ("已获取画面…", "导航完成") is not worth a line of the user's attention
        chatter = kind == "status" and st.get("task_state") == "idle" and st.get("state") not in {"error", "confirm", "stopped"}
        if (not self.busy and not chatter) or kind in {"harness_connected", "harness_disconnected"}:
            self.apply_telemetry_snapshot(st, message=display_msg)

        if kind in {"step_running", "task_start", "task_progress"}:
            self.effects.activate()
            self._flash_border(CYAN, 0)
        elif kind == "harness_connected":
            self._flash_border(CYAN, 1800)
            if play_sound:
                self.sound.play("connect", self.sound_enabled)
        elif kind == "harness_disconnected":
            self.effects.deactivate()
            self._flash_border(BORDER_DEFAULT, 0)
        elif kind == "task_done":
            self.effects.pulse_complete()
            self._flash_border(EMERALD, 2500)
            if play_sound:
                self.sound.play("done", self.sound_enabled)
        elif kind == "task_alert":
            self.effects.pause_for_human()
            self._flash_border(AMBER, 2500)
            if play_sound:
                self.sound.play("alert", self.sound_enabled)
        elif kind == "task_error":
            self.effects.deactivate()
            self._flash_border(DANGER, 2500)
            if play_sound:
                self.sound.play("error", self.sound_enabled)

    @staticmethod
    def _extract_direct_url(inst: str) -> Optional[str]:
        s = inst.strip()
        if s.startswith(("http://", "https://")):
            return s
        if s.startswith(("打开 http://", "打开 https://")):
            return s[3:].strip()
        return None

    def on_submit(self) -> None:
        if self.busy:
            self.engine.request_abort()
            self.effects.deactivate()
            self.show_status("正在中止…", DANGER)
            return
        inst = self._get_instruction()
        if not inst:
            self.entry.focus_set()
            return
        self._hide_confirm()
        self.engine.reset_abort()
        self.engine.prewarm_async()
        self._set_busy(True)
        self.effects.activate()
        self._flash_border(CYAN, 0)

        direct_url = self._extract_direct_url(inst)
        if direct_url is not None:
            self._set_dot(CYAN)
            self.show_status("打开中…", CYAN)
            fut = self.worker.submit(lambda u=direct_url: self.engine.navigate("goto", url=u))
            self._watch(fut, self._on_managed_done)
            return

        if self.target_mode == "desktop":
            self._run_desktop(inst)
        else:
            self._set_dot(CYAN)
            self.show_status("执行中…", CYAN)
            fut = self.worker.submit(self._worker_managed, inst)
            self._watch(fut, self._on_managed_done)

    def _capture_confirm_frame(self) -> Image.Image:
        if self.engine._is_desktop_like():
            _, raw_img = self.engine._capture_desktop_window(focus=False)
            return raw_img
        _, raw_img = self.engine.chrome.capture_full_window()
        return raw_img

    def _worker_managed(self, inst: str) -> dict[str, Any]:
        self.engine.grounder.set_model(self.model_choice)
        url_match = inst.strip()
        if url_match.startswith(("http://", "https://")):
            return self.engine.navigate("goto", url=url_match)
        if url_match.startswith("打开 http://") or url_match.startswith("打开 https://"):
            return self.engine.navigate("goto", url=url_match[3:].strip())

        if self.exec_mode == "preview":
            return {"_preview": True, **self.engine.locate(inst, zoom=False)}
        if self.exec_mode == "confirm":
            loc = self.engine.locate(inst, zoom=False)
            if loc.get("action") in {None, "not_found"}:
                return loc
            raw_img = self._capture_confirm_frame()
            crop = self._crop_crosshair(raw_img, loc.get("coordinate"))
            return {"_need_confirm": True, "instruction": inst, "loc": loc, "crop": crop}
        res = self.engine.act(instruction=inst, strategy="hybrid", auto_heal=True)
        if res.get("status") == "needs_human_confirmation":
            raw_img = self._capture_confirm_frame()
            crop = self._crop_crosshair(raw_img, res.get("coordinate"))
            return {"_need_confirm": True, "instruction": inst, "red_tier": True, "res": res, "crop": crop}
        return res

    @staticmethod
    def _crop_crosshair(img: Image.Image, coord: list[int] | None) -> Optional[Image.Image]:
        if not coord:
            return None
        vis = img.copy()
        draw = ImageDraw.Draw(vis)
        w, h = vis.size
        px = round(coord[0] / 1000.0 * max(1, w - 1))
        py = round(coord[1] / 1000.0 * max(1, h - 1))
        draw_target_marker(draw, px, py)
        cw, ch = 260, 140
        left = max(0, min(px - cw // 2, w - cw))
        top = max(0, min(py - ch // 2, h - ch))
        return vis.crop((left, top, min(w, left + cw), min(h, top + ch)))

    def _on_managed_done(self, data: dict[str, Any]) -> None:
        if data.get("_need_confirm"):
            self.effects.pause_for_human()
            if data.get("red_tier"):
                res = data["res"]
                self.pending_managed = {"instruction": data["instruction"], "confirm_token": res["confirm_token"]}
                self.show_confirm_card(res["reason"], data.get("crop"), warning=True, detail=f"指令：{data['instruction']}")
            else:
                loc = data["loc"]
                parsed = ParsedAction(action=loc["action"], coordinate=tuple(loc["coordinate"]) if loc.get("coordinate") else None, raw={})
                self.pending_managed = {"instruction": data["instruction"], "confirm_token": None}
                self.show_confirm_card(f"即将执行：{_at_mark(format_human_action(parsed), data.get('crop'))}", data.get("crop"), warning=False)
            self._flash_border(AMBER, 2500)
            return

        st = data.get("status")
        if st == "aborted":
            self.effects.deactivate()
            self._flash_border(BORDER_DEFAULT, 0)
            self.show_status("已停止", TEXT_SECONDARY)
        elif st == "not_found" or data.get("action") == "not_found":
            self.effects.deactivate()
            self._flash_border(BORDER_DEFAULT, 0)
            self.show_status(data.get("reason") or "未找到目标", EMERALD)
        elif st == "needs_human_takeover":
            self.effects.pause_for_human()
            self._flash_border(AMBER, 2500)
            self.show_status(data.get("reason") or "遇到登录墙或验证码，请接管处理", AMBER)
        elif st == "ok":
            elapsed = data.get("elapsed_s") or data.get("infer_s")
            t_str = f" · {elapsed:.2f}s" if isinstance(elapsed, (int, float)) else ""
            heal_str = " (放大精修)" if data.get("auto_healed") else ""
            snap = self.engine.get_status()
            snap["task_state"] = "done"
            self.apply_telemetry_snapshot(snap, f"完成{heal_str}{t_str}", EMERALD)
            self.effects.pulse_complete()
            self._flash_border(EMERALD, 2200)
            self.sound.play("done", self.sound_enabled)
        else:
            self.effects.deactivate()
            self._flash_border(DANGER, 2200)
            self.show_status(data.get("reason") or data.get("exec_status") or f"状态：{st}", DANGER)

    def _run_desktop(self, inst: str) -> None:
        self._refresh_desktop_windows(silent=True)
        if not self.selected_hwnd:
            self._set_busy(False)
            self.effects.deactivate()
            self.show_status("未找到桌面 Chrome", AMBER, ttl_ms=3500)
            return
        self.show_status("识别中…", CYAN)
        self._launch_desktop_worker(self.selected_hwnd, inst)

    def _launch_desktop_worker(self, hwnd: int, inst: str) -> None:
        def work() -> dict[str, Any]:
            res = self.engine.predict_desktop_window(hwnd, inst, model_label=self.model_choice)
            if res.get("aborted") or self.engine.abort_event.is_set():
                return {"aborted": True, "auto_executed": False, **res}
            parsed = res["parsed"]
            info = res.get("info")
            frame = res.get("frame")
            gate = self.engine._check_desktop_title_gate(info.title) if info is not None else None
            if info is not None and frame is not None:
                policy, _hit_ocr = self.engine.evaluate_desktop_action_policy(
                    inst, info, frame, parsed, gate_reason=gate
                )
            else:
                policy = evaluate_policy(inst, gate_reason=gate)
            if parsed is not None and parsed.action not in {"not_found", "done"} and self.exec_mode == "auto" and policy.tier == "green":
                if self.engine.abort_event.is_set():
                    return {"aborted": True, "auto_executed": False, **res}
                ok, reason = self.engine.execute_desktop_pending(res["info"], res["frame"], parsed)
                if reason == "aborted_by_user":
                    return {"aborted": True, "auto_executed": False, "ok": False, "reason": reason, **res}
                return {"auto_executed": True, "ok": ok, "reason": reason, **res}
            return {"auto_executed": False, "policy": policy, **res}

        fut = self.worker.submit(work)
        self._watch(fut, self._on_desktop_done)

    def _on_desktop_done(self, data: dict[str, Any]) -> None:
        if data.get("aborted"):
            self.effects.deactivate()
            self._flash_border(BORDER_DEFAULT, 0)
            self.show_status("已停止", TEXT_SECONDARY)
            return
        parsed: Optional[ParsedAction] = data.get("parsed")
        elapsed = float(data.get("elapsed_s", 0.0))
        if parsed is None:
            self.effects.deactivate()
            self._flash_border(DANGER, 2200)
            self.show_status("模型输出无效，请重试", DANGER)
            return
        if parsed.action == "not_found":
            self.effects.deactivate()
            self._flash_border(BORDER_DEFAULT, 0)
            self.show_status(parsed.reason or f"未找到目标 · {elapsed:.2f}s", EMERALD)
            return
        if data.get("auto_executed"):
            ok = data.get("ok")
            msg = f"完成 · {elapsed:.2f}s" if ok else RESULT_MESSAGES.get(data.get("reason", ""), data.get("reason", "执行失败"))
            self.show_status(msg, EMERALD if ok else DANGER)
            if ok:
                self.effects.pulse_complete()
                self._flash_border(EMERALD, 2200)
                self.sound.play("done", self.sound_enabled)
            else:
                self.effects.deactivate()
                self._flash_border(DANGER, 2200)
            return
        summary = format_human_action(parsed)
        action = _at_mark(summary, data.get("preview_crop"))
        policy = data.get("policy")
        warning = bool(policy and policy.tier in {"red", "takeover"})
        if warning:
            summary = f"{_brief_reason(policy.reason)} · {summary}"
        if self.exec_mode == "preview":
            self.effects.deactivate()
            self._flash_border(BORDER_DEFAULT, 0)
            self.show_status(f"预览：{summary} · {elapsed:.2f}s", EMERALD)
            return
        self.effects.pause_for_human()
        self.pending_desktop = data
        self.show_confirm_card(policy.reason if warning else f"即将执行：{action}", data.get("preview_crop"), warning=warning, detail=f"将{action}" if warning else "")
        self._flash_border(AMBER, 2500)

    def on_confirm_yes(self) -> None:
        if self.busy:
            return
        if self.pending_managed:
            pm = self.pending_managed
            self._hide_confirm()
            self._set_busy(True)
            self.effects.activate()
            self._flash_border(CYAN, 0)
            self.show_status("执行中…", CYAN)
            fut = self.worker.submit(
                lambda: self.engine.act(instruction=pm["instruction"], confirm_token=pm["confirm_token"], strategy="hybrid", auto_heal=True)
            )
            self._watch(fut, self._on_managed_done)
            return
        if self.pending_desktop:
            pd = self.pending_desktop
            self._hide_confirm()
            self._set_busy(True)
            self.effects.activate()
            self._flash_border(CYAN, 0)

            def run_exec() -> tuple[bool, str]:
                return self.engine.execute_desktop_pending(pd["info"], pd["frame"], pd["parsed"])

            def done(res: tuple[bool, str]) -> None:
                ok, reason = res
                msg = "完成" if ok else RESULT_MESSAGES.get(reason, reason)
                self.show_status(msg, EMERALD if ok else DANGER)
                if ok:
                    self.effects.pulse_complete()
                    self._flash_border(EMERALD, 2200)
                    self.sound.play("done", self.sound_enabled)
                else:
                    self.effects.deactivate()
                    self._flash_border(DANGER, 2200)

            self._watch(self.worker.submit(run_exec), done)

    def on_confirm_cancel(self) -> None:
        self._hide_confirm()
        self.effects.deactivate()
        self._flash_border(BORDER_DEFAULT, 0)
        self.show_status("已取消", TEXT_SECONDARY)

    def _watch(self, fut: Future[Any], on_done: Callable[[Any], None]) -> None:
        if not fut.done():
            self.root.after(50, self._watch, fut, on_done)
            return
        self._set_busy(False)
        try:
            res = fut.result()
        except Exception as exc:
            self.root.deiconify()
            self.effects.deactivate()
            self._flash_border(DANGER, 2200)
            self.show_status(f"出错：{str(exc)[:80]}", DANGER)
            return
        on_done(res)

    def close(self) -> None:
        self._closed = True
        PopupMenu.dismiss()
        self._stop_elapsed_tick()
        self._cancel_status_timer()
        for anim in (getattr(self, "pline", None), getattr(self, "orb", None), getattr(self, "mini_orb", None)):
            if anim is not None:
                anim.stop()
        if self._border_after_id is not None:
            try:
                self.root.after_cancel(self._border_after_id)
            except Exception:
                pass
            self._border_after_id = None
        try:
            self.effects.close()
        except Exception:
            pass
        try:
            if self.mcp_server is not None:
                self.mcp_server.shutdown()
                self.mcp_server.server_close()
        except Exception:
            pass
        try:
            self.engine.close()
        except Exception:
            pass
        self.worker.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def run_panel(args: Namespace) -> None:
    root = tk.Tk()
    ChromePilotPanel(root, args)
    root.mainloop()
