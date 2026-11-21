"""Capture a dedicated Chrome window without switching the user's Windows desktop."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import numpy as np
from PIL import Image
import pywintypes
import win32api
import win32con
import win32event
import win32gui
import win32process
import win32service
import win32ui

from roi_utils import enable_per_monitor_dpi_awareness, get_dwm_extended_frame_bounds


def emit(payload: dict) -> None:
    print(json.dumps(payload), flush=True)


def browser_windows(desktop, process_id: int) -> list[int]:
    try:
        handles = desktop.EnumDesktopWindows()
    except pywintypes.error:
        return []
    return [
        int(handle) for handle in handles
        if win32gui.IsWindow(handle)
        and win32gui.GetClassName(handle) == "Chrome_WidgetWin_1"
        and win32process.GetWindowThreadProcessId(handle)[1] == process_id
    ]


def capture(handle: int) -> Image.Image:
    left, top, right, bottom = win32gui.GetWindowRect(handle)
    width, height = right - left, bottom - top
    window_hdc = win32gui.GetWindowDC(handle)
    source = win32ui.CreateDCFromHandle(window_hdc)
    target = source.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(source, width, height)
        target.SelectObject(bitmap)
        print_window = ctypes.windll.user32.PrintWindow
        print_window.argtypes = (wintypes.HWND, wintypes.HDC, wintypes.UINT)
        print_window.restype = wintypes.BOOL
        if not print_window(handle, target.GetSafeHdc(), 2):
            raise RuntimeError("Chrome window did not render to PrintWindow")
        image = Image.frombytes("RGB", (width, height), bitmap.GetBitmapBits(True), "raw", "BGRX", 0, 1)
    finally:
        target.DeleteDC()
        if bitmap.GetHandle():
            win32gui.DeleteObject(bitmap.GetHandle())
        win32gui.ReleaseDC(handle, window_hdc)

    frame = get_dwm_extended_frame_bounds(handle)
    crop = (frame.left - left, frame.top - top, frame.right - left, frame.bottom - top)
    if 0 <= crop[0] < crop[2] <= width and 0 <= crop[1] < crop[3] <= height:
        image = image.crop(crop)
    return image


def capture_frame(handle: int, path: Path, marker: list[int], scale: float) -> dict:
    for _ in range(5):
        try:
            image = capture(handle)
        except (OSError, RuntimeError):
            break
        pixels = np.asarray(image)
        rows, columns = np.where(np.all(pixels[:200, :80] == marker, axis=2))
        if rows.size >= 12 * scale * scale:
            left, top = int(columns.min()), int(rows.min())
            mark_width, mark_height = int(columns.max()) - left + 1, int(rows.max()) - top + 1
            if abs(mark_width - 16 * scale) <= 2 and abs(mark_height - 8 * scale) <= 2:
                image.save(path, format="PNG", compress_level=5)
                return {"origin": [left, top], "size": list(image.size), "mode": "full_window"}
        time.sleep(0.12)
    return {"mode": "unavailable"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--scale", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dpi_awareness = enable_per_monitor_dpi_awareness()
    desktop_name = f"ChromePilotM3_{os.getpid()}"
    desktop = win32service.CreateDesktop(desktop_name, 0, 0x01FF, None)
    try:
        with tempfile.TemporaryDirectory(prefix="ChromePilotM3_", ignore_cleanup_errors=True) as temporary:
            profile = Path(temporary)
            startup = win32process.STARTUPINFO()
            startup.lpDesktop = f"winsta0\\{desktop_name}"
            command = subprocess.list2cmdline([
                args.chrome, f"--user-data-dir={profile}", "--remote-debugging-port=0", "--no-first-run",
                "--no-default-browser-check", "--lang=zh-CN", "--accept-lang=zh-CN", "--disable-gpu",
                "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
                "--disable-background-timer-throttling", "--disable-background-networking", "--no-proxy-server",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1", "--window-size=1240,750",
                f"--force-device-scale-factor={args.scale}", args.url,
            ])
            process, thread, process_id, _ = win32process.CreateProcess(
                args.chrome, command, None, None, False, 0, None, None, startup,
            )
            try:
                port_file = profile / "DevToolsActivePort"
                for _ in range(100):
                    windows = [handle for handle in browser_windows(desktop, process_id) if win32gui.IsWindowVisible(handle)]
                    if len(windows) == 1 and port_file.is_file() and port_file.stat().st_size:
                        handle = windows[0]
                        port = int(port_file.read_text(encoding="ascii").splitlines()[0])
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Dedicated Chrome window did not start")
                window_dpi = ctypes.windll.user32.GetDpiForWindow(handle)
                emit({"port": port, "windows_dpi": window_dpi, "dpi_awareness": dpi_awareness,
                      "started_at": datetime.now().astimezone().isoformat(timespec="seconds")})
                for line in sys.stdin:
                    request = json.loads(line)
                    if request.get("stop"):
                        break
                    name = request["name"]
                    if Path(name).name != name or not name.endswith(".png"):
                        raise ValueError("Expected a PNG basename")
                    emit(capture_frame(handle, args.output / name, request["marker"], args.scale))
            finally:
                for handle in browser_windows(desktop, process_id):
                    win32gui.PostMessage(handle, win32con.WM_CLOSE, 0, 0)
                if win32event.WaitForSingleObject(process, 5000) != 0:
                    win32api.TerminateProcess(process, 0)
                thread.Close()
                process.Close()
    finally:
        desktop.CloseDesktop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
