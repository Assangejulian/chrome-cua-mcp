"""Win32 non-intrusive visual effects for Chrome Pilot Computer Use:
1. Screen-edge flowing blue gradient aura (click-through, excluded from screen capture).
2. Custom luminous AI mouse cursor while Computer Use is active (with multi-layer restoration).
"""

from __future__ import annotations

import atexit
import ctypes
from ctypes import wintypes
import math
from pathlib import Path
import threading
import time
from typing import Any, Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# Isolated Win32 DLL loaders with explicit 64-bit argtypes/restypes
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

WM_NCHITTEST = 0x0084
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
HTTRANSPARENT = -1

PM_REMOVE = 0x0001
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
BI_RGB = 0
DIB_RGB_COLORS = 0

SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_CXCURSOR = 13
SM_CYCURSOR = 14

WDA_EXCLUDEFROMCAPTURE = 0x00000011

OCR_NORMAL = 32512
OCR_HAND = 32649
SPI_SETCURSORS = 0x0057


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HICON),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
        ("lPrivate", wintypes.DWORD),
    ]


def _setup_win32_bindings() -> None:
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE

    _user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _user32.GetSystemMetrics.restype = ctypes.c_int

    _user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.DefWindowProcW.restype = LRESULT

    _user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    _user32.RegisterClassExW.restype = wintypes.ATOM

    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    _user32.CreateWindowExW.restype = wintypes.HWND

    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.DestroyWindow.restype = wintypes.BOOL

    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.ShowWindow.restype = wintypes.BOOL

    _user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    _user32.SetWindowDisplayAffinity.restype = wintypes.BOOL

    _user32.UpdateLayeredWindow.argtypes = [
        wintypes.HWND, wintypes.HDC, ctypes.POINTER(POINT), ctypes.POINTER(SIZE),
        wintypes.HDC, ctypes.POINTER(POINT), wintypes.COLORREF,
        ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD,
    ]
    _user32.UpdateLayeredWindow.restype = wintypes.BOOL

    _user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
    _user32.PeekMessageW.restype = wintypes.BOOL

    _user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    _user32.TranslateMessage.restype = wintypes.BOOL

    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    _user32.DispatchMessageW.restype = LRESULT

    _user32.GetDC.argtypes = [wintypes.HWND]
    _user32.GetDC.restype = wintypes.HDC

    _user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _user32.ReleaseDC.restype = ctypes.c_int

    _gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _gdi32.CreateCompatibleDC.restype = wintypes.HDC

    _gdi32.DeleteDC.argtypes = [wintypes.HDC]
    _gdi32.DeleteDC.restype = wintypes.BOOL

    _gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _gdi32.SelectObject.restype = wintypes.HGDIOBJ

    _gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _gdi32.DeleteObject.restype = wintypes.BOOL

    _gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
    ]
    _gdi32.CreateDIBSection.restype = wintypes.HBITMAP

    _gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
    _gdi32.CreateBitmap.restype = wintypes.HBITMAP

    _user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
    _user32.CreateIconIndirect.restype = wintypes.HICON

    _user32.DestroyIcon.argtypes = [wintypes.HICON]
    _user32.DestroyIcon.restype = wintypes.BOOL

    _user32.SetSystemCursor.argtypes = [wintypes.HICON, wintypes.DWORD]
    _user32.SetSystemCursor.restype = wintypes.BOOL

    _user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT]
    _user32.SystemParametersInfoW.restype = wintypes.BOOL


_setup_win32_bindings()


