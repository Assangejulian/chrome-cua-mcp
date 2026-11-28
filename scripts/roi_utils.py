"""Windows DPI-aware Chrome window geometry, capture, and coordinate mapping utilities."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import mss
import numpy as np
from PIL import Image
import win32api
import win32con
import win32gui
import win32process

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
PROCESS_PER_MONITOR_DPI_AWARE = 2


@dataclass(frozen=True)
class Rect:
    """Screen-space rectangle in physical pixels (may hold negative multi-monitor coords)."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    def to_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)


@dataclass(frozen=True)
class ChromeWindowInfo:
    """Geometry and DPI snapshot of a top-level Chrome window."""

    hwnd: int
    title: str
    dpi: int
    dpi_scale: float
    is_foreground: bool
    is_minimized: bool
    window_rect: Rect
    dwm_rect: Rect
    client_rect: Rect


def enable_per_monitor_dpi_awareness() -> str:
    """Enable Per-Monitor DPI Awareness v2 before any window or screen capture calls."""
    user32 = ctypes.windll.user32
    if hasattr(user32, "SetProcessDpiAwarenessContext"):
        if user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            return "PER_MONITOR_AWARE_V2"
    try:
        shcore = ctypes.windll.shcore
        hr = shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE)
        if hr in (0, -2147024891):  # S_OK or E_ACCESSDENIED (already set)
            return "PER_MONITOR_DPI_AWARE"
    except Exception:
        pass
    user32.SetProcessDPIAware()
    return "SYSTEM_DPI_AWARE_FALLBACK"


def get_dwm_extended_frame_bounds(hwnd: int) -> Rect:
    """Return DWM visible frame bounds excluding invisible Win10/11 drop-shadow borders."""
    rect = wintypes.RECT()
    dwmapi = ctypes.windll.dwmapi
    hr = dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        wintypes.DWORD(DWMWA_EXTENDED_FRAME_BOUNDS),
        ctypes.byref(rect),
        ctypes.sizeof(rect),
    )
    if hr != 0:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        return Rect(l, t, r, b)
    return Rect(rect.left, rect.top, rect.right, rect.bottom)


def get_client_screen_rect(hwnd: int) -> Rect:
    """Return client area bounds converted to physical screen coordinates."""
    _, _, cw, ch = win32gui.GetClientRect(hwnd)
    left, top = win32gui.ClientToScreen(hwnd, (0, 0))
    return Rect(left, top, left + cw, top + ch)


def get_window_dpi(hwnd: int) -> int:
    """Return DPI for the monitor hosting hwnd (96 = 100%)."""
    user32 = ctypes.windll.user32
    if hasattr(user32, "GetDpiForWindow"):
        dpi = int(user32.GetDpiForWindow(wintypes.HWND(hwnd)))
        if dpi > 0:
            return dpi
    return 96


def inspect_chrome_window(hwnd: int) -> ChromeWindowInfo:
    """Read current geometry, visibility, and DPI metadata for a Chrome window handle."""
    title = win32gui.GetWindowText(hwnd)
    wl, wt, wr, wb = win32gui.GetWindowRect(hwnd)
    dpi = get_window_dpi(hwnd)
    fg_hwnd = win32gui.GetForegroundWindow()
    return ChromeWindowInfo(
        hwnd=hwnd,
        title=title,
        dpi=dpi,
        dpi_scale=round(dpi / 96.0, 4),
        is_foreground=(hwnd == fg_hwnd),
        is_minimized=bool(win32gui.IsIconic(hwnd)),
        window_rect=Rect(wl, wt, wr, wb),
        dwm_rect=get_dwm_extended_frame_bounds(hwnd),
        client_rect=get_client_screen_rect(hwnd),
    )


def get_window_process_name(hwnd: int) -> str:
    """Return lowercase executable filename (e.g. 'chrome.exe', 'qq.exe') for a window, or ''."""
    if not hwnd or not win32gui.IsWindow(hwnd):
        return ""
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid:
            return ""
        process = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        try:
            image = win32process.GetModuleFileNameEx(process, 0)
        finally:
            process.Close()
        return Path(image).name.casefold()
    except Exception:
        return ""


def get_primary_screen_rect() -> Rect:
    """Return physical-pixel bounds of the primary monitor."""
    user32 = ctypes.windll.user32
    w = max(1, int(user32.GetSystemMetrics(0)))
    h = max(1, int(user32.GetSystemMetrics(1)))
    return Rect(0, 0, w, h)


def inspect_primary_screen() -> ChromeWindowInfo:
    """Build a synthetic window snapshot (hwnd=0) covering the full primary screen."""
    rect = get_primary_screen_rect()
    user32 = ctypes.windll.user32
    dpi = int(user32.GetDpiForSystem()) if hasattr(user32, "GetDpiForSystem") else 96
    if dpi <= 0:
        dpi = 96
    return ChromeWindowInfo(
        hwnd=0,
        title="Primary Screen",
        dpi=dpi,
        dpi_scale=round(dpi / 96.0, 4),
        is_foreground=True,
        is_minimized=False,
        window_rect=rect,
        dwm_rect=rect,
        client_rect=rect,
    )


def _is_window_cloaked(hwnd: int) -> bool:
    cloaked = ctypes.c_int(0)
    try:
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            wintypes.DWORD(DWMWA_CLOAKED),
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
        return hr == 0 and bool(cloaked.value)
    except Exception:
        return False


