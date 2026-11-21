"""A/B llama-server launch flags on distinct screenshots: load time, VRAM, host memory, latency."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import random
import statistics
import sys
import time

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from PIL import ImageDraw

from live_chrome_pilot import default_local_model, start_llama_server, stop_llama_server
from m0_bench_vlm import (
    M1_SYSTEM_PROMPT,
    call_vlm,
    encode_image_b64,
    make_synthetic_chrome_frame,
    query_gpu_memory_mib,
    resize_to_token_budget,
    wait_for_server,
)

CONFIGS: dict[str, list[str]] = {
    "default": [],
    "np1": ["-np", "1"],
    "np1_cram0": ["-np", "1", "--cache-ram", "0"],
    "np1_cram0_fa": ["-np", "1", "--cache-ram", "0", "-fa", "on"],
}


class _PMC(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _proc_mem_mib(pid: int) -> dict[str, int]:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000 | 0x0010, False, pid)
    if not h:
        return {}
    try:
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(pmc)
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            return {}
        return {"working_set_mib": pmc.WorkingSetSize >> 20, "commit_mib": pmc.PagefileUsage >> 20}
    finally:
        k32.CloseHandle(h)


def _frames(n: int, seed: int = 7) -> list[tuple[str, list[int]]]:
    """Distinct synthetic Chrome frames (random clutter) with the ground-truth box of the '+' new-tab button."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        img, targets = make_synthetic_chrome_frame(1280, 800)
        d = ImageDraw.Draw(img)
        for _j in range(14):
            x, y = rng.randint(20, 1100), rng.randint(330, 740)
            d.rectangle([x, y, x + rng.randint(30, 160), y + rng.randint(10, 40)], fill=tuple(rng.randint(60, 240) for _c in range(3)))
        out.append((encode_image_b64(resize_to_token_budget(img, max_tokens=1024)), targets))
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18250)
    parser.add_argument("--n", type=int, default=24)
    parser.add_argument("--configs", nargs="*", default=list(CONFIGS))
    args = parser.parse_args()

    model_dir, prefix, mmproj = default_local_model()
    frames = _frames(args.n)
    target0 = frames[0][1][0]
    instruction = str(target0.get("instruction") or "点击新标签页按钮")
    report: dict[str, object] = {"n_requests": args.n, "instruction": instruction, "configs": {}}

    for i, name in enumerate(args.configs):
        ns = argparse.Namespace(
            port=args.port + i, max_image_tokens=1024, min_image_tokens=None, model_dir=model_dir,
            model_prefix=prefix, quant="Q4_K_M", mmproj_name=mmproj, server_extra_args=CONFIGS[name],
        )
        gpu_before = query_gpu_memory_mib()["used_mib"]
        proc, base = start_llama_server(ns)
        try:
            load_s = wait_for_server(base, proc, timeout_s=90.0)
            call_vlm(base, frames[0][0], instruction, max_tokens=96, system_prompt=M1_SYSTEM_PROMPT)
            gpu_loaded = query_gpu_memory_mib()["used_mib"]
            mem_loaded = _proc_mem_mib(proc.pid)
            lat, outputs = [], []
            for img_b64, _t in frames:
                t = time.perf_counter()
                text, usage = call_vlm(base, img_b64, instruction, max_tokens=96, system_prompt=M1_SYSTEM_PROMPT)
                lat.append((time.perf_counter() - t) * 1000.0)
                outputs.append(text.strip())
            report["configs"][name] = {
                "flags": CONFIGS[name],
                "load_s": round(load_s, 2),
                "vram_delta_mib": gpu_loaded - gpu_before,
                "vram_after_run_delta_mib": query_gpu_memory_mib()["used_mib"] - gpu_before,
                "host_mem_after_load": mem_loaded,
                "host_mem_after_run": _proc_mem_mib(proc.pid),
                "latency_ms": {"median": round(statistics.median(lat), 1), "p90": round(sorted(lat)[int(len(lat) * 0.9) - 1], 1), "min": round(min(lat), 1), "max": round(max(lat), 1)},
                "last_usage": usage,
                "outputs": outputs,
            }
            print(name, json.dumps({k: v for k, v in report["configs"][name].items() if k != "outputs"}, ensure_ascii=False), flush=True)
        finally:
            stop_llama_server(proc)
            time.sleep(1.5)

    cfgs = report["configs"]
    base_out = cfgs.get("default", {}).get("outputs") if isinstance(cfgs, dict) else None
    if base_out:
        for name, row in cfgs.items():
            row["outputs_identical_to_default"] = sum(a == b for a, b in zip(row["outputs"], base_out))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print({n: r.get("outputs_identical_to_default") for n, r in cfgs.items()})


if __name__ == "__main__":
    main()
