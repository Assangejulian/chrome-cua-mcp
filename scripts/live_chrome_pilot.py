"""Interactive and one-shot live Chrome pilot for testing local GGUF vision-action models."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Optional

import ctypes

from PIL import Image, ImageChops, ImageDraw
import win32con
import win32gui
import win32process

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from action_executor import check_corner_failsafe, execute_action, move_mouse_abs
from action_schema import ActionValidationError, ParsedAction, parse_and_validate_action
from m0_bench_vlm import (
    M1_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    call_vlm,
    encode_image_b64,
    resize_to_token_budget,
    wait_for_server,
)
from roi_utils import (
    ChromeWindowInfo,
    Rect,
    capture_chrome_window,
    enable_per_monitor_dpi_awareness,
    find_chrome_windows,
    inspect_chrome_window,
    is_point_on_window,
    is_window_ready,
    norm_to_screen,
    restore_foreground_if_needed,
)


def focus_chrome_window(info: ChromeWindowInfo, settle_s: float = 0.2) -> ChromeWindowInfo:
    """Restore if minimized, bring Chrome window to foreground, and refresh geometry."""
    restore_foreground_if_needed(info.hwnd)
    user32 = ctypes.windll.user32

    def _try_activate() -> None:
        win32gui.ShowWindow(info.hwnd, win32con.SW_SHOW)
        win32gui.BringWindowToTop(info.hwnd)
        win32gui.SetForegroundWindow(info.hwnd)

    try:
        _try_activate()
    except win32gui.error as exc:
        print(f"[WARN] Could not switch focus to Chrome automatically ({exc}).")

    if win32gui.GetForegroundWindow() != info.hwnd:
        cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
        fg_hwnd = win32gui.GetForegroundWindow()
        try:
            fg_tid = win32process.GetWindowThreadProcessId(fg_hwnd)[0] if fg_hwnd else 0
        except win32gui.error:
            fg_tid = 0
        try:
            target_tid = win32process.GetWindowThreadProcessId(info.hwnd)[0] if info.hwnd else 0
        except win32gui.error:
            target_tid = 0

        attached_pairs: list[tuple[int, int]] = []
        try:
            for tid in (fg_tid, target_tid):
                if tid and tid != cur_tid and (cur_tid, tid) not in attached_pairs:
                    if user32.AttachThreadInput(cur_tid, tid, True):
                        attached_pairs.append((cur_tid, tid))
            if fg_tid and target_tid and fg_tid != target_tid and fg_tid != cur_tid and target_tid != cur_tid:
                if user32.AttachThreadInput(fg_tid, target_tid, True):
                    attached_pairs.append((fg_tid, target_tid))
            try:
                _try_activate()
            except win32gui.error:
                pass
        finally:
            for src_tid, dst_tid in reversed(attached_pairs):
                user32.AttachThreadInput(src_tid, dst_tid, False)

    if win32gui.GetForegroundWindow() != info.hwnd:
        vk_menu = 0x12
        keyeventf_keyup = 0x0002
        try:
            user32.keybd_event(vk_menu, 0, 0, 0)
            user32.keybd_event(vk_menu, 0, keyeventf_keyup, 0)
            _try_activate()
        except win32gui.error as exc:
            print(f"[WARN] Could not switch focus to Chrome automatically ({exc}).")

    time.sleep(settle_s)
    return inspect_chrome_window(info.hwnd)


def select_chrome_window(auto_focus: bool = True) -> Optional[ChromeWindowInfo]:
    """Pick the top Chrome window (preferring non-minimized foreground) and optionally focus it."""
    wins = find_chrome_windows()
    if not wins:
        return None
    non_min = [w for w in wins if not w.is_minimized]
    target = non_min[0] if non_min else wins[0]
    if auto_focus:
        target = focus_chrome_window(target)
    return target


def save_prediction_overlay(
    img: Image.Image,
    parsed: ParsedAction,
    raw_text: str,
    out_path: Path,
) -> Path:
    """Render crosshair and action banner onto captured Chrome image for visual inspection."""
    vis = img.copy()
    draw = ImageDraw.Draw(vis)
    w, h = vis.size

    if parsed.coordinate is not None:
        nx, ny = parsed.coordinate
        px = min(max(round(nx / 1000.0 * (w - 1)), 0), w - 1)
        py = min(max(round(ny / 1000.0 * (h - 1)), 0), h - 1)
        r = 14
        draw.ellipse([(px - r, py - r), (px + r, py + r)], outline=(255, 40, 40), width=3)
        draw.line([(px - 22, py), (px + 22, py)], fill=(255, 40, 40), width=3)
        draw.line([(px, py - 22), (px, py + 22)], fill=(255, 40, 40), width=3)
        draw.ellipse([(px - 3, py - 3), (px + 3, py + 3)], fill=(0, 230, 118))

    banner = f"{parsed.action} coord={parsed.coordinate} | raw={raw_text}"
    draw.rectangle([(0, 0), (w, 28)], fill=(20, 20, 24))
    draw.text((8, 7), banner[:160], fill=(255, 255, 255))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    vis.save(out_path, format="PNG")
    return out_path


def resolve_model_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    server_exe = PROJECT_ROOT / "tools" / "llama_cpp" / "bin" / "llama-server.exe"
    model_dir = args.model_dir
    model_path = model_dir / f"{args.model_prefix}{args.quant}.gguf"
    mmproj_path = model_dir / args.mmproj_name
    for p, label in [(server_exe, "llama-server.exe"), (model_path, "model"), (mmproj_path, "mmproj")]:
        if not p.exists():
            raise FileNotFoundError(f"Missing {label}: {p}")
    return server_exe, model_path, mmproj_path


def start_llama_server(args: argparse.Namespace) -> tuple[subprocess.Popen[str], str]:
    server_exe, model_path, mmproj_path = resolve_model_paths(args)
    with socket.socket() as probe:
        probe.settimeout(0.5)
        if probe.connect_ex(("127.0.0.1", args.port)) == 0:
            raise RuntimeError(f"Local port {args.port} is already in use. Choose a different --port.")
    cmd = [
        str(server_exe), "--model", str(model_path), "--mmproj", str(mmproj_path),
        "--ctx-size", "8192", "--n-gpu-layers", "99", "--image-max-tokens", str(args.max_image_tokens),
        "--reasoning", "off", "--jinja", "--host", "127.0.0.1", "--port", str(args.port),
    ]
    if args.min_image_tokens is not None:
        cmd.extend(["--image-min-tokens", str(args.min_image_tokens)])
    cmd.extend(str(x) for x in (getattr(args, "server_extra_args", None) or ()))
    log_path = PROJECT_ROOT / "artifacts" / "live_chrome_server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Starting llama-server ({model_path.name})...")
    with log_path.open("a", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    return proc, f"http://127.0.0.1:{args.port}"


def stop_llama_server(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def execute_confirmed_action(info: ChromeWindowInfo, frame: Image.Image, action: ParsedAction) -> tuple[bool, str]:
    try:
        current = capture_chrome_window(info)
    except (RuntimeError, win32gui.error):
        return False, "window_lock_invalidated"
    if current.size != frame.size:
        return False, "frame_changed"
    difference = ImageChops.difference(frame, current)
    if action.coordinate is None:
        if difference.getbbox() is not None:
            return False, "frame_changed"
    else:
        red, green, blue = difference.split()
        significant = ImageChops.lighter(ImageChops.lighter(red, green), blue).point(
            [0 if value < 16 else 255 for value in range(256)]
        )
        width, height = frame.size
        x, y = norm_to_screen(*action.coordinate, Rect(0, 0, width, height))
        radius = max(1, round(48 * info.dpi_scale))
        target = significant.crop((max(0, x - radius), max(0, y - radius), min(width, x + radius + 1), min(height, y + radius + 1)))
        if target.getbbox() is not None:
            return False, "target_changed"
        if width * height - significant.histogram()[0] > width * height * 0.02:
            return False, "frame_changed"
    return execute_action(action, info.hwnd, info.dwm_rect)


def run_single_prediction(
    base_url: str,
    info: Optional[ChromeWindowInfo],
    instruction: str,
    max_image_tokens: int,
    sys_prompt: str,
    preview_dir: Optional[Path],
    raw_img: Optional[Image.Image] = None,
) -> tuple[Optional[ChromeWindowInfo], Image.Image, str, Optional[ParsedAction], Optional[tuple[int, int]], float, Optional[Path]]:
    """Predict from a Chrome frame, validate the action, and optionally save a preview."""
    t_step0 = time.perf_counter()
    if raw_img is None:
        if info is None:
            raise ValueError("Either ChromeWindowInfo or raw_img must be provided")
        raw_img = capture_chrome_window(info)
    roi = info.dwm_rect if info is not None else Rect(0, 0, raw_img.size[0], raw_img.size[1])
    resized = resize_to_token_budget(raw_img, max_tokens=max_image_tokens)
    img_b64 = encode_image_b64(resized)

    t_inf0 = time.perf_counter()
    raw_text, usage = call_vlm(base_url, img_b64, instruction, system_prompt=sys_prompt)
    t_inf = time.perf_counter() - t_inf0

    parsed: Optional[ParsedAction] = None
    screen_pt: Optional[tuple[int, int]] = None
    overlay_path: Optional[Path] = None
    try:
        parsed = parse_and_validate_action(raw_text)
        if parsed.coordinate is not None:
            screen_pt = norm_to_screen(parsed.coordinate[0], parsed.coordinate[1], roi)
        if preview_dir is not None:
            ts = datetime.now().strftime("%H%M%S_%f")[:-3]
            overlay_path = save_prediction_overlay(raw_img, parsed, raw_text, preview_dir / f"preview_{ts}.png")
    except ActionValidationError as exc:
        print(f"[ERROR] Invalid action JSON ({exc}): {raw_text}")

    t_total = time.perf_counter() - t_step0
    print(
        f"[VLM] infer={t_inf:.3f}s step={t_total:.3f}s tokens={usage.get('prompt_tokens')}+{usage.get('completion_tokens')} raw={raw_text}"
    )
    if parsed and screen_pt:
        print(f"[TARGET] norm={parsed.coordinate} -> screen_px={screen_pt} (ROI={roi.to_tuple()})")
    if overlay_path:
        print(f"[PREVIEW] {overlay_path}")
    return info, raw_img, raw_text, parsed, screen_pt, t_total, overlay_path


def default_local_model(models_root: Path = PROJECT_ROOT / "models") -> tuple[Path, str, str]:
    """Return (model_dir, model_prefix, mmproj_name) for the newest available local GGUF model."""
    candidates = (
        ("m4_gguf", "m4-qwen35-2b-", "m4-mmproj-f16.gguf"),
        ("m35_gguf", "m35-qwen35-2b-", "m35-mmproj-f16.gguf"),
        ("m3_gguf", "m3-qwen35-2b-", "m3-mmproj-f16.gguf"),
        ("m2_gguf", "m2-qwen35-2b-", "m2-mmproj-f16.gguf"),
        ("qwen35_2b_gguf", "Qwen3.5-2B-", "mmproj-F16.gguf"),
    )
    for directory, prefix, mmproj in candidates:
        model_dir = models_root / directory
        if (model_dir / f"{prefix}Q4_K_M.gguf").is_file() and (model_dir / mmproj).is_file():
            return model_dir, prefix, mmproj
    return models_root / "m3_gguf", "m3-qwen35-2b-", "m3-mmproj-f16.gguf"


def main() -> int:
    default_dir, default_prefix, default_mmproj = default_local_model()
    parser = argparse.ArgumentParser(description="Live Chrome test harness for local Qwen3.5 GGUF GUI pilot.")
    parser.add_argument("--quant", default="Q4_K_M", choices=["Q4_K_M", "Q8_0"])
    parser.add_argument("--model-dir", type=Path, default=default_dir)
    parser.add_argument("--model-prefix", default=default_prefix)
    parser.add_argument("--mmproj-name", default=default_mmproj)
    parser.add_argument("--prompt-style", choices=["m1", "m0"], default="m1")
    parser.add_argument("--max-image-tokens", type=int, default=1024)
    parser.add_argument("--min-image-tokens", type=int, help="Set a minimum visual token budget in llama-server.")
    parser.add_argument("--port", type=int, default=18085)
    parser.add_argument("--ui", action="store_true", help="Open the floating Chrome pilot panel.")
    parser.add_argument("--instruction", default="", help="Run a single instruction cleanly instead of interactive REPL.")
    parser.add_argument("--image", type=Path, default=None, help="Predict offline against a saved screenshot PNG/JPG (forces preview mode).")
    parser.add_argument(
        "--mode",
        choices=["preview", "move", "execute", "confirm"],
        default="confirm",
        help="preview=PNG only; move=move cursor only; execute=click immediately; confirm=ask before action.",
    )
    parser.add_argument("--no-focus", action="store_true", help="Do not switch foreground focus to Chrome automatically.")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.max_image_tokens < 1 or (args.min_image_tokens is not None and not 1 <= args.min_image_tokens <= args.max_image_tokens):
        parser.error("image token limits must be positive, with minimum no greater than maximum")
    if args.ui and (args.instruction or args.image is not None or args.no_focus):
        parser.error("--ui cannot be used with --instruction, --image, or --no-focus")

    mode = enable_per_monitor_dpi_awareness()
    print(f"DPI mode: {mode}")
    if args.ui:
        from chrome_pilot_panel import run_panel

        run_panel(args)
        return 0

    sys_prompt = M1_SYSTEM_PROMPT if args.prompt_style == "m1" else SYSTEM_PROMPT
    preview_dir = PROJECT_ROOT / "artifacts" / "live_previews"
    proc, base_url = start_llama_server(args)
    try:
        load_s = wait_for_server(base_url, proc, timeout_s=90.0)
        print(f"[READY] llama-server loaded in {load_s:.2f}s at {base_url}")

        def handle_instruction(inst: str) -> None:
            if args.image is not None:
                with Image.open(args.image) as source:
                    img = source.convert("RGB")
                run_single_prediction(base_url, None, inst, args.max_image_tokens, sys_prompt, preview_dir, raw_img=img)
                return

            info = select_chrome_window(auto_focus=not args.no_focus)
            if info is None:
                print("[WINDOW] No Google Chrome window found in this desktop session. Open Chrome and try again.")
                return
            print(f"[WINDOW] hwnd={info.hwnd} title={info.title!r} dpi={info.dpi} ({info.dpi_scale}x) roi={info.dwm_rect.to_tuple()}")
            _, frame, _, parsed, screen_pt, _, _ = run_single_prediction(
                base_url, info, inst, args.max_image_tokens, sys_prompt, preview_dir
            )
            if parsed is None or args.mode == "preview":
                return

            if check_corner_failsafe():
                print("[ABORT] Failsafe corner (<=4, <=4) active; skipped mouse/keyboard action.")
                return

            chosen = args.mode
            if chosen in ("move", "confirm") and screen_pt is not None:
                if is_window_ready(info.hwnd, info.dwm_rect) and is_point_on_window(info.hwnd, screen_pt):
                    move_mouse_abs(*screen_pt)
                    print("[HOVER] Mouse moved to predicted target (no click sent yet).")

            if chosen == "confirm":
                chosen = "execute" if input("Action choice: [y=execute real action | Enter=skip]: ").strip().lower() == "y" else "preview"

            if chosen == "execute":
                if not args.no_focus:
                    focus_chrome_window(info, settle_s=0.15)
                ok, status = execute_confirmed_action(info, frame, parsed)
                print(f"[EXEC] ok={ok} status={status}")

        if args.instruction:
            try:
                handle_instruction(args.instruction)
            except (RuntimeError, win32gui.error) as exc:
                print(f"[ERROR] {exc}", file=sys.stderr)
                return 1
        else:
            print("Interactive Chrome Pilot ready. Type an instruction (or 'q' to quit).")
            while True:
                try:
                    inst = input("\nInstruction> ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not inst or inst.lower() in ("q", "quit", "exit"):
                    break
                try:
                    handle_instruction(inst)
                except (RuntimeError, win32gui.error) as exc:
                    print(f"[ERROR] {exc}", file=sys.stderr)
    finally:
        stop_llama_server(proc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
