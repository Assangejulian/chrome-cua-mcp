"""Download portable llama.cpp Windows CUDA binaries and Qwen3.5-2B GGUF + mmproj models for M0."""

from __future__ import annotations

import os
from pathlib import Path
import time
import zipfile

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LLAMA_TAG = "b12441"
LLAMA_ZIPS = [
    f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_TAG}/llama-{LLAMA_TAG}-bin-win-cuda-12.4-x64.zip",
    f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_TAG}/cudart-llama-bin-win-cuda-12.4-x64.zip",
]
GGUF_REPO = "unsloth/Qwen3.5-2B-GGUF"
GGUF_FILES = [
    "mmproj-F16.gguf",
    "Qwen3.5-2B-Q4_K_M.gguf",
    "Qwen3.5-2B-Q8_0.gguf",
]


def download_with_resume(url: str, dest: Path) -> None:
    """Download a file using requests (honors Windows system proxy) if missing or incomplete."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    head = requests.head(url, allow_redirects=True, timeout=30)
    head.raise_for_status()
    expected_size = int(head.headers.get("content-length", 0))
    if dest.exists() and expected_size > 0 and dest.stat().st_size == expected_size:
        print(f"[SKIP] {dest.name} already downloaded ({expected_size / (1024**2):.1f} MiB)")
        return

    tmp_dest = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, 6):
        existing = tmp_dest.stat().st_size if tmp_dest.exists() else 0
        if expected_size > 0 and existing == expected_size:
            tmp_dest.replace(dest)
            return
        headers = {"Range": f"bytes={existing}-"} if existing > 0 else {}
        try:
            with requests.get(url, headers=headers, stream=True, allow_redirects=True, timeout=60) as resp:
                if resp.status_code == 416:
                    tmp_dest.replace(dest)
                    return
                resp.raise_for_status()
                mode = "ab" if existing > 0 and resp.status_code == 206 else "wb"
                if mode == "wb":
                    existing = 0
                downloaded = existing
                last_pct = int(downloaded * 100 / expected_size) if expected_size > 0 else -10
                with open(tmp_dest, mode) as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        if expected_size > 0:
                            pct = int(downloaded * 100 / expected_size)
                            if pct >= last_pct + 10:
                                last_pct = pct
                                print(f"  {dest.name}: {pct}% ({downloaded / (1024**2):.1f}/{expected_size / (1024**2):.1f} MiB)")
            tmp_dest.replace(dest)
            return
        except requests.RequestException as exc:
            print(f"  [RETRY {attempt}/5] {dest.name} interrupted ({exc}), resuming...")
            time.sleep(2.0)
    raise RuntimeError(f"Failed to download {dest.name} after 5 attempts")


def setup_llama_cpp() -> Path:
    """Download and extract llama.cpp Windows CUDA binaries into tools/llama_cpp/bin."""
    tools_dir = PROJECT_ROOT / "tools" / "llama_cpp"
    bin_dir = tools_dir / "bin"
    server_exe = bin_dir / "llama-server.exe"
    cudart_dll = next(bin_dir.glob("cudart64_*.dll"), None) if bin_dir.exists() else None
    if server_exe.exists() and cudart_dll is not None:
        print(f"[SKIP] llama-server.exe already extracted at {server_exe}")
        return server_exe

    bin_dir.mkdir(parents=True, exist_ok=True)
    for url in LLAMA_ZIPS:
        zip_path = tools_dir / url.rsplit("/", 1)[-1]
        print(f"[DOWN] {zip_path.name}")
        download_with_resume(url, zip_path)
        print(f"[UNZIP] {zip_path.name} -> {bin_dir}")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(bin_dir)
    if not server_exe.exists():
        raise FileNotFoundError(f"Expected {server_exe} after unzip")
    return server_exe


def setup_gguf_models() -> Path:
    """Download Qwen3.5-2B GGUF weights and matching mmproj using resumable HTTP requests."""
    model_dir = PROJECT_ROOT / "models" / "qwen35_2b_gguf"
    model_dir.mkdir(parents=True, exist_ok=True)
    for filename in GGUF_FILES:
        url = f"https://huggingface.co/{GGUF_REPO}/resolve/main/{filename}"
        dest = model_dir / filename
        print(f"[HF] Checking/downloading {GGUF_REPO}/{filename}...")
        download_with_resume(url, dest)
        size_mb = dest.stat().st_size / (1024**2)
        print(f"[OK] {filename}: {size_mb:.1f} MiB")
    return model_dir


def main() -> int:
    server_exe = setup_llama_cpp()
    model_dir = setup_gguf_models()
    print(f"[READY] llama-server: {server_exe}")
    print(f"[READY] GGUF models : {model_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