def find_all_windows(
    exclude_hwnds: set[int] | None = None,
    min_width: int = 160,
    min_height: int = 120,
) -> list[tuple[ChromeWindowInfo, str]]:
    """Enumerate visible top-level application windows across the Windows desktop."""
    excluded = set(exclude_hwnds or ())
    shell_classes = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
    matches: list[tuple[ChromeWindowInfo, str]] = []
    pid_names: dict[int, str] = {}

    def collect(hwnd: int, _: object) -> bool:
        try:
            if hwnd in excluded or not win32gui.IsWindowVisible(hwnd):
                return True
            if not win32gui.GetWindowText(hwnd):
                return True
            cls = win32gui.GetClassName(hwnd)
            if cls in shell_classes:
                return True
            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            if (ex_style & win32con.WS_EX_TOOLWINDOW) and not (ex_style & win32con.WS_EX_APPWINDOW):
                return True
            if _is_window_cloaked(hwnd):
                return True
            info = inspect_chrome_window(hwnd)
            if not info.is_minimized and (info.dwm_rect.width < min_width or info.dwm_rect.height < min_height):
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid not in pid_names:
                pid_names[pid] = get_window_process_name(hwnd)
            matches.append((info, pid_names[pid]))
        except win32gui.error:
            pass
        finally:
            ctypes.windll.kernel32.SetLastError(0)
        return True

    ctypes.windll.kernel32.SetLastError(0)
    win32gui.EnumWindows(collect, None)
    matches.sort(key=lambda pair: (not pair[0].is_foreground, pair[0].is_minimized, pair[1], pair[0].title))
    return matches


def find_chrome_windows() -> list[ChromeWindowInfo]:
    """Enumerate visible top-level Chrome browser windows, foreground first."""
    matches: list[ChromeWindowInfo] = []
    chrome_processes: dict[int, bool] = {}

    def collect(hwnd: int, _: object) -> bool:
        try:
            if not win32gui.IsWindowVisible(hwnd) or win32gui.GetClassName(hwnd) != "Chrome_WidgetWin_1":
                return True
            if not win32gui.GetWindowText(hwnd):
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid not in chrome_processes:
                process = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                try:
                    image = win32process.GetModuleFileNameEx(process, 0)
                finally:
                    process.Close()
                chrome_processes[pid] = Path(image).name.casefold() == "chrome.exe"
            if not chrome_processes[pid]:
                return True
            info = inspect_chrome_window(hwnd)
            if info.is_minimized or (info.dwm_rect.width >= 200 and info.dwm_rect.height >= 150):
                matches.append(info)
        except win32gui.error:
            pass
        finally:
            # pywin32 EnumWindows inspects GetLastError() even when callbacks return True.
            ctypes.windll.kernel32.SetLastError(0)
        return True

    ctypes.windll.kernel32.SetLastError(0)
    win32gui.EnumWindows(collect, None)
    matches.sort(key=lambda item: (not item.is_foreground, item.is_minimized))
    return matches


def capture_roi(roi: Rect) -> Image.Image:
    """Capture a physical-pixel ROI via mss without copying full desktop buffers."""
    if roi.width <= 0 or roi.height <= 0:
        raise ValueError(f"Invalid ROI dimensions: {roi}")
    monitor = {
        "left": roi.left,
        "top": roi.top,
        "width": roi.width,
        "height": roi.height,
    }
    with mss.mss() as sct:
        shot = sct.grab(monitor)
        arr = np.asarray(shot, dtype=np.uint8)[:, :, :3][:, :, ::-1]  # BGRA -> RGB
        return Image.fromarray(arr)


def norm_to_screen(x_norm: float, y_norm: float, roi: Rect) -> tuple[int, int]:
    """Map [0, 1000] normalized coordinates to clamped screen physical pixels inside roi."""
    if roi.width <= 0 or roi.height <= 0:
        raise ValueError(f"Invalid ROI dimensions: {roi}")
    rel_x = min(max(round(float(x_norm) / 1000.0 * (roi.width - 1)), 0), roi.width - 1)
    rel_y = min(max(round(float(y_norm) / 1000.0 * (roi.height - 1)), 0), roi.height - 1)
    return roi.left + rel_x, roi.top + rel_y


def screen_to_norm(screen_x: int, screen_y: int, roi: Rect) -> Optional[tuple[int, int]]:
    """Map physical screen coordinates inside roi to [0, 1000] integers; None if outside."""
    if not (roi.left <= screen_x < roi.right and roi.top <= screen_y < roi.bottom):
        return None
    rel_x = screen_x - roi.left
    rel_y = screen_y - roi.top
    x_norm = round(rel_x / max(1, roi.width - 1) * 1000.0)
    y_norm = round(rel_y / max(1, roi.height - 1) * 1000.0)
    return min(max(x_norm, 0), 1000), min(max(y_norm, 0), 1000)


def is_window_ready(hwnd: int, expected_dwm_rect: Rect) -> bool:
    """Verify target Chrome window still exists, is foreground, unminimized, and stationary."""
    if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
        return False
    if win32gui.IsIconic(hwnd):
        return False
    if win32gui.GetForegroundWindow() != hwnd:
        return False
    return get_dwm_extended_frame_bounds(hwnd) == expected_dwm_rect


def capture_chrome_window(info: ChromeWindowInfo) -> Image.Image:
    """Capture only while the selected Chrome window is stable and in the foreground."""
    if not is_window_ready(info.hwnd, info.dwm_rect):
        raise RuntimeError("Chrome is not in the foreground or its geometry changed.")
    image = capture_roi(info.dwm_rect)
    if not is_window_ready(info.hwnd, info.dwm_rect):
        raise RuntimeError("Chrome changed while capturing the screenshot.")
    return image


def is_point_on_window(hwnd: int, point: tuple[int, int]) -> bool:
    window_at_point = win32gui.WindowFromPoint(point)
    return bool(window_at_point and win32gui.GetAncestor(window_at_point, win32con.GA_ROOT) == hwnd)


def restore_foreground_if_needed(hwnd: int) -> None:
    """Helper to restore minimized window if explicitly requested in controlled tests."""
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
