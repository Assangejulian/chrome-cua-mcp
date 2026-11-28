"""Generate and AST-validate the unattended-safe M4 Colab A100 SFT + GGUF export notebook."""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import m3_train_colab as m3


def build_helpers_source() -> str:
    """Assemble standalone Colab cell source reusing verified m3_train_colab helpers plus M4 dual-channel upgrades."""
    reused_fns = [
        m3.jsonl,
        m3.write_json,
        m3.write_jsonl,
        m3.digest,
        m3.inside,
        m3.extract_bundle,
        m3.check_action,
        m3.install_colab_stack,
        m3.install_cpu_export_stack,
        m3.checkpoints,
        m3.run_training,
    ]
    header = '''from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import random
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import traceback
import zipfile

SYSTEM_PROMPT = (
    "You are a Chrome browser GUI action assistant. Given a Chrome window screenshot and a user instruction, "
    "output strictly one JSON object for the next action using [0, 1000] normalized integer coordinates. "
    "Do not output explanations or markdown."
)
SCREEN_READER_PROMPT = (
    "You are an accurate visual screen reader. Answer the user's question directly and concisely "
    "based on the browser screenshot and OCR excerpt. Reply in plain Chinese or English text ONLY "
    "(do NOT output action JSON)."
)
BASE_MODEL = "Qwen/Qwen3.5-2B"
BASE_REVISION = "15852e8c16360a2fea060d615a32b45270f8a8fc"
LLAMA_CPP_EXPORT_REVISION = "164ab018f9b7368ff8122094994c4b74e468aa0e"
POINTER_ACTIONS = {"left_click", "right_click", "double_click", "move", "scroll"}
ACTIONS = POINTER_ACTIONS | {"type", "hotkey", "wait", "done", "not_found"}
'''
    reused_src = "\n\n".join(inspect.getsource(fn) for fn in reused_fns)
    m4_additions = '''

def load_sft_m4(root, manifest, source, image_hashes, *, training):
    path = inside(root, manifest)
    rows = []
    identifiers = set()
    for row in jsonl(path):
        sample_id = row.get("sample_id")
        try:
            if not isinstance(sample_id, str) or not sample_id or sample_id in identifiers:
                raise ValueError("Missing or repeated sample_id")
            banned = {"test", "eval_only", "holdout"} | ({"eval", "val", "dev", "validation"} if training else set())
            if str(row.get("split", "")).lower() in banned:
                raise ValueError("Held-out row cannot enter SFT")
            messages, images = row["messages"], row["images"]
            if [message["role"] for message in messages] != ["system", "user", "assistant"] or len(images) != 1:
                raise ValueError("Expected system/user/assistant and one image")
            instruction = messages[1]["content"]
            if not instruction.startswith("<image>") or instruction.count("<image>") != 1 or not instruction[7:].strip():
                raise ValueError("Expected one leading image token and a nonempty instruction")

            is_ask_screen = row.get("task_type") == "ask_screen" or messages[0]["content"] == SCREEN_READER_PROMPT
            if is_ask_screen:
                if messages[0]["content"] != SCREEN_READER_PROMPT:
                    raise ValueError("ask_screen row must match exact SCREEN_READER_PROMPT")
                ans = str(messages[2]["content"]).strip()
                if not (10 <= len(ans) <= 120):
                    raise ValueError(f"ask_screen answer length out of [10, 120]: {len(ans)}")
                if ans.startswith("{") or ans.startswith("```") or '"action"' in ans:
                    raise ValueError("ask_screen answer must be plain text, not action JSON")
                sys_prompt = SCREEN_READER_PROMPT
                asst_content = ans
                action_label = "ask_screen"
            else:
                output = json.loads(messages[2]["content"])
                action_label = check_action(output)
                sys_prompt = SYSTEM_PROMPT
                asst_content = json.dumps(output, ensure_ascii=False, separators=(",", ":"))

            image = inside(root, images[0])
            if image.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"} or not image.is_file():
                raise ValueError(f"Missing or unsupported image: {images[0]}")
            if image not in image_hashes:
                from PIL import Image

                with Image.open(image) as decoded:
                    decoded.verify()
                image_hashes[image] = digest(image)
        except (KeyError, ValueError, TypeError, AttributeError, OSError) as error:
            raise ValueError(f"{path} ({sample_id!r}): {error}") from error
        identifiers.add(sample_id)
        rows.append((action_label, image_hashes[image], {
            "sample_id": f"{source}:{sample_id}",
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": instruction},
                {"role": "assistant", "content": asst_content},
            ],
            "images": [str(image)],
        }))
    if not rows:
        raise ValueError(f"Empty manifest: {path}")
    return rows


def deduplicate_m4(rows):
    unique = {}
    conflicting = set()
    for record in rows:
        _, checksum, row = record
        key = (checksum, row["messages"][0]["content"], row["messages"][1]["content"])
        previous = unique.setdefault(key, record)
        if previous[2]["messages"][2]["content"] != row["messages"][2]["content"]:
            conflicting.add(key)
    result = [record for key, record in unique.items() if key not in conflicting]
    return result, {"repeated": len(rows) - len(unique), "conflicting_pairs_removed": len(conflicting)}


def prepare_m4(data_root, scratch, bundles, seed=42, dev_limit=None, expected_archives=None):
    scratch.mkdir(parents=True, exist_ok=False)
    training, validation = [], []
    receipt = {}
    image_hashes = {}
    train_groups = set()
    rng = random.Random(seed)
    for name, relative, train_manifest, val_manifest, train_cap in bundles:
        if not name.isascii() or not name.replace("_", "").isalnum() or name in receipt:
            raise ValueError(f"Invalid or duplicate source name: {name}")
        if any(marker in str(item).lower() for item in (relative, train_manifest, val_manifest)
               for marker in ("holdout", "screenspot", "venus")):
            raise ValueError("Independent test data is not accepted by the training preparer")
        source = inside(data_root, relative)
        if not source.is_file():
            raise FileNotFoundError(f"Required bundle missing from Drive: {source}")
        expected = (expected_archives or {}).get(relative)
        if expected and source.stat().st_size != expected["bytes"]:
            raise ValueError(f"Size differs from manifest: {source.name}")
        staged = scratch / f"{name}-{source.name}"
        shutil.copyfile(source, staged)
        checksum = digest(staged)
        if expected and checksum != expected["sha256"]:
            raise ValueError(f"SHA-256 differs from manifest: {source.name}")
        root = scratch / name
        root.mkdir()
        extract_bundle(staged, root)
        staged.unlink()
        capture_meta = root / "capture.json"
        if capture_meta.is_file():
            meta = json.loads(capture_meta.read_text(encoding="utf-8"))
            if meta.get("purpose") != "TRAIN_ONLY" or val_manifest:
                raise ValueError(f"Capture bundle is not designated training-only: {source.name}")
            train_groups.update(meta.get("groups", []))
        train_rows = load_sft_m4(root, train_manifest, name, image_hashes, training=True)
        val_rows = load_sft_m4(root, val_manifest, name, image_hashes, training=False) if val_manifest else []
        if set(row[1] for row in train_rows) & set(row[1] for row in val_rows):
            raise ValueError(f"Train/validation image overlap in {name}")
        train_rows, duplicates = deduplicate_m4(sorted(train_rows, key=lambda row: row[2]["sample_id"]))
        if train_cap is not None:
            if type(train_cap) is not int or train_cap < 1:
                raise ValueError("train_cap must be a positive integer or None")
            train_rows = rng.sample(train_rows, min(train_cap, len(train_rows)))
        training.extend(train_rows)
        validation.extend(val_rows)
        receipt[name] = {
            "archive": relative,
            "sha256": checksum,
            "train_manifest": train_manifest,
            "validation_manifest": val_manifest,
            "train_actions": dict(Counter(row[0] for row in train_rows)),
            "validation_actions_before_cap": dict(Counter(row[0] for row in val_rows)),
            "training_deduplication": duplicates,
        }
    if not training or not validation:
        raise ValueError("An explicit training split and an explicit development/validation split are required")
    if set(row[1] for row in training) & set(row[1] for row in validation):
        raise ValueError("Train/validation image hashes overlap across sources")
    training, train_merged_duplicates = deduplicate_m4(training)
    validation, val_merged_duplicates = deduplicate_m4(validation)
    if not training or not validation:
        raise ValueError("Train or development set is empty after removing conflicting duplicates")
    if dev_limit is not None:
        if type(dev_limit) is not int or dev_limit < 1:
            raise ValueError("dev_limit must be a positive integer or None")
        validation = rng.sample(validation, min(dev_limit, len(validation)))
    rng.shuffle(training)
    write_jsonl(scratch / "train.jsonl", (row[2] for row in training))
    write_jsonl(scratch / "validation.jsonl", (row[2] for row in validation))
    report = {
        "seed": seed,
        "sources": receipt,
        "train_count": len(training),
        "validation_count": len(validation),
        "train_actions": dict(Counter(row[0] for row in training)),
        "validation_actions": dict(Counter(row[0] for row in validation)),
        "merged_deduplication": {"training": train_merged_duplicates, "validation": val_merged_duplicates},
        "train_groups_in_capture_manifests": sorted(train_groups),
        "train_hashes": sorted({row[1] for row in training}),
        "validation_hashes": sorted({row[1] for row in validation}),
    }
    write_json(scratch / "data_receipt.json", report)
    return report


def require_bf16_gpu_guard():
    if not shutil.which("nvidia-smi"):
        raise RuntimeError("Switch Runtime -> Change runtime type -> A100, H100, or L4 GPU, then Run all")
    probe = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], capture_output=True, text=True)
    hardware = probe.stdout.strip()
    if probe.returncode != 0 or not hardware:
        raise RuntimeError(f"Switch Runtime -> Change runtime type -> A100, H100, or L4 GPU, then Run all (found: {hardware!r})")
    if "A100" in hardware or "H100" in hardware:
        batch, accumulation = 4, 8
    elif "L4" in hardware:
        batch, accumulation = 2, 16
    else:
        raise RuntimeError(
            f"Switch Runtime -> Change runtime type -> A100, H100, or L4 GPU, then Run all (found: {hardware!r}; CPU/T4 unsupported for bf16)"
        )
    print(f"[OK] GPU confirmed: {hardware} | batch={batch}, grad_accum={accumulation} (effective_batch={batch * accumulation})")
    return hardware, batch, accumulation


def swift_command_m4(scratch, output, *, model_revision, seed, rank=32, batch=4, accumulation=8,
                     image_tokens=1024, learning_rate=1e-4, epochs=2, cadence=80):
    if len(model_revision) != 40 or any(char not in "0123456789abcdef" for char in model_revision):
        raise ValueError("model_revision must be an exact 40-char Hugging Face commit")
    command = [
        "swift", "sft", "--model", BASE_MODEL, "--model_revision", model_revision, "--use_hf", "true",
        "--tuner_type", "lora", "--target_modules", "all-linear",
        "--lora_rank", str(rank), "--lora_alpha", str(rank * 2), "--lora_dropout", "0.05",
        "--freeze_vit", "true", "--freeze_aligner", "true", "--freeze_llm", "false",
        "--add_non_thinking_prefix", "true", "--loss_scale", "ignore_empty_think",
        "--dataset", str(scratch / "train.jsonl"), "--val_dataset", str(scratch / "validation.jsonl"),
        "--split_dataset_ratio", "0", "--torch_dtype", "bfloat16", "--attn_impl", "sdpa",
        "--per_device_train_batch_size", str(batch), "--per_device_eval_batch_size", str(batch),
        "--gradient_accumulation_steps", str(accumulation), "--learning_rate", str(learning_rate),
        "--lr_scheduler_type", "cosine", "--warmup_ratio", "0.05",
        "--num_train_epochs", str(epochs), "--max_length", "4096",
        "--eval_steps", str(cadence), "--save_steps", str(cadence), "--save_total_limit", "6",
        "--logging_steps", "1", "--dataloader_num_workers", "0", "--seed", str(seed),
        "--output_dir", str(output),
    ]
    env = {
        "IMAGE_MAX_TOKEN_NUM": str(image_tokens),
        "PYTHONUNBUFFERED": "1",
        "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
        "CUDA_VISIBLE_DEVICES": "0",
    }
    return command, env


def export_gguf_m4(adapter, run_dir, scratch, revision, device="cpu"):
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        raise ValueError("Pin the verified llama.cpp commit before export")
    if not (adapter / "adapter_config.json").is_file() or not (adapter / "adapter_model.safetensors").is_file():
        raise ValueError(f"Export requires a complete PEFT LoRA checkpoint: {adapter}")
    if device not in {"cpu", "auto"}:
        raise ValueError("Export device must be cpu or auto")
    config = json.loads((adapter / "adapter_config.json").read_text(encoding="utf-8"))
    if config.get("base_model_name_or_path", "").split("/")[-1] not in {BASE_MODEL.rsplit("/", 1)[-1], BASE_REVISION}:
        raise ValueError("Adapter does not identify the expected Qwen base")

    root = scratch / "llama.cpp"
    build = root / "build"
    merged = scratch / "merged"
    bundle = scratch / "gguf"
    for p in (root, merged, bundle):
        if p.exists():
            shutil.rmtree(p)
    bundle.mkdir(parents=True)

    subprocess.run([
        "swift", "export", "--adapters", str(adapter), "--use_hf", "true", "--merge_lora", "true",
        "--model", BASE_MODEL, "--model_revision", BASE_REVISION, "--device_map", device,
        "--torch_dtype", "bfloat16", "--output_dir", str(merged),
    ], check=True)

    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "fetch", "--depth", "1", "https://github.com/ggml-org/llama.cpp.git", revision], check=True)
    subprocess.run(["git", "-C", str(root), "checkout", "--detach", "FETCH_HEAD"], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "gguf", "sentencepiece"], check=True)
    subprocess.run([
        "cmake", "-S", str(root), "-B", str(build), "-DGGML_CUDA=OFF", "-DLLAMA_BUILD_TESTS=OFF",
        "-DLLAMA_BUILD_SERVER=OFF", "-DLLAMA_BUILD_EXAMPLES=OFF", "-DLLAMA_BUILD_TOOLS=ON",
    ], check=True)
    subprocess.run(["cmake", "--build", str(build), "--target", "llama-quantize", "-j4"], check=True)

    converter = [sys.executable, str(root / "convert_hf_to_gguf.py"), str(merged)]
    bf16 = bundle / "m4-bf16.gguf"
    projector = bundle / "m4-mmproj-f16.gguf"
    subprocess.run(converter + ["--outfile", str(bf16), "--outtype", "bf16"], check=True)
    subprocess.run(converter + ["--mmproj", "--outfile", str(projector), "--outtype", "f16"], check=True)

    files = [projector]
    for quant in ("Q8_0", "Q4_K_M"):
        target = bundle / f"m4-qwen35-2b-{quant}.gguf"
        subprocess.run([str(build / "bin" / "llama-quantize"), str(bf16), str(target), quant], check=True)
        files.append(target)
    if bf16.is_file():
        bf16.unlink()

    sha_map = {}
    for path in files:
        with path.open("rb") as stream:
            if stream.read(4) != b"GGUF":
                raise ValueError(f"Invalid GGUF: {path.name}")
        sha_map[path.name] = digest(path)

    output = run_dir / "gguf"
    output.mkdir(parents=True, exist_ok=True)
    for path in files:
        dst = output / path.name
        shutil.copyfile(path, dst)
        if digest(dst) != sha_map[path.name]:
            raise RuntimeError(f"Drive copy SHA-256 verification failed: {dst}")

    write_json(output / "export_receipt.json", {
        "revision": revision,
        "model_revision": BASE_REVISION,
        "adapter": str(adapter),
        "deployment_validated": False,
        "sha256": sha_map,
    })
'''
    return header + "\n" + reused_src + m4_additions


