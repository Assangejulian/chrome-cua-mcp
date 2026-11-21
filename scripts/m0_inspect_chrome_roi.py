"""Non-destructive M0 diagnostic script: verify DPI awareness, Chrome ROI geometry, coordinate clamp, and schema checks."""

from __future__ import annotations

from pathlib import Path
import sys

from PIL import ImageDraw

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from action_schema import ActionValidationError, parse_and_validate_action
from roi_utils import (
    Rect,
    capture_chrome_window,
    enable_per_monitor_dpi_awareness,
    find_chrome_windows,
    norm_to_screen,
    screen_to_norm,
)


def run_unit_checks() -> None:
    """Verify coordinate clamping, negative multi-monitor mapping, and action validation."""
    neg_roi = Rect(left=-1920, top=-100, right=0, bottom=980)  # 1920x1080 secondary monitor
    sx0, sy0 = norm_to_screen(0, 0, neg_roi)
    assert (sx0, sy0) == (-1920, -100), f"Expected (-1920, -100), got {(sx0, sy0)}"
    sx1, sy1 = norm_to_screen(1000, 1000, neg_roi)
    assert (sx1, sy1) == (-1, 979), f"Expected (-1, 979), got {(sx1, sy1)}"
    assert screen_to_norm(-1, 979, neg_roi) == (1000, 1000)
    assert screen_to_norm(0, 980, neg_roi) is None

    ok_click = parse_and_validate_action('{"action": "left_click", "coordinate": [268, 19]}')
    assert ok_click.coordinate == (268, 19)
    ok_scroll = parse_and_validate_action('{"action": "scroll", "coordinate": [500, 500], "direction": "down", "amount": 3}')
    assert ok_scroll.direction == "down" and ok_scroll.amount == 3
    ok_not_found = parse_and_validate_action('{"action": "not_found", "reason": "missing"}')
    assert ok_not_found.coordinate is None

    for bad in [
        "not json at all",
        '{"action": "left_click", "coordinate": [1001, 20]}',
        '{"action": "unknown", "coordinate": [10, 10]}',
        '{"action": "type", "text": ""}',
    ]:
        try:
            parse_and_validate_action(bad)
        except ActionValidationError:
            continue
        raise AssertionError(f"Expected ActionValidationError for: {bad}")
    print("[PASS] Coordinate mapping, negative-monitor bounds, clamp, and schema unit checks passed.")


def main() -> int:
    dpi_mode = enable_per_monitor_dpi_awareness()
    print(f"DPI awareness mode: {dpi_mode}")
    run_unit_checks()

    windows = find_chrome_windows()
    if not windows:
        print("[INFO] No visible Chrome window found right now; skipping live ROI screenshot.")
        return 0

    for idx, win in enumerate(windows):
        print(
            f"[Chrome #{idx}] hwnd={win.hwnd} fg={win.is_foreground} min={win.is_minimized} "
            f"dpi={win.dpi} ({win.dpi_scale*100:.0f}%) title={win.title!r}"
        )
        print(
            f"  GetWindowRect={win.window_rect.to_tuple()} ({win.window_rect.width}x{win.window_rect.height})\n"
            f"  DWM_EXTENDED ={win.dwm_rect.to_tuple()} ({win.dwm_rect.width}x{win.dwm_rect.height})\n"
            f"  ClientScreen ={win.client_rect.to_tuple()} ({win.client_rect.width}x{win.client_rect.height})"
        )

    target = next((w for w in windows if w.is_foreground and not w.is_minimized), None)
    if target is None:
        print("[INFO] Bring the intended Chrome window to the foreground to capture its ROI.")
        return 0

    img = capture_chrome_window(target)
    out_dir = PROJECT_ROOT / "artifacts"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / "m0_chrome_dwm_raw.png"
    annotated_path = out_dir / "m0_chrome_dwm_check.png"
    img.save(raw_path)

    vis = img.copy()
    draw = ImageDraw.Draw(vis)
    w, h = vis.size
    draw.rectangle([0, 0, w - 1, h - 1], outline=(0, 255, 0), width=3)
    # Visualize client area relative to DWM bounds to verify tab bar inclusion
    cl = target.client_rect.left - target.dwm_rect.left
    ct = target.client_rect.top - target.dwm_rect.top
    cr = target.client_rect.right - target.dwm_rect.left - 1
    cb = target.client_rect.bottom - target.dwm_rect.top - 1
    draw.rectangle([cl, ct, cr, cb], outline=(255, 80, 80), width=2)
    vis.save(annotated_path)
    print(f"[PASS] Saved Chrome DWM capture ({w}x{h}) to: {raw_path}")
    print(f"[PASS] Saved DWM(green) vs Client(red) check image to: {annotated_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