def _create_topdown_dib(hdc_ref: wintypes.HDC, width: int, height: int) -> tuple[wintypes.HBITMAP, ctypes.c_void_p]:
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB
    bits = ctypes.c_void_p()
    hbmp = _gdi32.CreateDIBSection(hdc_ref, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
    if not hbmp or not bits.value:
        raise OSError(f"CreateDIBSection failed (err={ctypes.get_last_error()})")
    return hbmp, bits


# ---------------- Component A: Custom AI Mouse Cursor ----------------

_ACTIVE_CURSOR_MANAGERS: set["AICursorManager"] = set()
_CURSOR_GLOBAL_LOCK = threading.Lock()


def restore_all_cursors() -> None:
    """Restore system cursors across any remaining AICursorManager instances on exit."""
    with _CURSOR_GLOBAL_LOCK:
        for mgr in list(_ACTIVE_CURSOR_MANAGERS):
            mgr._active = False
    try:
        _user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0)
    except Exception:
        pass


atexit.register(restore_all_cursors)


def render_ai_cursor_rgba(kind: str = "arrow", size: int | None = None) -> tuple[Image.Image, tuple[int, int]]:
    """Render a sleek 4x-supersampled luminous cyan-blue AI cursor RGBA sprite."""
    if size is None:
        sys_sz = int(_user32.GetSystemMetrics(SM_CXCURSOR) or 32)
        size = max(36, min(64, int(round(sys_sz * 1.45))))
    scale = 4
    w = size * scale
    h = size * scale

    if kind == "hand":
        hotspot = (size // 2, size // 2)
        cx, cy = w // 2, h // 2
        glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        r_outer = round(w * 0.30)
        gd.ellipse([cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer], outline=(37, 99, 235, 220), width=scale * 4)
        gd.ellipse([cx - r_outer // 2, cy - r_outer // 2, cx + r_outer // 2, cy + r_outer // 2], fill=(56, 189, 248, 210))
        glow = glow.filter(ImageFilter.GaussianBlur(radius=scale * 2.8))

        base = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        bd = ImageDraw.Draw(base)
        r_ring = round(w * 0.27)
        bd.ellipse([cx - r_ring, cy - r_ring, cx + r_ring, cy + r_ring], fill=(14, 18, 30, 145), outline=(56, 189, 248, 255), width=round(scale * 2.2))
        r_in = round(w * 0.23)
        bd.ellipse([cx - r_in, cy - r_in, cx + r_in, cy + r_in], outline=(224, 242, 254, 240), width=round(scale * 0.9))
        r_dot = round(w * 0.09)
        bd.ellipse([cx - r_dot, cy - r_dot, cx + r_dot, cy + r_dot], fill=(56, 189, 248, 255), outline=(255, 255, 255, 255), width=scale)

        comp = Image.alpha_composite(glow, base)
        return comp.resize((size, size), Image.Resampling.LANCZOS), hotspot

    # Default: sleek vector arrow with luminous cyan->cobalt aura + glowing AI core jewel
    hotspot = (5, 4)
    ox, oy = hotspot[0] * scale, hotspot[1] * scale
    poly = [
        (ox, oy),
        (ox, oy + round(w * 0.56)),
        (ox + round(w * 0.145), oy + round(w * 0.435)),
        (ox + round(w * 0.245), oy + round(w * 0.625)),
        (ox + round(w * 0.325), oy + round(w * 0.585)),
        (ox + round(w * 0.225), oy + round(w * 0.395)),
        (ox + round(w * 0.41), oy + round(w * 0.39)),
    ]

    halo1 = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d1 = ImageDraw.Draw(halo1)
    d1.polygon(poly, fill=(37, 99, 235, 195), outline=(56, 189, 248, 255), width=round(scale * 3.2))
    halo1 = halo1.filter(ImageFilter.GaussianBlur(radius=scale * 2.6))

    halo2 = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d2 = ImageDraw.Draw(halo2)
    d2.polygon(poly, outline=(56, 189, 248, 245), width=round(scale * 1.8))
    halo2 = halo2.filter(ImageFilter.GaussianBlur(radius=scale * 1.1))

    body = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    db = ImageDraw.Draw(body)
    db.polygon(poly, fill=(13, 21, 38, 245))
    # Gradient cyan rim (outer cyan stroke + inner bright edge)
    db.line([*poly, poly[0]], fill=(56, 189, 248, 255), width=round(scale * 1.65), joint="curve")
    db.line([*poly, poly[0]], fill=(224, 242, 254, 235), width=round(scale * 0.65), joint="curve")

    # Small glowing cyan AI core diamond inside upper arrow body
    jx, jy = ox + round(w * 0.125), oy + round(w * 0.275)
    jr = round(scale * 1.45)
    db.polygon([(jx, jy - jr), (jx + jr, jy), (jx, jy + jr), (jx - jr, jy)], fill=(56, 189, 248, 255))

    comp = Image.alpha_composite(Image.alpha_composite(halo1, halo2), body)
    return comp.resize((size, size), Image.Resampling.LANCZOS), hotspot


def create_hcursor_from_rgba(img: Image.Image, hotspot: tuple[int, int]) -> int:
    """Build a Win32 32-bit ARGB HCURSOR handle from a PIL RGBA image."""
    rgba = np.asarray(img.convert("RGBA"), dtype=np.uint8)
    h, w, _ = rgba.shape
    bgra = rgba[:, :, [2, 1, 0, 3]].copy()

    hdc_screen = _user32.GetDC(None)
    if not hdc_screen:
        raise OSError("GetDC(None) failed")
    hbm_color = None
    hbm_mask = None
    try:
        hbm_color, bits = _create_topdown_dib(hdc_screen, w, h)
        ctypes.memmove(bits.value, bgra.ctypes.data, bgra.nbytes)
        stride = ((w + 31) // 32) * 4
        zeros = ctypes.create_string_buffer(stride * h)
        hbm_mask = _gdi32.CreateBitmap(w, h, 1, 1, zeros)
        if not hbm_mask:
            raise OSError(f"CreateBitmap failed (err={ctypes.get_last_error()})")

        ii = ICONINFO(
            fIcon=False,
            xHotspot=int(max(0, min(w - 1, hotspot[0]))),
            yHotspot=int(max(0, min(h - 1, hotspot[1]))),
            hbmMask=hbm_mask,
            hbmColor=hbm_color,
        )
        hcur = _user32.CreateIconIndirect(ctypes.byref(ii))
        if not hcur:
            raise OSError(f"CreateIconIndirect failed (err={ctypes.get_last_error()})")
        return int(hcur)
    finally:
        if hbm_color:
            _gdi32.DeleteObject(hbm_color)
        if hbm_mask:
            _gdi32.DeleteObject(hbm_mask)
        _user32.ReleaseDC(None, hdc_screen)


class AICursorManager:
    """Applies a luminous cyan-blue AI cursor during CUA runs and guarantees restoration."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = False
        self._arrow_sprite, self._arrow_hot = render_ai_cursor_rgba("arrow")
        self._hand_sprite, self._hand_hot = render_ai_cursor_rgba("hand")
        with _CURSOR_GLOBAL_LOCK:
            _ACTIVE_CURSOR_MANAGERS.add(self)

    def activate(self) -> None:
        with self._lock:
            if self._active:
                return
            try:
                h_arrow = create_hcursor_from_rgba(self._arrow_sprite, self._arrow_hot)
                if not _user32.SetSystemCursor(wintypes.HICON(h_arrow), OCR_NORMAL):
                    _user32.DestroyIcon(wintypes.HICON(h_arrow))
                    return
                h_hand = create_hcursor_from_rgba(self._hand_sprite, self._hand_hot)
                if not _user32.SetSystemCursor(wintypes.HICON(h_hand), OCR_HAND):
                    _user32.DestroyIcon(wintypes.HICON(h_hand))
                self._active = True
            except Exception:
                self.restore()

    def restore(self) -> None:
        with self._lock:
            was_active = self._active
            self._active = False
            if was_active:
                try:
                    _user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0)
                except Exception:
                    pass

    def close(self) -> None:
        self.restore()
        with _CURSOR_GLOBAL_LOCK:
            _ACTIVE_CURSOR_MANAGERS.discard(self)


# ---------------- Component B: Click-Through Screen-Edge Glow ----------------

_BLUE_PALETTE = np.array(
    [
        [56.0, 189.0, 248.0],   # electric cyan
        [37.0, 99.0, 235.0],    # cobalt blue
        [99.0, 102.0, 241.0],   # indigo accent
        [34.0, 211.0, 238.0],   # bright aqua
    ],
    dtype=np.float32,
)

_EMERALD_PALETTE = np.array(
    [
        [52.0, 211.0, 153.0],   # emerald mint
        [34.0, 197.0, 94.0],    # vivid emerald
        [56.0, 189.0, 248.0],   # cyan highlight
        [16.0, 185.0, 129.0],   # deep teal
    ],
    dtype=np.float32,
)


def _sample_palette(u: np.ndarray, palette: np.ndarray) -> np.ndarray:
    """Smoothly wrap-interpolate an RGB gradient over normalized phase u in [0, 1)."""
    n = len(palette)
    pos = np.mod(u, 1.0) * n
    idx0 = np.floor(pos).astype(np.int32) % n
    idx1 = (idx0 + 1) % n
    frac = (pos - np.floor(pos))[..., None]
    # cosine smoothstep
    t = 0.5 - 0.5 * np.cos(frac * np.pi)
    return palette[idx0] * (1.0 - t) + palette[idx1] * t


def _strip_rgba(
    xs: np.ndarray,
    ys: np.ndarray,
    edge_dist: np.ndarray,
    thickness: int,
    width: int,
    height: int,
    phase: float,
    palette: np.ndarray,
) -> np.ndarray:
    """Compute premultiplied BGRA pixels for a thin perimeter strip."""
    norm_d = np.clip(edge_dist / max(1.0, float(thickness - 1)), 0.0, 1.0)
    rim = 0.62 * np.exp(-((edge_dist / 2.2) ** 2))
    bloom = 0.44 * (np.maximum(0.0, np.cos(norm_d * (np.pi * 0.5))) ** 2.3)

    theta = np.arctan2(ys - height * 0.5, xs - width * 0.5)
    u = theta / (2.0 * np.pi) + 0.5 + phase
    wave = 0.85 + 0.15 * np.sin(2.0 * np.pi * (u * 2.0 + phase))
    alpha = np.clip((rim + bloom) * wave, 0.0, 1.0)

    rgb = _sample_palette(u, palette)
    # Bright crisp white-cyan boost right along outer 2px border
    rim_boost = np.clip(rim * 0.45, 0.0, 0.35)[..., None]
    rgb = rgb * (1.0 - rim_boost) + 255.0 * rim_boost

    alpha_u8 = np.clip(np.round(alpha * 255.0), 0, 255).astype(np.uint8)
    alpha_f = alpha_u8.astype(np.float32) / 255.0
    bgra = np.empty((*edge_dist.shape, 4), dtype=np.uint8)
    bgra[..., 0] = np.clip(np.round(rgb[..., 2] * alpha_f), 0, 255).astype(np.uint8)
    bgra[..., 1] = np.clip(np.round(rgb[..., 1] * alpha_f), 0, 255).astype(np.uint8)
    bgra[..., 2] = np.clip(np.round(rgb[..., 0] * alpha_f), 0, 255).astype(np.uint8)
    bgra[..., 3] = alpha_u8
    return np.ascontiguousarray(bgra)


def build_glow_strip_frames(
    width: int,
    height: int,
    thickness: int = 38,
    num_frames: int = 18,
    palette: str = "blue",
) -> list[dict[str, np.ndarray]]:
    """Precompute disjoint perimeter strip animation frames (premultiplied BGRA)."""
    t = max(8, min(thickness, min(width, height) // 4))
    pal = _EMERALD_PALETTE if palette == "emerald" else _BLUE_PALETTE
    mid_h = max(0, height - 2 * t)

    # Disjoint coordinate grids
    x_full = np.arange(width, dtype=np.float32)[None, :]
    y_top = np.arange(t, dtype=np.float32)[:, None]
    y_bot = np.arange(height - t, height, dtype=np.float32)[:, None]
    side_x = np.minimum(x_full, float(width - 1) - x_full)
    d_top = np.minimum(y_top, side_x)
    d_bot = np.minimum(float(height - 1) - y_bot, side_x)

    if mid_h > 0:
        y_mid = np.arange(t, height - t, dtype=np.float32)[:, None]
        x_left = np.arange(t, dtype=np.float32)[None, :]
        x_right = np.arange(width - t, width, dtype=np.float32)[None, :]
        d_left = np.broadcast_to(x_left, (mid_h, t)).astype(np.float32)
        d_right = np.broadcast_to(float(width - 1) - x_right, (mid_h, t)).astype(np.float32)

    frames: list[dict[str, np.ndarray]] = []
    for i in range(max(1, num_frames)):
        phase = i / float(max(1, num_frames))
        frame: dict[str, np.ndarray] = {
            "top": _strip_rgba(np.broadcast_to(x_full, (t, width)), np.broadcast_to(y_top, (t, width)), d_top, t, width, height, phase, pal),
            "bottom": _strip_rgba(np.broadcast_to(x_full, (t, width)), np.broadcast_to(y_bot, (t, width)), d_bot, t, width, height, phase, pal),
        }
        if mid_h > 0:
            frame["left"] = _strip_rgba(np.broadcast_to(x_left, (mid_h, t)), np.broadcast_to(y_mid, (mid_h, t)), d_left, t, width, height, phase, pal)
            frame["right"] = _strip_rgba(np.broadcast_to(x_right, (mid_h, t)), np.broadcast_to(y_mid, (mid_h, t)), d_right, t, width, height, phase, pal)
        else:
            empty = np.zeros((0, t, 4), dtype=np.uint8)
            frame["left"] = empty
            frame["right"] = empty
        frames.append(frame)
    return frames


class ScreenEdgeGlowOverlay:
    """Dedicated thread managing a click-through, capture-excluded Win32 layered edge aura."""

    def __init__(self, thickness: int = 38) -> None:
        self.thickness = thickness
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._mode = "hidden"  # "hidden" | "fade_in" | "blue" | "emerald" | "fade_out"
        self._alpha = 0.0
        self._target_alpha = 0.0
        self._emerald_until = 0.0
        self._preview_until = 0.0
        self._thread: Optional[threading.Thread] = None
        self._hwnd: int = 0
        self._wndproc_ref: Any = None
        self._width = 0
        self._height = 0

    def _ensure_started(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._ready_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="cua-edge-glow")
        self._thread.start()
        self._ready_event.wait(timeout=2.0)

    def start_blue(self, preview_s: float = 0.0) -> None:
        self._ensure_started()
        with self._lock:
            self._mode = "blue"
            self._target_alpha = 235.0
            self._emerald_until = 0.0
            self._preview_until = (time.monotonic() + preview_s) if preview_s > 0.0 else 0.0

    def pulse_emerald_and_fade(self, hold_s: float = 0.85) -> None:
        with self._lock:
            if self._mode == "hidden" and self._alpha <= 1.0:
                return
            self._mode = "emerald"
            self._target_alpha = 242.0
            self._emerald_until = time.monotonic() + hold_s
            self._preview_until = 0.0

    def fade_out(self) -> None:
        with self._lock:
            self._preview_until = 0.0
            self._emerald_until = 0.0
            if self._mode != "hidden":
                self._mode = "fade_out"
                self._target_alpha = 0.0

    def close(self) -> None:
        self._stop_event.set()
        hwnd = self._hwnd
        if hwnd:
            try:
                _user32.ShowWindow(wintypes.HWND(hwnd), SW_HIDE)
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None

    def _run_loop(self) -> None:
        w = max(320, int(_user32.GetSystemMetrics(SM_CXSCREEN) or 1920))
        h = max(240, int(_user32.GetSystemMetrics(SM_CYSCREEN) or 1080))
        t = max(8, min(self.thickness, min(w, h) // 4))
        self._width, self._height = w, h

        blue_frames = build_glow_strip_frames(w, h, thickness=t, num_frames=18, palette="blue")
        emerald_frames = build_glow_strip_frames(w, h, thickness=t, num_frames=18, palette="emerald")

        hinst = _kernel32.GetModuleHandleW(None)
        cls_name = f"ChromePilotEdgeGlow_{threading.get_ident()}"

        @WNDPROC
        def _wnd_proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            if msg == WM_NCHITTEST:
                return HTTRANSPARENT
            if msg == WM_CLOSE:
                return 0
            return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = _wnd_proc
        wc = WNDCLASSEXW(
            cbSize=ctypes.sizeof(WNDCLASSEXW),
            style=0,
            lpfnWndProc=self._wndproc_ref,
            cbClsExtra=0,
            cbWndExtra=0,
            hInstance=hinst,
            hIcon=None,
            hCursor=None,
            hbrBackground=None,
            lpszMenuName=None,
            lpszClassName=cls_name,
            hIconSm=None,
        )
        _user32.RegisterClassExW(ctypes.byref(wc))

        ex_style = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        hwnd = _user32.CreateWindowExW(ex_style, cls_name, "ChromePilotEdgeGlow", WS_POPUP, 0, 0, w, h, None, None, hinst, None)
        self._hwnd = int(hwnd or 0)
        if not self._hwnd:
            self._ready_event.set()
            return

        _user32.SetWindowDisplayAffinity(wintypes.HWND(self._hwnd), WDA_EXCLUDEFROMCAPTURE)

        hdc_screen = _user32.GetDC(None)
        hdc_mem = _gdi32.CreateCompatibleDC(hdc_screen)
        hbmp, bits = _create_topdown_dib(hdc_screen, w, h)
        old_obj = _gdi32.SelectObject(hdc_mem, hbmp)

        ctypes.memset(bits.value, 0, w * h * 4)
        arr_type = ctypes.c_uint8 * (w * h * 4)
        fb = np.ctypeslib.as_array(arr_type.from_address(bits.value)).reshape((h, w, 4))

        pt_dst = POINT(0, 0)
        pt_src = POINT(0, 0)
        sz = SIZE(w, h)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 0, AC_SRC_ALPHA)
        visible = False
        frame_idx = 0
        msg_buf = MSG()
        self._ready_event.set()

        try:
            while not self._stop_event.is_set():
                while _user32.PeekMessageW(ctypes.byref(msg_buf), None, 0, 0, PM_REMOVE):
                    _user32.TranslateMessage(ctypes.byref(msg_buf))
                    _user32.DispatchMessageW(ctypes.byref(msg_buf))

                now = time.monotonic()
                with self._lock:
                    if self._mode == "blue" and self._preview_until > 0.0 and now >= self._preview_until:
                        self._mode = "fade_out"
                        self._target_alpha = 0.0
                        self._preview_until = 0.0
                    elif self._mode == "emerald" and self._emerald_until > 0.0 and now >= self._emerald_until:
                        self._mode = "fade_out"
                        self._target_alpha = 0.0
                        self._emerald_until = 0.0

                    mode = self._mode
                    target_a = self._target_alpha

                if mode == "hidden":
                    if visible:
                        _user32.ShowWindow(wintypes.HWND(self._hwnd), SW_HIDE)
                        visible = False
                    self._stop_event.wait(0.08)
                    continue

                if self._alpha < target_a:
                    self._alpha = min(target_a, self._alpha + 32.0)
                elif self._alpha > target_a:
                    self._alpha = max(target_a, self._alpha - 24.0)

                if mode == "fade_out" and self._alpha <= 0.5:
                    with self._lock:
                        if self._mode == "fade_out":
                            self._mode = "hidden"
                    if visible:
                        _user32.ShowWindow(wintypes.HWND(self._hwnd), SW_HIDE)
                        visible = False
                    self._alpha = 0.0
                    continue

                cur_frames = emerald_frames if mode == "emerald" else blue_frames
                f = cur_frames[frame_idx % len(cur_frames)]
                frame_idx += 1

                fb[:t, :, :] = f["top"]
                fb[h - t :, :, :] = f["bottom"]
                if h > 2 * t:
                    fb[t : h - t, :t, :] = f["left"]
                    fb[t : h - t, w - t :, :] = f["right"]

                blend.SourceConstantAlpha = int(max(0, min(255, round(self._alpha))))
                _user32.UpdateLayeredWindow(
                    wintypes.HWND(self._hwnd),
                    hdc_screen,
                    ctypes.byref(pt_dst),
                    ctypes.byref(sz),
                    hdc_mem,
                    ctypes.byref(pt_src),
                    0,
                    ctypes.byref(blend),
                    ULW_ALPHA,
                )
                if not visible:
                    _user32.ShowWindow(wintypes.HWND(self._hwnd), SW_SHOWNOACTIVATE)
                    visible = True
                self._stop_event.wait(0.042)
        finally:
            if old_obj:
                _gdi32.SelectObject(hdc_mem, old_obj)
            if hbmp:
                _gdi32.DeleteObject(hbmp)
            if hdc_mem:
                _gdi32.DeleteDC(hdc_mem)
            if hdc_screen:
                _user32.ReleaseDC(None, hdc_screen)
            if self._hwnd:
                _user32.DestroyWindow(wintypes.HWND(self._hwnd))
                self._hwnd = 0


# ---------------- Component C: Facade & Inactivity Watchdog ----------------

class ComputerUseEffects:
    """Coordinated edge blue aura + luminous AI cursor with 45s safety watchdog."""

    def __init__(self, enabled: bool = True, inactivity_timeout_s: float = 45.0) -> None:
        self.enabled = bool(enabled)
        self.inactivity_timeout_s = float(inactivity_timeout_s)
        self._lock = threading.Lock()
        self._cursor = AICursorManager()
        self._overlay = ScreenEdgeGlowOverlay(thickness=38)
        self._watchdog_timer: Optional[threading.Timer] = None
        self._closed = False

    def _reset_watchdog_locked(self) -> None:
        if self._watchdog_timer is not None:
            self._watchdog_timer.cancel()
            self._watchdog_timer = None
        if self._closed or self.inactivity_timeout_s <= 0.0:
            return
        timer = threading.Timer(self.inactivity_timeout_s, self.deactivate)
        timer.daemon = True
        timer.start()
        self._watchdog_timer = timer

    def _cancel_watchdog_locked(self) -> None:
        if self._watchdog_timer is not None:
            self._watchdog_timer.cancel()
            self._watchdog_timer = None

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.enabled = bool(enabled)
            if not self.enabled:
                self._cancel_watchdog_locked()
                self._cursor.restore()
                self._overlay.fade_out()

    def activate(self) -> None:
        with self._lock:
            if self._closed or not self.enabled:
                return
            self._cursor.activate()
            self._overlay.start_blue()
            self._reset_watchdog_locked()

    def keepalive(self) -> None:
        with self._lock:
            if self._closed or not self.enabled:
                return
            self._reset_watchdog_locked()

    def pause_for_human(self) -> None:
        with self._lock:
            self._cancel_watchdog_locked()
            self._cursor.restore()
            self._overlay.fade_out()

    def pulse_complete(self) -> None:
        with self._lock:
            self._cancel_watchdog_locked()
            self._cursor.restore()
            if not self._closed and self.enabled:
                self._overlay.pulse_emerald_and_fade(hold_s=0.85)

    def deactivate(self) -> None:
        with self._lock:
            self._cancel_watchdog_locked()
            self._cursor.restore()
            self._overlay.fade_out()

    def preview_brief(self, duration_s: float = 1.6) -> None:
        with self._lock:
            if self._closed or not self.enabled:
                return
            self._overlay.start_blue(preview_s=duration_s)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._cancel_watchdog_locked()
        self._cursor.close()
        self._overlay.close()


def save_effects_previews(out_dir: Path) -> tuple[Path, Path]:
    """Save visual inspection previews of the flowing screen-edge glow and custom AI cursors."""
    out_dir.mkdir(parents=True, exist_ok=True)
    w, h, t = 1280, 720, 36
    base = np.zeros((h, w, 3), dtype=np.uint8)
    base[:, :] = (12, 16, 24)
    bg_img = Image.fromarray(base)
    bd = ImageDraw.Draw(bg_img)
    bd.rounded_rectangle([88, 66, w - 88, h - 54], radius=16, fill=(20, 26, 38), outline=(38, 48, 68), width=2)
    bd.text((120, 92), "Chrome Pilot Computer Use · Edge Blue Glow Mockup", fill=(148, 163, 184))
    bg_rgba = bg_img.convert("RGBA")

    glow_rgba = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    g_arr = np.array(glow_rgba, dtype=np.uint8)
    frame = build_glow_strip_frames(w, h, thickness=t, num_frames=4, palette="blue")[1]

    def _to_straight_rgba(bgra_p: np.ndarray) -> np.ndarray:
        a = bgra_p[..., 3]
        out = np.zeros_like(bgra_p)
        nz = a > 0
        af = a[nz].astype(np.float32) / 255.0
        out[..., 0][nz] = np.clip(np.round(bgra_p[..., 2][nz] / af), 0, 255).astype(np.uint8)
        out[..., 1][nz] = np.clip(np.round(bgra_p[..., 1][nz] / af), 0, 255).astype(np.uint8)
        out[..., 2][nz] = np.clip(np.round(bgra_p[..., 0][nz] / af), 0, 255).astype(np.uint8)
        out[..., 3] = a
        return out

    g_arr[:t, :, :] = _to_straight_rgba(frame["top"])
    g_arr[h - t :, :, :] = _to_straight_rgba(frame["bottom"])
    g_arr[t : h - t, :t, :] = _to_straight_rgba(frame["left"])
    g_arr[t : h - t, w - t :, :] = _to_straight_rgba(frame["right"])
    comp = Image.alpha_composite(bg_rgba, Image.fromarray(g_arr))
    glow_path = out_dir / "glow_overlay_preview.png"
    comp.convert("RGB").save(glow_path)

    arrow, _ = render_ai_cursor_rgba("arrow", 48)
    hand, _ = render_ai_cursor_rgba("hand", 48)
    canvas = Image.new("RGBA", (520, 240), (14, 18, 25, 255))
    cd = ImageDraw.Draw(canvas)
    cd.rounded_rectangle([18, 18, 250, 222], radius=16, fill=(18, 24, 36, 255), outline=(42, 50, 68, 255))
    cd.rounded_rectangle([270, 18, 502, 222], radius=16, fill=(235, 242, 250, 255), outline=(203, 213, 225, 255))
    a_big = arrow.resize((128, 128), Image.Resampling.LANCZOS)
    h_big = hand.resize((128, 128), Image.Resampling.LANCZOS)
    canvas.alpha_composite(a_big, (32, 46))
    canvas.alpha_composite(h_big, (132, 46))
    canvas.alpha_composite(a_big, (284, 46))
    canvas.alpha_composite(h_big, (384, 46))
    cd.text((54, 192), "Dark Desktop (Arrow + Pointer)", fill=(148, 163, 184))
    cd.text((304, 192), "Light Desktop (Arrow + Pointer)", fill=(51, 65, 85))
    cursor_path = out_dir / "ai_cursor_preview.png"
    canvas.convert("RGB").save(cursor_path)
    return glow_path, cursor_path
