"""One-step local setup, run by setup.bat: llama.cpp runtime, model weights, and the MCP config for wherever this folder lives.

    .venv/Scripts/python.exe scripts/install.py               asks whether to download the vision model
    .venv/Scripts/python.exe scripts/install.py --with-model  no question, download it
    .venv/Scripts/python.exe scripts/install.py --no-model    DOM-only: nothing large is downloaded

Safe to run again: finished downloads are verified and skipped, interrupted ones resume.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from chrome_cua_mcp import client_config  # noqa: E402
from cua_engine import DEFAULT_CHROME  # noqa: E402
from m0_setup_local import download_with_resume, setup_llama_cpp  # noqa: E402

WEIGHTS_URL = "https://github.com/Assangejulian/chrome-cua-mcp/releases/download/m35-weights-v1"
WEIGHTS_DIR = PROJECT_ROOT / "models" / "m35_gguf"
WEIGHTS = {
    "m35-mmproj-f16.gguf": "77d7cb2a85629bfeee146d43be07cc54394926e9aeb0822cd7a820b360817289",
    "m35-qwen35-2b-Q4_K_M.gguf": "2210e7bd00edbec847d72c179971b38c4fa9455bf57cfc5e26bc511bcf3a7da3",
}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 24), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_weights() -> None:
    for name, expected in WEIGHTS.items():
        dest = WEIGHTS_DIR / name
        if dest.is_file() and sha256_of(dest) == expected:
            print(f"  [已有] {name}")
            continue
        print(f"  [下载] {name}")
        download_with_resume(f"{WEIGHTS_URL}/{name}", dest)
        if sha256_of(dest) != expected:
            dest.unlink()
            raise RuntimeError(f"{name} 校验失败（SHA-256 不符），已删除，请重新运行 setup.bat")
        print(f"  [校验通过] {name}")


def want_model(args: argparse.Namespace) -> bool:
    if args.with_model or args.no_model:
        return bool(args.with_model)
    if all((WEIGHTS_DIR / name).is_file() for name in WEIGHTS) and (PROJECT_ROOT / "tools" / "llama_cpp" / "bin" / "llama-server.exe").is_file():
        return True  # already installed: just verify
    if not sys.stdin.isatty():
        print("  非交互运行且未指定 --with-model，跳过视觉模型。")
        return False
    print("  视觉功能（给没有文字的图标定位、读屏问答）需要 NVIDIA 显卡，")
    print("  要下载约 2.7 GB（llama.cpp 运行库 0.7 GB + 模型 1.9 GB），占用磁盘约 4 GB。")
    print("  不装也能用：托管模式下按 DOM 操作网页不需要模型，之后随时可以重新运行 setup.bat 补装。")
    return input("  现在下载吗？[Y/n] ").strip().lower() not in {"n", "no"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--with-model", action="store_true")
    group.add_argument("--no-model", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(errors="replace")

    print(f"安装位置：{PROJECT_ROOT}")
    if sys.version_info[:2] != (3, 11):
        print(f"  [注意] 当前是 Python {sys.version_info.major}.{sys.version_info.minor}；项目只在 3.11 上测试过。")
    if not DEFAULT_CHROME.is_file():
        print(f"  [注意] 没有在默认位置找到 Chrome：{DEFAULT_CHROME}。托管模式需要它。")

    if want_model(args):
        if shutil.which("nvidia-smi") is None:
            print("  [注意] 没有找到 nvidia-smi。llama.cpp 用的是 CUDA 版本，没有 NVIDIA 显卡时视觉功能无法启动。")
        print("llama.cpp 运行库：")
        setup_llama_cpp()
        print("模型权重（M3.5，Q4_K_M）：")
        fetch_weights()
        vision = "已安装"
    else:
        vision = "未安装（只能用不依赖模型的 DOM 路径；重新运行 setup.bat 可补装）"

    out_dir = PROJECT_ROOT / "configs"
    json_path, toml_path = out_dir / "generated_stdio.json", out_dir / "generated_codex.toml"
    json_path.write_text(client_config("stdio", "json") + "\n", encoding="utf-8")
    toml_path.write_text(client_config("stdio", "codex"), encoding="utf-8")

    print()
    print("=" * 64)
    print(f"安装完成。视觉模型：{vision}")
    print()
    print("1) 启动悬浮窗（同时提供 HTTP MCP：http://127.0.0.1:18390/mcp）")
    print(f"     双击 {PROJECT_ROOT / '启动_Chrome_Pilot_M35.bat'}")
    print()
    print("2) 或者让上层模型直接拉起 Stdio MCP。配置已按当前位置生成：")
    print(f"     Codex（追加到 config.toml）：{toml_path}")
    print(f"     其他客户端（mcpServers JSON）：{json_path}")
    print()
    print(client_config("stdio", "codex"))
    print("移动或重命名这个文件夹之后，重新运行 setup.bat 即可得到新的配置。")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
