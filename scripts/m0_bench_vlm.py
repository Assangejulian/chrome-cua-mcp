"""M0 local RTX 4060 benchmark harness using llama-server.exe and unsloth/Qwen3.5-2B-GGUF."""

from __future__ import annotations

import argparse
import base64
from io import BytesIO
import json
from pathlib import Path
import statistics
import subprocess
import threading
import sys
import time
from typing import Any

from PIL import Image, ImageDraw
import requests

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from action_schema import ActionValidationError, parse_and_validate_action
from roi_utils import (
    Rect,
    capture_roi,
    enable_per_monitor_dpi_awareness,
    find_chrome_windows,
    norm_to_screen,
)

SYSTEM_PROMPT = (
    "You are a Chrome browser GUI action assistant. Given a Chrome window screenshot and a user instruction, "
    "output strictly one JSON object for the next action using [0, 1000] normalized integer coordinates, e.g. "
    '{"action": "left_click", "coordinate": [520, 310]} or {"action": "not_found", "reason": "..."}. '
    "Do not output explanations or markdown."
)

M1_SYSTEM_PROMPT = (
    "You are a Chrome browser GUI action assistant. Given a Chrome window screenshot and a user instruction, "
    "output strictly one JSON object for the next action using [0, 1000] normalized integer coordinates. "
    "Do not output explanations or markdown."
)


def resize_to_token_budget(img: Image.Image, max_tokens: int = 1024, grid: int = 32) -> Image.Image:
    """Downscale image preserving aspect ratio and aligning dimensions to 32px grid."""
    w, h = img.size
    max_pixels = max_tokens * grid * grid
    cur_pixels = w * h
    if cur_pixels > max_pixels:
        scale = (max_pixels / cur_pixels) ** 0.5
        w = max(grid, int(w * scale))
        h = max(grid, int(h * scale))
    new_w = max(grid, round(w / grid) * grid)
    new_h = max(grid, round(h / grid) * grid)
    while new_w * new_h > max_pixels and new_w > grid and new_h > grid:
        if new_w >= new_h:
            new_w -= grid
        else:
            new_h -= grid
    if (new_w, new_h) != img.size:
        return img.resize((new_w, new_h), Image.Resampling.BICUBIC)
    return img


def encode_image_b64(img: Image.Image) -> str:
    """Encode PIL RGB image as PNG base64 data URI."""
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=False)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def query_gpu_memory_mib() -> dict[str, int]:
    """Read current GPU memory usage in MiB via nvidia-smi."""
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.total,memory.used,memory.free", "--format=csv,noheader,nounits"],
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).strip()
    total, used, free = [int(x.strip()) for x in out.splitlines()[0].split(",")]
    return {"total_mib": total, "used_mib": used, "free_mib": free}


def make_synthetic_chrome_frame(width: int = 1280, height: int = 800) -> tuple[Image.Image, list[dict[str, Any]]]:
    """Create a synthetic Chrome-like window screenshot with known ground-truth bboxes for offline M0 testing."""
    img = Image.new("RGB", (width, height), (245, 247, 250))
    draw = ImageDraw.Draw(img)

    # Top tab bar + address bar simulation
    draw.rectangle([0, 0, width, 42], fill=(222, 225, 230))
    draw.rectangle([16, 8, 220, 42], fill=(255, 255, 255))
    draw.text((32, 18), "New Tab", fill=(32, 33, 36))
    # New tab '+' button bbox
    plus_box = [232, 10, 260, 38]
    draw.rectangle(plus_box, fill=(210, 214, 220), outline=(120, 120, 120))
    draw.text((242, 17), "+", fill=(32, 33, 36))

    # Close window 'X' button top-right
    close_box = [width - 42, 6, width - 10, 36]
    draw.rectangle(close_box, fill=(232, 17, 35))
    draw.text((width - 30, 14), "X", fill=(255, 255, 255))

    # Toolbar + address bar
    draw.rectangle([0, 42, width, 86], fill=(255, 255, 255))
    addr_box = [140, 48, width - 120, 80]
    draw.rectangle(addr_box, fill=(241, 243, 244), outline=(180, 185, 190))
    draw.text((156, 58), "Search Google or type a URL", fill=(95, 99, 104))

    # Page content: search box + blue submit button
    search_box = [380, 320, 820, 368]
    draw.rectangle(search_box, fill=(255, 255, 255), outline=(66, 133, 244), width=2)
    draw.text((396, 338), "Enter keywords...", fill=(130, 130, 130))

    submit_box = [835, 320, 945, 368]
    draw.rectangle(submit_box, fill=(26, 115, 232))
    draw.text((865, 338), "Search", fill=(255, 255, 255))

    def to_norm_box(b: list[int]) -> list[int]:
        return [
            round(b[0] / (width - 1) * 1000),
            round(b[1] / (height - 1) * 1000),
            round(b[2] / (width - 1) * 1000),
            round(b[3] / (height - 1) * 1000),
        ]

    targets = [
        {"instruction": "Click the '+' button to open a new tab", "bbox": to_norm_box(plus_box)},
        {"instruction": "Click the blue Search button", "bbox": to_norm_box(submit_box)},
        {"instruction": "Click the Chrome address bar", "bbox": to_norm_box(addr_box)},
        {"instruction": "Click the red close window button 'X' at top right", "bbox": to_norm_box(close_box)},
    ]
    return img, targets


