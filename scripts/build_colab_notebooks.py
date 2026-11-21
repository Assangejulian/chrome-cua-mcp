"""Generate validated M1 (Free CPU data prep) and M2 (A100 smoke test & export gate) Colab notebooks."""

from __future__ import annotations

import ast
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NB_DIR = PROJECT_ROOT / "notebooks"


def md_cell(source: str) -> dict:
    return {
        "cell_type": "markdown",
        "metadata": {},
        "source": [line + "\n" for line in source.strip().splitlines()],
    }


def code_cell(source: str) -> dict:
    lines = source.strip().splitlines()
    py_lines = [ln for ln in lines if not ln.lstrip().startswith(("!", "%"))]
    ast.parse("\n".join(py_lines))
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": [line + "\n" for line in lines],
    }


def build_m1_notebook() -> dict:
    c1 = md_cell(
        """
# M1: Cloud Free-CPU Web Grounding Data Prep (0 CCU)
- **IMPORTANT**: Make sure Colab Runtime is **CPU (None accelerator)** so **0 CCU** are spent!
- Streams `agentsea/wave-ui` (Web + Desktop subsets) and `lmms-lab/ScreenSpot-v2`, filters out full-page long screenshots, validates `[0, 1000]` boxes, injects ~5% `not_found` negatives, splits leak-free by `image_hash`, and packs a fast `.tar` bundle into Google Drive.
"""
    )

    c2 = code_cell(
        """
import os, shutil, subprocess
from google.colab import drive

if shutil.which("nvidia-smi") is not None:
    gpu_check = subprocess.run(["nvidia-smi"], capture_output=True, text=True)
    if gpu_check.returncode == 0:
        raise RuntimeError("GPU runtime detected! Switch Runtime -> Change runtime type -> CPU so you do not burn CCUs.")
print("[OK] CPU-only runtime verified (0 CCU burn).")

drive.mount("/content/drive")
DRIVE_ROOT = "/content/drive/MyDrive/chrome-gui-pilot"
DATA_DRIVE_DIR = f"{DRIVE_ROOT}/data"
os.makedirs(DATA_DRIVE_DIR, exist_ok=True)
print("Drive target:", DATA_DRIVE_DIR)
"""
    )

    c3 = code_cell(
        """
!pip install -q -U "datasets>=3.0" pillow pyarrow tqdm
"""
    )

    c4 = code_cell(
        """
import hashlib
import io
import json
import random
from collections import Counter
from pathlib import Path

from datasets import load_dataset
from PIL import Image

MAX_IMAGE_TOKENS = 1024
GRID = 32
MAX_PIXELS = MAX_IMAGE_TOKENS * GRID * GRID  # 1,048,576 pixels (~1280x800)
TARGET_WEB = 16000
TARGET_DESKTOP = 4000
NEG_RATIO = 0.05
SEED = 42

WORK_DIR = Path("/content/m1_build")
IMG_DIR = WORK_DIR / "images"
IMG_DIR.mkdir(parents=True, exist_ok=True)

SYSTEM_PROMPT = (
    "You are a Chrome browser GUI action assistant. Given a Chrome window screenshot and a user instruction, "
    "output strictly one JSON object for the next action using [0, 1000] normalized integer coordinates. "
    "Do not output explanations or markdown."
)

PROMPT_TEMPLATES = [
    "{inst}",
    "Click on: {inst}",
    "Locate and click: {inst}",
    "请点击界面中的：{inst}",
    "帮我找到并点击：{inst}",
]


def resize_for_qwen35(img: Image.Image) -> Image.Image:
    w, h = img.size
    if w * h > MAX_PIXELS:
        scale = (MAX_PIXELS / (w * h)) ** 0.5
        w = max(GRID, int(w * scale))
        h = max(GRID, int(h * scale))
    new_w = max(GRID, round(w / GRID) * GRID)
    new_h = max(GRID, round(h / GRID) * GRID)
    while new_w * new_h > MAX_PIXELS and new_w > GRID and new_h > GRID:
        if new_w >= new_h:
            new_w -= GRID
        else:
            new_h -= GRID
    if (new_w, new_h) != img.size:
        return img.resize((new_w, new_h), Image.Resampling.BICUBIC)
    return img


def is_valid_viewport_image(img: Image.Image) -> bool:
    w, h = img.size
    if w < 320 or h < 240:
        return False
    # Reject extreme full-page scrolling screenshots
    if h / w > 1.85 or w / h > 3.2 or max(w, h) > 4096:
        return False
    return True


def to_norm_box_xyxy(bbox, img_w: int, img_h: int):
    if not bbox or len(bbox) != 4:
        return None
    x1, y1, x2, y2 = [float(v) for v in bbox]
    if max(abs(x1), abs(y1), abs(x2), abs(y2)) <= 1.05:
        x1, x2 = x1 * img_w, x2 * img_w
        y1, y2 = y1 * img_h, y2 * img_h
    if x2 <= x1 or y2 <= y1 or x2 > img_w * 1.05 or y2 > img_h * 1.05:
        return None
    nx1 = min(max(round(x1 / max(1, img_w - 1) * 1000.0), 0), 1000)
    ny1 = min(max(round(y1 / max(1, img_h - 1) * 1000.0), 0), 1000)
    nx2 = min(max(round(x2 / max(1, img_w - 1) * 1000.0), 0), 1000)
    ny2 = min(max(round(y2 / max(1, img_h - 1) * 1000.0), 0), 1000)
    if nx2 - nx1 < 2 or ny2 - ny1 < 2:
        return None
    return [nx1, ny1, nx2, ny2]


def make_sft_record(sample_id: str, rel_img_path: str, instruction_text: str, assistant_json: dict) -> dict:
    return {
        "sample_id": sample_id,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"<image>{instruction_text}"},
            {"role": "assistant", "content": json.dumps(assistant_json, ensure_ascii=False)},
        ],
        "images": [rel_img_path],
    }


rng = random.Random(SEED)
counts = Counter()
seen_image_hashes = {}
image_instructions = {}
records = []

print("Streaming agentsea/wave-ui ...")
ds_wave = load_dataset("agentsea/wave-ui", split="train", streaming=True)

for idx, row in enumerate(ds_wave):
    if counts["web"] >= TARGET_WEB and counts["desktop"] >= TARGET_DESKTOP:
        break
    platform = (row.get("platform") or "").lower().strip()
    if platform not in ("web", "desktop"):
        continue
    if platform == "web" and counts["web"] >= TARGET_WEB:
        continue
    if platform == "desktop" and counts["desktop"] >= TARGET_DESKTOP:
        continue

    img = row.get("image")
    if img is None:
        counts["skip_no_img"] += 1
        continue
    try:
        if img.mode != "RGB":
            img = img.convert("RGB")
    except Exception:
        counts["skip_bad_img"] += 1
        continue

    if not is_valid_viewport_image(img):
        counts["skip_aspect_or_size"] += 1
        continue

    orig_w, orig_h = img.size
    res = row.get("resolution")
    ref_w, ref_h = (int(res[0]), int(res[1])) if (res and len(res) == 2 and res[0] > 0 and res[1] > 0) else (orig_w, orig_h)
    norm_box = to_norm_box_xyxy(row.get("bbox"), ref_w, ref_h)
    if norm_box is None and (ref_w, ref_h) != (orig_w, orig_h):
        norm_box = to_norm_box_xyxy(row.get("bbox"), orig_w, orig_h)
    if norm_box is None:
        counts["skip_bad_box"] += 1
        continue

    raw_inst = (row.get("instruction") or row.get("name") or row.get("description") or "").strip()
    if len(raw_inst) < 2 or len(raw_inst) > 180:
        counts["skip_bad_inst"] += 1
        continue

    resized = resize_for_qwen35(img)
    buf = io.BytesIO()
    resized.save(buf, format="JPEG", quality=92)
    img_bytes = buf.getvalue()
    img_hash = hashlib.sha1(img_bytes).hexdigest()[:16]

    rel_path = f"images/wave_{img_hash}.jpg"
    abs_path = WORK_DIR / rel_path
    if img_hash not in seen_image_hashes:
        abs_path.write_bytes(img_bytes)
        seen_image_hashes[img_hash] = (rel_path, resized.size[0], resized.size[1])
        image_instructions[img_hash] = set()
    image_instructions[img_hash].add(raw_inst.lower())

    cx = (norm_box[0] + norm_box[2]) // 2
    cy = (norm_box[1] + norm_box[3]) // 2
    prompt_text = rng.choice(PROMPT_TEMPLATES).format(inst=raw_inst)
    sample_id = f"wave_{platform}_{idx:07d}"

    sft_rec = make_sft_record(sample_id, rel_path, prompt_text, {"action": "left_click", "coordinate": [cx, cy]})
    meta_rec = {
        "sample_id": sample_id,
        "image_path": rel_path,
        "instruction": prompt_text,
        "raw_instruction": raw_inst,
        "action": "left_click",
        "coordinate": [cx, cy],
        "bbox": norm_box,
        "source": f"waveui_{row.get('source') or platform}",
        "platform": platform,
        "site_domain": str(row.get("source") or "unknown"),
        "session_id": img_hash,
        "crop_mode": "upstream_dataset",
        "img_w": resized.size[0],
        "img_h": resized.size[1],
    }
    records.append((img_hash, sft_rec, meta_rec))
    counts[platform] += 1
    if (counts["web"] + counts["desktop"]) % 2000 == 0:
        print(f"Collected web={counts['web']}, desktop={counts['desktop']}, unique_imgs={len(seen_image_hashes)}")

print("WaveUI collection summary:", dict(counts))
"""
    )

    c5 = code_cell(
        """
# Add ~5% negative (not_found) samples using mismatched cross-screenshot instructions
all_hashes = list(seen_image_hashes.keys())
all_insts = [(h, list(insts)[0]) for h, insts in image_instructions.items() if insts]
num_neg = int(len(records) * NEG_RATIO)
neg_added = 0
for _ in range(num_neg * 3):
    if neg_added >= num_neg:
        break
    h_img = rng.choice(all_hashes)
    h_other, neg_inst = rng.choice(all_insts)
    if h_other == h_img or neg_inst in image_instructions[h_img]:
        continue
    rel_path, rw, rh = seen_image_hashes[h_img]
    prompt_text = rng.choice(PROMPT_TEMPLATES).format(inst=neg_inst)
    sample_id = f"wave_neg_{neg_added:06d}"
    sft_rec = make_sft_record(
        sample_id,
        rel_path,
        prompt_text,
        {"action": "not_found", "reason": "Target element is not present in the current screenshot"},
    )
    meta_rec = {
        "sample_id": sample_id,
        "image_path": rel_path,
        "instruction": prompt_text,
        "raw_instruction": neg_inst,
        "action": "not_found",
        "coordinate": None,
        "bbox": None,
        "source": "waveui_synthetic_neg",
        "platform": "web",
        "site_domain": "synthetic_neg",
        "session_id": h_img,
        "crop_mode": "upstream_dataset",
        "img_w": rw,
        "img_h": rh,
    }
    records.append((h_img, sft_rec, meta_rec))
    neg_added += 1

print(f"Added {neg_added} synthetic negative samples.")

# Leak-free train/val split strictly grouped by image hash
unique_hashes = sorted(seen_image_hashes.keys())
rng.shuffle(unique_hashes)
val_cut = max(1, int(len(unique_hashes) * 0.05))
val_hash_set = set(unique_hashes[:val_cut])

train_sft, val_sft, meta_all = [], [], []
for h, sft_rec, meta_rec in records:
    meta_all.append(meta_rec)
    if h in val_hash_set:
        val_sft.append(sft_rec)
    else:
        train_sft.append(sft_rec)

rng.shuffle(train_sft)
print(f"Split by image_hash: train={len(train_sft)}, val={len(val_sft)} (zero shared screenshots)")
"""
    )

    c6 = code_cell(
        """
# Prepare ScreenSpot-v2 evaluation split (verified [x, y, w, h] pixel format)
print("Loading lmms-lab/ScreenSpot-v2 ...")
ss_ds = load_dataset("lmms-lab/ScreenSpot-v2", split="train")
ss_sft, ss_meta = [], []
ss_sources = Counter()

for idx, row in enumerate(ss_ds):
    img = row["image"]
    if img.mode != "RGB":
        img = img.convert("RGB")
    orig_w, orig_h = img.size
    bx, by, bw, bh = [float(v) for v in row["bbox"]]
    norm_box = to_norm_box_xyxy([bx, by, bx + bw, by + bh], orig_w, orig_h)
    if norm_box is None:
        continue

    resized = resize_for_qwen35(img)
    rel_path = f"images/screenspot_{idx:05d}.jpg"
    resized.save(WORK_DIR / rel_path, format="JPEG", quality=95)

    cx = (norm_box[0] + norm_box[2]) // 2
    cy = (norm_box[1] + norm_box[3]) // 2
    inst = row["instruction"].strip()
    ds_src = str(row.get("data_source") or "unknown").lower()
    ds_type = str(row.get("data_type") or "unknown").lower()
    ss_sources[ds_src] += 1

    sample_id = f"ssv2_{idx:05d}"
    ss_sft.append(make_sft_record(sample_id, rel_path, inst, {"action": "left_click", "coordinate": [cx, cy]}))
    ss_meta.append({
        "sample_id": sample_id,
        "image_path": rel_path,
        "instruction": inst,
        "action": "left_click",
        "coordinate": [cx, cy],
        "bbox": norm_box,
        "source": f"screenspot_v2_{ds_src}",
        "data_type": ds_type,
        "data_source": ds_src,
        "img_w": resized.size[0],
        "img_h": resized.size[1],
    })

print("ScreenSpot-v2 loaded:", len(ss_sft), dict(ss_sources))
"""
    )

    c7 = code_cell(
        """
import os
import shutil
import tarfile

def write_jsonl(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\\n")

write_jsonl(WORK_DIR / "train_open_web.jsonl", train_sft)
write_jsonl(WORK_DIR / "val_open_web.jsonl", val_sft)
write_jsonl(WORK_DIR / "metadata_open_web.jsonl", meta_all)
write_jsonl(WORK_DIR / "val_screenspot_v2.jsonl", ss_sft)
write_jsonl(WORK_DIR / "metadata_screenspot_v2.jsonl", ss_meta)

stats = {
    "max_image_tokens": MAX_IMAGE_TOKENS,
    "train_count": len(train_sft),
    "val_open_count": len(val_sft),
    "screenspot_v2_count": len(ss_sft),
    "wave_counts": dict(counts),
    "screenspot_sources": dict(ss_sources),
    "neg_samples": neg_added,
}
(WORK_DIR / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

tar_local = Path("/content/m1_web_bundle.tar")
print("Packing tar archive...")
with tarfile.open(tar_local, "w") as tar:
    tar.add(WORK_DIR, arcname=".")

size_mb = tar_local.stat().st_size / (1024**2)
DATA_DRIVE_DIR = "/content/drive/MyDrive/chrome-gui-pilot/data"
os.makedirs(DATA_DRIVE_DIR, exist_ok=True)
print(f"Archive size: {size_mb:.1f} MiB -> copying to Google Drive...")
shutil.copy2(tar_local, Path(DATA_DRIVE_DIR) / "m1_web_bundle.tar")
shutil.copy2(WORK_DIR / "stats.json", Path(DATA_DRIVE_DIR) / "m1_stats.json")
print("[DONE] Data bundle saved to Drive:", f"{DATA_DRIVE_DIR}/m1_web_bundle.tar")
"""
    )

    return {
        "cells": [c1, c2, c3, c4, c5, c6, c7],
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.11"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def build_m2_notebook() -> dict:
    c1 = md_cell(
        """
# M2: A100 Smoke Test & Hard Export Gate (500 Samples)
- Runs candidate `ms-swift` v4.3 Qwen3.5 stack (`tuner_type=lora`, `add_non_thinking_prefix=true`, `loss_scale=ignore_empty_think`).
- Uses prebuilt wheels where available (avoids slow source builds), logs `step/s`, `IMAGE_MAX_TOKEN_NUM`, and `pip freeze`.
- Enforces M2 Go/No-Go Gate: merges LoRA, converts merged HF model + matching `mmproj` to GGUF (`Q8_0` & `Q4_K_M`), and compares Merged HF vs GGUF Q8_0 vs GGUF Q4_K_M on the same validation subset.
"""
    )

    c2 = code_cell(
        """
import os, time
from google.colab import drive

START_TS = time.time()
print("=== GPU Check ===")
!nvidia-smi

drive.mount("/content/drive")
DRIVE_ROOT = "/content/drive/MyDrive/chrome-gui-pilot"
for sub in ["data", "logs", "m2_gate"]:
    os.makedirs(f"{DRIVE_ROOT}/{sub}", exist_ok=True)
"""
    )

    c3 = code_cell(
        """
import json, shutil, subprocess, sys, urllib.request

# 1. Install base ms-swift v4.3 Qwen3.5 candidate stack first so torch/torchao versions are finalized
!pip install -q -U ms-swift "transformers>=5.9,<5.23.0" "qwen_vl_utils>=0.0.14" peft liger-kernel "torchao>=0.16.0"
!pip install -q -U "flash-linear-attention>=0.4.2" --no-build-isolation

# 2. Query finalized torch/CUDA/ABI in a fresh subprocess and match prebuilt Dao-AILab wheels
env_json = subprocess.check_output(
    [
        sys.executable,
        "-c",
        (
            "import json, sys, torch; "
            "print(json.dumps({"
            "'py': f'cp{sys.version_info.major}{sys.version_info.minor}', "
            "'torch': '.'.join(torch.__version__.split('+')[0].split('.')[:2]), "
            "'abi': 'TRUE' if getattr(torch._C, '_GLIBCXX_USE_CXX11_ABI', False) else 'FALSE', "
            "'cu': 'cu' + (torch.version.cuda or '12.4').split('.')[0]"
            "}))"
        ),
    ],
    text=True,
).strip()
env = json.loads(env_json)
py_tag, torch_ver, abi_str, cuda_major = env["py"], env["torch"], env["abi"], env["cu"]
print(f"Runtime env: python={py_tag}, torch={torch_ver}, cuda={cuda_major}, cxx11abi={abi_str}")

try:
    rel = json.loads(urllib.request.urlopen("https://api.github.com/repos/Dao-AILab/flash-attention/releases/latest", timeout=20).read())
    assets = rel.get("assets", [])
    for pkg_prefix in ("causal_conv1d", "flash_attn"):
        candidates = [
            a["browser_download_url"] for a in assets
            if a["name"].startswith(pkg_prefix + "-")
            and f"torch{torch_ver}cxx11abi{abi_str}-{py_tag}-{py_tag}-linux_x86_64.whl" in a["name"]
        ]
        preferred = [u for u in candidates if f"+{cuda_major}" in u] or candidates
        if preferred:
            whl_url = preferred[0]
            print(f"[WHEEL] Installing {pkg_prefix}: {whl_url.rsplit('/', 1)[-1]}")
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", whl_url], check=False)
        else:
            print(f"[INFO] No prebuilt wheel found for {pkg_prefix} ({py_tag}, torch{torch_ver}, abi{abi_str})")
except Exception as exc:
    print("[WARN] Wheel lookup failed:", exc)

if subprocess.run([sys.executable, "-c", "import causal_conv1d"], capture_output=True).returncode != 0:
    print("[FALLBACK] Installing causal-conv1d from source...")
    !MAX_JOBS=8 pip install -q -U git+https://github.com/Dao-AILab/causal-conv1d --no-build-isolation

!pip freeze > /content/m2_pip_freeze.txt
shutil.copy2("/content/m2_pip_freeze.txt", "/content/drive/MyDrive/chrome-gui-pilot/logs/m2_pip_freeze.txt")
!python -c "import swift, transformers, peft, torchao; from peft.import_utils import is_torchao_available; print('swift:', swift.__version__, 'transformers:', transformers.__version__, 'peft:', peft.__version__, 'torchao:', torchao.__version__, 'peft_torchao_ok:', is_torchao_available())"
"""
    )

    c4 = code_cell(
        """
import json
import shutil
from pathlib import Path

WORK_DATA = Path("/content/work_data")
WORK_DATA.mkdir(parents=True, exist_ok=True)

shutil.copy2("/content/drive/MyDrive/chrome-gui-pilot/data/m1_web_bundle.tar", "/content/m1_web_bundle.tar")
!tar -xf /content/m1_web_bundle.tar -C /content/work_data

def rewrite_abs_and_slice(src_jsonl: Path, dst_jsonl: Path, limit: int) -> list[dict]:
    kept = []
    with open(src_jsonl, "r", encoding="utf-8") as fin, open(dst_jsonl, "w", encoding="utf-8") as fout:
        for line in fin:
            if len(kept) >= limit:
                break
            row = json.loads(line)
            row["images"] = [str(WORK_DATA / p) for p in row["images"]]
            fout.write(json.dumps(row, ensure_ascii=False) + "\\n")
            kept.append(row)
    return kept

train_500 = rewrite_abs_and_slice(WORK_DATA / "train_open_web.jsonl", WORK_DATA / "m2_train_500.jsonl", 500)
val_100 = rewrite_abs_and_slice(WORK_DATA / "val_open_web.jsonl", WORK_DATA / "m2_val_100.jsonl", 100)
print(f"Prepared smoke splits: train={len(train_500)}, val={len(val_100)}")
"""
    )

    c5 = code_cell(
        """
import importlib.util
import os
import shutil
import subprocess

os.makedirs("/content/logs", exist_ok=True)
ATTN_IMPL = "flash_attention_2" if importlib.util.find_spec("flash_attn") is not None else "sdpa"
print("Selected attn_impl:", ATTN_IMPL, flush=True)

# M2 Baseline 1: freeze ViT and aligner first, train LLM LoRA only (--use_hf true downloads from Hugging Face, not ModelScope)
env = os.environ.copy()
env["PYTHONUNBUFFERED"] = "1"
env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
env["IMAGE_MAX_TOKEN_NUM"] = "1024"
env["CUDA_VISIBLE_DEVICES"] = "0"

cmd = [
    "swift", "sft",
    "--model", "Qwen/Qwen3.5-2B",
    "--use_hf", "true",
    "--tuner_type", "lora",
    "--target_modules", "all-linear",
    "--lora_rank", "32",
    "--lora_alpha", "64",
    "--lora_dropout", "0.05",
    "--freeze_vit", "true",
    "--freeze_aligner", "true",
    "--freeze_llm", "false",
    "--add_non_thinking_prefix", "true",
    "--loss_scale", "ignore_empty_think",
    "--dataset", "/content/work_data/m2_train_500.jsonl",
    "--val_dataset", "/content/work_data/m2_val_100.jsonl",
    "--torch_dtype", "bfloat16",
    "--per_device_train_batch_size", "4",
    "--gradient_accumulation_steps", "8",
    "--learning_rate", "1e-4",
    "--lr_scheduler_type", "cosine",
    "--warmup_ratio", "0.05",
    "--num_train_epochs", "1",
    "--max_length", "4096",
    "--eval_steps", "15",
    "--save_steps", "15",
    "--logging_steps", "1",
    "--attn_impl", ATTN_IMPL,
    "--use_liger_kernel", "true",
    "--dataloader_num_workers", "0",
    "--output_dir", "/content/outputs/m2_smoke",
]

with open("/content/logs/m2_sft.log", "w", encoding="utf-8") as logf:
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        logf.write(line)
    ret = proc.wait()

shutil.copy2("/content/logs/m2_sft.log", "/content/drive/MyDrive/chrome-gui-pilot/logs/m2_sft.log")
assert ret == 0, f"swift sft failed with exit code {ret}; inspect m2_sft.log"
"""
    )

    c6 = code_cell(
        """
import glob
import subprocess
import sys
from pathlib import Path

ckpts = sorted(glob.glob("/content/outputs/m2_smoke/**/checkpoint-*", recursive=True), key=lambda p: int(p.rsplit("-", 1)[-1]))
assert ckpts, "No checkpoint found under /content/outputs/m2_smoke"
LATEST_CKPT = ckpts[-1]
print("Merging checkpoint:", LATEST_CKPT)

!swift export \
    --adapters "{LATEST_CKPT}" \
    --use_hf true \
    --merge_lora true \
    --torch_dtype bfloat16 \
    --output_dir /content/m2_merged

# Build llama.cpp targeting SM80 (A100) only for fast CUDA build; install ONLY gguf & sentencepiece (never requirements-convert_hf_to_gguf.txt which downgrades torch/transformers!)
!rm -rf /content/llama.cpp
!git clone --depth 1 https://github.com/ggml-org/llama.cpp /content/llama.cpp
!cmake -S /content/llama.cpp -B /content/llama.cpp/build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=80 -DLLAMA_BUILD_TESTS=OFF
!cmake --build /content/llama.cpp/build --target llama-quantize llama-server -j8
!pip install -q -U gguf sentencepiece

gguf_dir = Path("/content/m2_gguf")
gguf_dir.mkdir(parents=True, exist_ok=True)
bf16_gguf = gguf_dir / "m2-qwen35-2b-bf16.gguf"
mmproj_gguf = gguf_dir / "m2-mmproj-f16.gguf"
q8_gguf = gguf_dir / "m2-qwen35-2b-Q8_0.gguf"
q4_gguf = gguf_dir / "m2-qwen35-2b-Q4_K_M.gguf"

subprocess.run([sys.executable, "/content/llama.cpp/convert_hf_to_gguf.py", "/content/m2_merged", "--outfile", str(bf16_gguf), "--outtype", "bf16"], check=True)
subprocess.run([sys.executable, "/content/llama.cpp/convert_hf_to_gguf.py", "/content/m2_merged", "--mmproj", "--outfile", str(mmproj_gguf), "--outtype", "f16"], check=True)
subprocess.run(["/content/llama.cpp/build/bin/llama-quantize", str(bf16_gguf), str(q8_gguf), "Q8_0"], check=True)
subprocess.run(["/content/llama.cpp/build/bin/llama-quantize", str(bf16_gguf), str(q4_gguf), "Q4_K_M"], check=True)
assert mmproj_gguf.exists(), f"Missing mmproj: {mmproj_gguf}"
!ls -lh /content/m2_gguf/
"""
    )

    c7 = code_cell(
        """
import base64, json, re, shutil, subprocess, time
from pathlib import Path
import requests
import torch
from PIL import Image
from transformers import AutoModelForImageTextToText, AutoProcessor

WORK_DATA = Path("/content/work_data")
meta_map = {}
with open(WORK_DATA / "metadata_open_web.jsonl", "r", encoding="utf-8") as f:
    for line in f:
        m = json.loads(line)
        meta_map[m["sample_id"]] = m

val_rows = [json.loads(line) for line in open(WORK_DATA / "m2_val_100.jsonl", "r", encoding="utf-8")]

def parse_action(raw: str):
    txt = raw.strip()
    m = re.search(r"\\{.*\\}", txt, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) and "action" in obj else None
    except Exception:
        return None

def score_predictions(preds: list[tuple[dict, str, float]]) -> dict:
    valid_json = 0
    action_ok = 0
    loc_total = 0
    loc_hits = 0
    latencies = []
    for row, raw, dt in preds:
        latencies.append(dt)
        meta = meta_map[row["sample_id"]]
        pred = parse_action(raw)
        if pred is None:
            continue
        valid_json += 1
        if pred.get("action") == meta["action"]:
            action_ok += 1
        if meta["bbox"] is not None:
            loc_total += 1
            coord = pred.get("coordinate")
            if isinstance(coord, list) and len(coord) == 2:
                x, y = coord
                b = meta["bbox"]
                if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                    if b[0] <= x <= b[2] and b[1] <= y <= b[3]:
                        loc_hits += 1
    lat_sorted = sorted(latencies)
    return {
        "n": len(preds),
        "json_valid_rate": round(valid_json / max(1, len(preds)), 4),
        "action_acc": round(action_ok / max(1, len(preds)), 4),
        "point_in_box_rate": round(loc_hits / max(1, loc_total), 4),
        "p50_s": round(lat_sorted[len(lat_sorted) // 2], 3),
        "p95_s": round(lat_sorted[min(len(lat_sorted) - 1, int(len(lat_sorted) * 0.95))], 3),
    }

print("Evaluating Merged HF model...")
processor = AutoProcessor.from_pretrained("/content/m2_merged")
hf_model = AutoModelForImageTextToText.from_pretrained("/content/m2_merged", torch_dtype=torch.bfloat16, device_map="cuda")
hf_model.eval()
hf_preds = []
for row in val_rows:
    img = Image.open(row["images"][0]).convert("RGB")
    msgs = [
        {"role": "system", "content": row["messages"][0]["content"]},
        {"role": "user", "content": [{"type": "image", "image": img}, {"type": "text", "text": row["messages"][1]["content"].replace("<image>", "")}]},
    ]
    text = processor.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    inputs = processor(text=[text], images=[img], return_tensors="pt").to("cuda")
    t0 = time.perf_counter()
    with torch.inference_mode():
        out_ids = hf_model.generate(**inputs, max_new_tokens=64, do_sample=False)
    gen_ids = out_ids[0][inputs["input_ids"].shape[1]:]
    raw = processor.decode(gen_ids, skip_special_tokens=True)
    hf_preds.append((row, raw, time.perf_counter() - t0))

del hf_model
torch.cuda.empty_cache()

mmproj_file = str(sorted(Path("/content/m2_gguf").glob("*mmproj*.gguf"))[0])
drive_gate = Path("/content/drive/MyDrive/chrome-gui-pilot/m2_gate")
drive_logs = Path("/content/drive/MyDrive/chrome-gui-pilot/logs")
Path("/content/logs").mkdir(parents=True, exist_ok=True)
shutil.copy2("/content/m2_gguf/m2-qwen35-2b-Q8_0.gguf", drive_gate / "m2-qwen35-2b-Q8_0.gguf")
shutil.copy2("/content/m2_gguf/m2-qwen35-2b-Q4_K_M.gguf", drive_gate / "m2-qwen35-2b-Q4_K_M.gguf")
shutil.copy2(mmproj_file, drive_gate / Path(mmproj_file).name)
print("[OK] GGUF artifacts backed up to Google Drive before GGUF eval.")

def eval_gguf(gguf_path: str, mmproj_path: str, port: int) -> dict:
    log_path = Path(f"/content/logs/llama_server_{port}.log")
    with open(log_path, "w", encoding="utf-8") as log_f:
        proc = subprocess.Popen(
            [
                "/content/llama.cpp/build/bin/llama-server",
                "--model", gguf_path,
                "--mmproj", mmproj_path,
                "--ctx-size", "4096",
                "--n-gpu-layers", "99",
                "--image-max-tokens", "1024",
                "--reasoning", "off",
                "--jinja",
                "--host", "127.0.0.1",
                "--port", str(port),
            ],
            stdout=log_f,
            stderr=subprocess.STDOUT,
        )
        sess = requests.Session()
        sess.trust_env = False
        try:
            deadline = time.time() + 120.0
            healthy = False
            last_status = None
            while time.time() < deadline:
                if proc.poll() is not None:
                    tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
                    raise RuntimeError(f"llama-server exited with {proc.returncode}\\n{tail}")
                try:
                    resp = sess.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                    last_status = resp.status_code
                    if resp.status_code == 200:
                        healthy = True
                        break
                except Exception as exc:
                    last_status = repr(exc)
                time.sleep(0.5)
            if not healthy:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
                raise TimeoutError(f"llama-server did not become healthy (last={last_status})\\n{tail}")
            preds = []
            for row in val_rows:
                b64 = base64.b64encode(Path(row["images"][0]).read_bytes()).decode("ascii")
                payload = {
                    "messages": [
                        {"role": "system", "content": row["messages"][0]["content"]},
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                                {"type": "text", "text": row["messages"][1]["content"].replace("<image>", "")},
                            ],
                        },
                    ],
                    "temperature": 0.0,
                    "max_tokens": 64,
                    "chat_template_kwargs": {"enable_thinking": False},
                }
                t0 = time.perf_counter()
                r = sess.post(f"http://127.0.0.1:{port}/v1/chat/completions", json=payload, timeout=30).json()
                preds.append((row, r["choices"][0]["message"]["content"], time.perf_counter() - t0))
            return score_predictions(preds)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            shutil.copy2(log_path, drive_logs / log_path.name)

report = {
    "elapsed_min": round((time.time() - START_TS) / 60.0, 2),
    "merged_hf_bf16": score_predictions(hf_preds),
    "gguf_q8_0": eval_gguf("/content/m2_gguf/m2-qwen35-2b-Q8_0.gguf", mmproj_file, 18081),
    "gguf_q4_k_m": eval_gguf("/content/m2_gguf/m2-qwen35-2b-Q4_K_M.gguf", mmproj_file, 18082),
}
print(json.dumps(report, indent=2))
Path("/content/m2_gate_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

shutil.copy2("/content/m2_gate_report.json", drive_gate / "m2_gate_report.json")
print("[PASS] M2 Gate Report & GGUF artifacts copied to Google Drive!")

# Optional: automatically release Colab A100 runtime on completion to prevent overnight CCU burn
AUTO_UNASSIGN_ON_FINISH = True
if AUTO_UNASSIGN_ON_FINISH:
    from google.colab import runtime
    print("Releasing Colab runtime automatically...")
    time.sleep(5)
    runtime.unassign()
"""
    )

    return {
        "cells": [c1, c2, c3, c4, c5, c6, c7],
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.11"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def main() -> int:
    NB_DIR.mkdir(parents=True, exist_ok=True)
    m1_path = NB_DIR / "M1_Colab_CPU_Data_Prep.ipynb"
    m2_path = NB_DIR / "M2_Colab_A100_Smoke_Test.ipynb"
    m1_path.write_text(json.dumps(build_m1_notebook(), ensure_ascii=False, indent=1), encoding="utf-8")
    m2_path.write_text(json.dumps(build_m2_notebook(), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[PASS] Built and AST-validated: {m1_path}")
    print(f"[PASS] Built and AST-validated: {m2_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
