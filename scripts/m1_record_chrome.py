"""M1 Chrome collector: maintains pre-click ring buffer, captures t-80ms frames on mouse press, and extracts UIA bbox hints."""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import sys
import threading
import time
from typing import Optional

from PIL import Image
from pynput import keyboard, mouse

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from roi_utils import (
    Rect,
    capture_roi,
    enable_per_monitor_dpi_awareness,
    find_chrome_windows,
    get_dwm_extended_frame_bounds,
    get_window_dpi,
    screen_to_norm,
)


@dataclass
class BufferedFrame:
    timestamp: float
    hwnd: int
    roi: Rect
    dpi_scale: float
    image: Image.Image


def try_get_uia_bbox(screen_x: int, screen_y: int) -> Optional[Rect]:
    """Query Windows UI Automation ElementFromPoint for native Chrome shell bounding box hint."""
    try:
        import comtypes.client  # type: ignore
    except Exception:
        return None
    try:
        uia = comtypes.client.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}")
        # Fallback cleanly if optional UIA typelib is not generated
        from comtypes.gen import UIAutomationClient  # type: ignore

        auto = uia.QueryInterface(UIAutomationClient.IUIAutomation)
        elem = auto.ElementFromPoint((screen_x, screen_y))
        rect = elem.CurrentBoundingRectangle
        if rect.right > rect.left and rect.bottom > rect.top:
            return Rect(rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        return None
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Record Chrome pre-click frames with metadata.")
    parser.add_argument("--session-id", default=datetime.now().strftime("sess_%Y%m%d_%H%M%S"))
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--pre-offset-ms", type=float, default=80.0)
    args = parser.parse_args()

    enable_per_monitor_dpi_awareness()
    out_dir = PROJECT_ROOT / "data" / "recorded_chrome"
    img_dir = out_dir / "images" / args.session_id
    img_dir.mkdir(parents=True, exist_ok=True)
    meta_path = out_dir / f"raw_{args.session_id}.jsonl"

    ring: deque[BufferedFrame] = deque(maxlen=16)
    lock = threading.Lock()
    stop_event = threading.Event()
    sample_idx = 0

    def sampler_loop() -> None:
        interval = 1.0 / max(1.0, args.fps)
        while not stop_event.is_set():
            t0 = time.perf_counter()
            wins = find_chrome_windows()
            fg = next((w for w in wins if w.is_foreground and not w.is_minimized), None)
            if fg is not None:
                try:
                    img = capture_roi(fg.dwm_rect)
                    frame = BufferedFrame(
                        timestamp=time.time(),
                        hwnd=fg.hwnd,
                        roi=fg.dwm_rect,
                        dpi_scale=fg.dpi_scale,
                        image=img,
                    )
                    with lock:
                        ring.append(frame)
                except Exception:
                    pass
            elapsed = time.perf_counter() - t0
            time.sleep(max(0.01, interval - elapsed))

    def select_pre_click_frame(click_ts: float) -> Optional[BufferedFrame]:
        target_ts = click_ts - (args.pre_offset_ms / 1000.0)
        with lock:
            frames = list(ring)
        if not frames:
            return None
        before = [f for f in frames if f.timestamp <= target_ts]
        return before[-1] if before else frames[0]

    def on_click(x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        nonlocal sample_idx
        if not pressed:
            return
        click_ts = time.time()
        frame = select_pre_click_frame(click_ts)
        if frame is None:
            return
        # Ensure click landed inside the buffered Chrome DWM window
        norm_pt = screen_to_norm(x, y, frame.roi)
        if norm_pt is None:
            return
        # Verify Chrome window remained stationary between pre-frame and click
        if get_dwm_extended_frame_bounds(frame.hwnd) != frame.roi:
            return

        action_name = {
            mouse.Button.left: "left_click",
            mouse.Button.right: "right_click",
        }.get(button)
        if action_name is None:
            return

        uia_rect = try_get_uia_bbox(x, y)
        norm_bbox = None
        if uia_rect is not None:
            tl = screen_to_norm(max(uia_rect.left, frame.roi.left), max(uia_rect.top, frame.roi.top), frame.roi)
            br = screen_to_norm(min(uia_rect.right - 1, frame.roi.right - 1), min(uia_rect.bottom - 1, frame.roi.bottom - 1), frame.roi)
            if tl and br and br[0] > tl[0] and br[1] > tl[1]:
                norm_bbox = [tl[0], tl[1], br[0], br[1]]

        sample_idx += 1
        sample_id = f"chrome_{args.session_id}_{sample_idx:05d}"
        rel_img = f"images/{args.session_id}/{sample_idx:05d}.png"
        frame.image.save(out_dir / rel_img, format="PNG")

        record = {
            "sample_id": sample_id,
            "image_path": rel_img,
            "instruction": "",  # Fill during annotation / review pass
            "action": action_name,
            "coordinate": list(norm_pt),
            "bbox": norm_bbox,
            "source": "chrome_recorded",
            "site_domain": "pending_label",
            "session_id": args.session_id,
            "crop_mode": "dwm_chrome",
            "window_rect": list(frame.roi.to_tuple()),
            "dpi_scale": round(get_window_dpi(frame.hwnd) / 96.0, 4),
            "img_w": frame.image.size[0],
            "img_h": frame.image.size[1],
            "frame_lead_ms": round((click_ts - frame.timestamp) * 1000.0, 1),
        }
        with open(meta_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"[REC #{sample_idx}] {action_name} norm={norm_pt} lead={record['frame_lead_ms']}ms uia_bbox={norm_bbox}")

    def on_key_press(key: keyboard.Key) -> Optional[bool]:
        if key == keyboard.Key.esc:
            print("[STOP] ESC pressed, shutting down recorder.")
            stop_event.set()
            return False
        return None

    t = threading.Thread(target=sampler_loop, daemon=True)
    t.start()
    print(f"[READY] Recording Chrome pre-click frames to {meta_path}. Press ESC to stop.")
    with mouse.Listener(on_click=on_click) as m_listener, keyboard.Listener(on_press=on_key_press) as k_listener:
        k_listener.join()
        stop_event.set()
        m_listener.stop()
    t.join(timeout=2.0)
    print(f"[DONE] Recorded {sample_idx} samples in {args.session_id}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