def wait_for_server(base_url: str, proc: subprocess.Popen[str], timeout_s: float = 60.0) -> float:
    """Wait until llama-server /health returns 200 and return load duration in seconds."""
    t0 = time.perf_counter()
    deadline = t0 + timeout_s
    while time.perf_counter() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"llama-server exited prematurely with code {proc.returncode}")
        try:
            resp = requests.get(f"{base_url}/health", timeout=2.0)
            if resp.status_code == 200:
                return time.perf_counter() - t0
        except requests.RequestException:
            pass
        time.sleep(0.25)
    raise TimeoutError(f"llama-server did not become healthy within {timeout_s}s")


_VLM_HTTP = threading.local()


def _vlm_session() -> requests.Session:
    """Per-thread keep-alive session; trust_env=False skips the per-request proxy lookup for localhost."""
    ses = getattr(_VLM_HTTP, "session", None)
    if ses is None:
        ses = requests.Session()
        ses.trust_env = False
        _VLM_HTTP.session = ses
    return ses


def call_vlm(
    base_url: str,
    img_b64: str,
    instruction: str,
    max_tokens: int = 64,
    system_prompt: str = SYSTEM_PROMPT,
) -> tuple[str, dict[str, Any]]:
    """Send a single non-thinking chat completion request to llama-server."""
    payload = {
        "model": "qwen3.5-2b",
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": img_b64}},
                    {"type": "text", "text": instruction},
                ],
            },
        ],
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    resp = _vlm_session().post(f"{base_url}/v1/chat/completions", json=payload, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    return content, data.get("usage", {})


def point_in_box(pt: tuple[int, int] | None, bbox: list[int]) -> bool:
    if pt is None:
        return False
    x, y = pt
    return bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return ordered[idx]


def run_benchmark(
    quant: str,
    runs: int,
    max_image_tokens: int,
    port: int,
    model_dir: Path | None = None,
    model_prefix: str = "Qwen3.5-2B-",
    mmproj_name: str = "mmproj-F16.gguf",
    system_prompt: str = SYSTEM_PROMPT,
) -> dict[str, Any]:
    """Launch llama-server for one GGUF quantization and measure cold/warm latencies, VRAM, and accuracy."""
    server_exe = PROJECT_ROOT / "tools" / "llama_cpp" / "bin" / "llama-server.exe"
    resolved_model_dir = model_dir or (PROJECT_ROOT / "models" / "qwen35_2b_gguf")
    model_path = resolved_model_dir / f"{model_prefix}{quant}.gguf"
    mmproj_path = resolved_model_dir / mmproj_name
    if not server_exe.exists() or not model_path.exists() or not mmproj_path.exists():
        raise FileNotFoundError(
            f"Missing binary or GGUF model files for {quant}: exe={server_exe}, model={model_path}, mmproj={mmproj_path}"
        )

    base_url = f"http://127.0.0.1:{port}"
    log_dir = PROJECT_ROOT / "artifacts"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / f"llama_server_{quant}.log", "w", encoding="utf-8")

    cmd = [
        str(server_exe),
        "--model",
        str(model_path),
        "--mmproj",
        str(mmproj_path),
        "--ctx-size",
        "8192",
        "--n-gpu-layers",
        "99",
        "--image-max-tokens",
        str(max_image_tokens),
        "--reasoning",
        "off",
        "--jinja",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    vram_before = query_gpu_memory_mib()
    t_start = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT, text=True)
    try:
        load_s = wait_for_server(base_url, proc, timeout_s=90.0)

        # Prepare test targets: prefer live Chrome screenshot if visible, plus synthetic ground-truth cases
        chrome_wins = [w for w in find_chrome_windows() if not w.is_minimized]
        synth_img, synth_cases = make_synthetic_chrome_frame()
        synth_resized = resize_to_token_budget(synth_img, max_tokens=max_image_tokens)

        live_img = capture_roi(chrome_wins[0].dwm_rect) if chrome_wins else None
        live_roi = chrome_wins[0].dwm_rect if chrome_wins else None

        # Cold first inference
        t_c0 = time.perf_counter()
        cold_b64 = encode_image_b64(synth_resized)
        cold_raw, cold_usage = call_vlm(base_url, cold_b64, synth_cases[0]["instruction"], system_prompt=system_prompt)
        cold_first_infer_s = time.perf_counter() - t_c0
        cold_total_s = time.perf_counter() - t_start

        # Warmed-up runs
        infer_latencies: list[float] = []
        step_latencies: list[float] = []
        valid_json = 0
        hits = 0
        peak_used_mib = vram_before["used_mib"]
        sample_outputs: list[dict[str, Any]] = []

        for i in range(runs):
            case = synth_cases[i % len(synth_cases)]
            t_step0 = time.perf_counter()
            if live_roi is not None and i == 0:
                _ = capture_roi(live_roi)
            resized = resize_to_token_budget(synth_img, max_tokens=max_image_tokens)
            b64 = encode_image_b64(resized)
            t_inf0 = time.perf_counter()
            raw, usage = call_vlm(base_url, b64, case["instruction"], system_prompt=system_prompt)
            t_inf = time.perf_counter() - t_inf0

            ok_schema = False
            hit = False
            parsed_coord = None
            try:
                parsed = parse_and_validate_action(raw)
                ok_schema = True
                valid_json += 1
                parsed_coord = parsed.coordinate
                if parsed_coord is not None:
                    _ = norm_to_screen(parsed_coord[0], parsed_coord[1], live_roi or Rect(0, 0, 1280, 800))
                hit = point_in_box(parsed_coord, case["bbox"])
                if hit:
                    hits += 1
            except ActionValidationError:
                pass
            t_step = time.perf_counter() - t_step0

            infer_latencies.append(t_inf)
            step_latencies.append(t_step)
            vram_now = query_gpu_memory_mib()["used_mib"]
            peak_used_mib = max(peak_used_mib, vram_now)
            if i < len(synth_cases):
                sample_outputs.append(
                    {
                        "instruction": case["instruction"],
                        "gt_bbox": case["bbox"],
                        "raw": raw,
                        "valid_json": ok_schema,
                        "pred_coord": parsed_coord,
                        "hit": hit,
                        "usage": usage,
                        "t_infer_s": round(t_inf, 3),
                        "t_step_s": round(t_step, 3),
                    }
                )

        # Optional live Chrome capture inference test (no click sent!)
        live_test = None
        if live_img is not None and live_roi is not None:
            t_l0 = time.perf_counter()
            live_resized = resize_to_token_budget(live_img, max_tokens=max_image_tokens)
            live_b64 = encode_image_b64(live_resized)
            live_raw, live_usage = call_vlm(
                base_url, live_b64, "Click the Chrome address bar", system_prompt=system_prompt
            )
            t_l_step = time.perf_counter() - t_l0
            live_test = {
                "roi": live_roi.to_tuple(),
                "resized_wh": live_resized.size,
                "raw": live_raw,
                "usage": live_usage,
                "t_step_s": round(t_l_step, 3),
            }

        return {
            "quant": quant,
            "max_image_tokens": max_image_tokens,
            "cold_load_s": round(load_s, 3),
            "cold_first_infer_s": round(cold_first_infer_s, 3),
            "cold_total_s": round(cold_total_s, 3),
            "cold_raw": cold_raw,
            "cold_usage": cold_usage,
            "warm_runs": runs,
            "t_infer_p50_s": round(statistics.median(infer_latencies), 3),
            "t_infer_p95_s": round(percentile(infer_latencies, 95), 3),
            "t_step_p50_s": round(statistics.median(step_latencies), 3),
            "t_step_p95_s": round(percentile(step_latencies, 95), 3),
            "json_valid_rate": round(valid_json / runs, 4),
            "synthetic_point_in_box_rate": round(hits / runs, 4),
            "vram_before_mib": vram_before["used_mib"],
            "vram_peak_mib": peak_used_mib,
            "vram_total_mib": vram_before["total_mib"],
            "sample_outputs": sample_outputs,
            "live_chrome_test": live_test,
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quants", nargs="+", default=["Q8_0", "Q4_K_M"])
    parser.add_argument("--runs", type=int, default=8)
    parser.add_argument("--max-image-tokens", type=int, default=1024)
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--model-dir", type=Path, default=PROJECT_ROOT / "models" / "qwen35_2b_gguf")
    parser.add_argument("--model-prefix", default="Qwen3.5-2B-")
    parser.add_argument("--mmproj-name", default="mmproj-F16.gguf")
    parser.add_argument("--prompt-style", choices=["m0", "m1"], default="m0")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "artifacts" / "m0_bench_results.json")
    args = parser.parse_args()

    sys_prompt = M1_SYSTEM_PROMPT if args.prompt_style == "m1" else SYSTEM_PROMPT
    enable_per_monitor_dpi_awareness()
    results = []
    for idx, q in enumerate(args.quants):
        print(f"=== Benchmarking {q} (runs={args.runs}, max_image_tokens={args.max_image_tokens}) ===")
        res = run_benchmark(
            q,
            args.runs,
            args.max_image_tokens,
            args.port + idx,
            model_dir=args.model_dir,
            model_prefix=args.model_prefix,
            mmproj_name=args.mmproj_name,
            system_prompt=sys_prompt,
        )
        results.append(res)
        print(json.dumps({k: v for k, v in res.items() if k != "sample_outputs"}, ensure_ascii=False, indent=2))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[SAVED] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