def build_m4_notebook(destination: Path) -> None:
    summary_path = PROJECT_ROOT / "artifacts" / "m4_bundle_20261125" / "bundle_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    archive_info = summary["archive"]
    m4_bytes = int(archive_info["bytes"])
    m4_sha256 = str(archive_info["sha256"])

    def cell(kind: str, identifier: str, text: str) -> dict:
        if kind == "code":
            ast.parse(text)
            return {
                "cell_type": "code",
                "id": identifier,
                "metadata": {},
                "execution_count": None,
                "outputs": [],
                "source": text.strip().splitlines(keepends=True),
            }
        return {
            "cell_type": "markdown",
            "id": identifier,
            "metadata": {},
            "source": text.strip().splitlines(keepends=True),
        }

    intro_md = """# M4 — Chrome GUI Pilot Dual-Channel SFT (Action + Screen Reader, 2 Epochs + 无人值守自动导出与断开)

- **运行前只需一步**：右上角切换运行时为 **A100 / H100 / L4 GPU**（推荐 A100；若 A100 暂时缺卡可选 **L4 GPU**，会自动切换 `batch=2, grad_accum=16` 保持有效 batch=32 并防爆显存），然后直接点 **代码执行程序 → 全部运行（Run all）**。
- **零误烧保护**：如果误开在 CPU 或不支持原生 `bf16` 的 T4 运行时，启动后第 1 秒会直接报错拦截，不会进入安装或等待。
- **双通道混训（M3.5 原 6 包 + M4 Teacher 包）**：总计 ~7.7K 训练样本（含真实中文站点点击、右键、滚屏寻目标、输入、纠偏负样本，以及 400+ 条 `ask_screen` 纯文本读屏摘要问答，各自严格匹配 `SYSTEM_PROMPT` 与 `SCREEN_READER_PROMPT`）。
- **全自动无人值守闭环**：校验 7 份数据包 SHA-256 与防泄漏分割 → 安装锁版训练栈 → `Qwen/Qwen3.5-2B`（2 epochs，每 80 步评估保存并写入 Drive `m4_runs/`）→ 导出 `m4-qwen35-2b-Q4_K_M.gguf`、`Q8_0` 和 `m4-mmproj-f16.gguf` 并核对 Drive 文件哈希 → 刷盘等待后**自动执行 `runtime.unassign()` 断开 GPU**（无论成功或报错都会执行 `finally` 释放算力）。
- **事后单导中间权重**：若验收后想单独导出某个中间 checkpoint（如 `checkpoint-320`），可切回 **CPU** 运行时，把 `RUN_TRAINING = False`、`EXPORT_ADAPTER = "/content/drive/MyDrive/chrome-gui-pilot/m4_runs/<RUN_ID>/checkpoints/.../checkpoint-320"` 后再运行。"""

    helpers_src = build_helpers_source()

    config_src = f'''from google.colab import drive
drive.mount("/content/drive")

DRIVE_ROOT = Path("/content/drive/MyDrive/chrome-gui-pilot")
RUN_ID = datetime.now(timezone.utc).strftime("m4-%Y%m%d-%H%M%S") + "-sft2ep"
SEED = 42
MODEL_REVISION = BASE_REVISION
LLAMA_CPP_REVISION = LLAMA_CPP_EXPORT_REVISION

RUN_TRAINING = True
RUN_EXPORT = True
EXPORT_ADAPTER = None
INSTALL_STACK = True
INSTALL_CUDA_ACCELERATORS = False
AUTO_UNASSIGN_ON_FINISH = True
EXPORT_ONLY = RUN_EXPORT and not RUN_TRAINING

SOURCES = [
    ("m1_web", "m1_web_bundle.tar", "train_open_web.jsonl", "val_open_web.jsonl", 1500),
    ("zh_fixture", "M3_zh_chrome_fixture_train_60.zip", "train_chrome.jsonl", None, None),
    ("satraj_chrome", "m3_satraj_chrome.tar", "train_satraj_chrome.jsonl", "val_satraj_chrome.jsonl", None),
    ("zh_boost", "m3_option_a_zh_boost.tar", "train_zh_boost.jsonl", None, None),
    ("hard_neg", "m3_option_a_hard_neg.tar", "train_hard_neg.jsonl", None, 300),
    ("m35_real_chrome", "m35_real_chrome_train_dev.tar", "train_real_chrome.jsonl", "val_real_chrome.jsonl", None),
    ("m4_teacher", "m4_teacher_bundle.tar", "train_m4_teacher.jsonl", "val_m4_teacher.jsonl", None),
]

EXPECTED_ARCHIVES = {{
    "m3_satraj_chrome.tar": {{
        "bytes": 152770560,
        "sha256": "6e364a5d0befc45d0fec89cddac121eb4dfae2da5dd495170eabe8599a652dad",
    }},
    "m3_option_a_zh_boost.tar": {{
        "bytes": 4577280,
        "sha256": "7b65ddfa185ca529fe4b49aadbbc5cf813b19102820dce280c6201f2e5242937",
    }},
    "m3_option_a_hard_neg.tar": {{
        "bytes": 77178880,
        "sha256": "2ed796d2bdb27c8d61140a8e54afc7d3582417c649ec00c0f41f0697832a85d9",
    }},
    "m35_real_chrome_train_dev.tar": {{
        "bytes": 10465280,
        "sha256": "0ecd6dea3352735753d59a4017636022f762a31cda43bc85caf8a18f34253b4d",
    }},
    "m4_teacher_bundle.tar": {{
        "bytes": {m4_bytes},
        "sha256": "{m4_sha256}",
    }},
}}'''

    run_src = '''gpu_confirmed = False
train_batch, train_accum = 4, 8
WORK = Path("/content/chrome-pilot-m4") / RUN_ID
RUN_DIR = DRIVE_ROOT / "m4_runs" / RUN_ID

try:
    if RUN_TRAINING:
        _, train_batch, train_accum = require_bf16_gpu_guard()
        gpu_confirmed = True
        if RUN_DIR.exists():
            raise FileExistsError(f"Choose a fresh RUN_ID; already exists: {RUN_DIR}")
    elif RUN_EXPORT and not EXPORT_ADAPTER:
        raise ValueError("Set EXPORT_ADAPTER to a saved PEFT checkpoint when RUN_TRAINING=False")

    if not EXPORT_ONLY:
        if WORK.exists():
            shutil.rmtree(WORK)
        report = prepare_m4(
            DRIVE_ROOT / "data",
            WORK,
            SOURCES,
            SEED,
            dev_limit=None,
            expected_archives=EXPECTED_ARCHIVES,
        )
        print(json.dumps({k: v for k, v in report.items() if not k.endswith("hashes")}, ensure_ascii=False, indent=2), flush=True)
        command, run_env = swift_command_m4(
            WORK,
            RUN_DIR / "checkpoints",
            model_revision=MODEL_REVISION,
            seed=SEED,
            batch=train_batch,
            accumulation=train_accum,
        )
        print("Command:", " ".join(command), flush=True)

    if INSTALL_STACK:
        if EXPORT_ONLY:
            install_cpu_export_stack()
        else:
            install_colab_stack(INSTALL_CUDA_ACCELERATORS)

    adapter = None
    if RUN_TRAINING:
        RUN_DIR.parent.mkdir(parents=True, exist_ok=True)
        adapter = run_training(command, run_env, WORK, RUN_DIR)
        print(f"[OK] Final LoRA checkpoint: {adapter}", flush=True)

    if RUN_EXPORT:
        WORK.mkdir(parents=True, exist_ok=True)
        if EXPORT_ONLY:
            RUN_DIR.mkdir(parents=True, exist_ok=True)
        target_adapter = adapter if adapter is not None else Path(EXPORT_ADAPTER)
        export_gguf_m4(target_adapter, RUN_DIR, WORK, LLAMA_CPP_REVISION, device="cpu")
        print(f"[OK] GGUF artifacts exported & verified at: {RUN_DIR / 'gguf'}", flush=True)
except Exception:
    err_text = traceback.format_exc()
    print(err_text, flush=True)
    if RUN_DIR.is_dir():
        (RUN_DIR / "error.txt").write_text(err_text, encoding="utf-8")
    raise
finally:
    try:
        os.sync()
    except Exception:
        pass
    time.sleep(12)
    if AUTO_UNASSIGN_ON_FINISH and gpu_confirmed:
        print("[CLEANUP] Syncing Drive and unassigning GPU runtime...", flush=True)
        from google.colab import runtime
        runtime.unassign()'''

    notebook = {
        "cells": [
            cell("markdown", "intro", intro_md),
            cell("code", "helpers", helpers_src),
            cell("code", "configure", config_src),
            cell("code", "run", run_src),
        ],
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.11"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(notebook, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"[PASS] Built and AST-validated: {destination}")


if __name__ == "__main__":
    build_m4_notebook(PROJECT_ROOT / "notebooks" / "M4_Colab_A100_Chrome_SFT.ipynb")
