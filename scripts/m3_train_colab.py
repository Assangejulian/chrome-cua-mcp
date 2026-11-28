"""Build the standalone M3 Colab notebook and prepare/launch its SFT run."""

from collections import Counter
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
import zipfile


SYSTEM_PROMPT = (
    "You are a Chrome browser GUI action assistant. Given a Chrome window screenshot and a user instruction, "
    "output strictly one JSON object for the next action using [0, 1000] normalized integer coordinates. "
    "Do not output explanations or markdown."
)
BASE_MODEL = "Qwen/Qwen3.5-2B"
BASE_REVISION = "15852e8c16360a2fea060d615a32b45270f8a8fc"
LLAMA_CPP_EXPORT_REVISION = "164ab018f9b7368ff8122094994c4b74e468aa0e"
POINTER_ACTIONS = {"left_click", "right_click", "double_click", "move", "scroll"}
ACTIONS = POINTER_ACTIONS | {"type", "hotkey", "wait", "done", "not_found"}
FIXTURE_TRAIN = "M3_zh_chrome_fixture_train_60.zip"
FIXTURE_HOLDOUT = "M3_zh_chrome_fixture_HOLDOUT_DO_NOT_TRAIN_24.zip"
OPTION_A_BUNDLES = [
    ("satraj_chrome", "m3_satraj_chrome.tar", "train_satraj_chrome.jsonl", "val_satraj_chrome.jsonl", None),
    ("zh_boost", "m3_option_a_zh_boost.tar", "train_zh_boost.jsonl", None, None),
    ("hard_neg", "m3_option_a_hard_neg.tar", "train_hard_neg.jsonl", None, None),
]


def jsonl(path):
    with path.open(encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except ValueError as error:
                    raise ValueError(f"{path}:{line_number}: invalid JSON") from error


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path, rows):
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inside(root, name):
    if not isinstance(name, str) or "\\" in name or ":" in name or "\x00" in name:
        raise ValueError(f"Invalid relative path: {name!r}")
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Unsafe relative path: {name!r}")
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes its bundle: {name!r}")
    return target


def extract_bundle(archive_path, destination):
    limit = min(shutil.disk_usage(destination).free - (2 << 30), 32 << 30)
    seen = set()
    total = 0

    def validate(name, size, directory):
        nonlocal total
        output = inside(destination, name)
        if output in seen or (output == destination and not directory):
            raise ValueError(f"Duplicate or invalid member: {name}")
        seen.add(output)
        total += size
        if total > limit:
            raise ValueError(f"Bundle exceeds available scratch budget: {archive_path.name}")
        return output

    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                mode = stat.S_IFMT(member.external_attr >> 16)
                if mode not in {0, stat.S_IFDIR, stat.S_IFREG}:
                    raise ValueError(f"Unsupported ZIP member: {member.filename}")
                validate(member.filename, member.file_size, member.is_dir())
            archive.extractall(destination)
    else:
        with tarfile.open(archive_path, "r:*") as archive:
            members = archive.getmembers()
            for member in members:
                if not (member.isfile() or member.isdir()):
                    raise ValueError(f"Unsupported TAR member: {member.name}")
                validate(member.name, member.size, member.isdir())
            archive.extractall(destination, members=members, filter="data")


def check_action(payload):
    if not isinstance(payload, dict) or payload.get("action") not in ACTIONS:
        raise ValueError("Unknown or missing action")
    action = payload["action"]
    allowed = {"action"}
    if action in POINTER_ACTIONS or action == "type" and "coordinate" in payload:
        allowed.add("coordinate")
        point = payload.get("coordinate")
        if not isinstance(point, list) or len(point) != 2 or any(type(value) is not int or not 0 <= value <= 1000 for value in point):
            raise ValueError("Expected a normalized integer coordinate in [0, 1000]")
    if action == "type":
        allowed.add("text")
        if not isinstance(payload.get("text"), str) or not payload["text"]:
            raise ValueError("type requires nonempty text")
    elif action == "scroll":
        allowed.update(("direction", "amount"))
        if payload.get("direction") not in {"up", "down", "left", "right"} or type(payload.get("amount")) is not int or not 1 <= payload["amount"] <= 20:
            raise ValueError("Invalid scroll direction or amount")
    elif action == "hotkey":
        allowed.add("keys")
        keys = payload.get("keys")
        if not isinstance(keys, list) or not keys or not all(isinstance(key, str) and key.strip() for key in keys):
            raise ValueError("hotkey requires a nonempty keys list")
    elif action == "wait":
        allowed.add("seconds")
        seconds = payload.get("seconds", 1)
        if type(seconds) not in {float, int} or not 0.1 <= seconds <= 10:
            raise ValueError("wait duration must be in [0.1, 10]")
    elif action == "not_found":
        allowed.add("reason")
        if "reason" in payload and not isinstance(payload["reason"], str):
            raise ValueError("not_found reason must be text")
    if payload.keys() - allowed:
        raise ValueError(f"Unexpected action fields: {sorted(payload.keys() - allowed)}")
    return action


def load_sft(root, manifest, source, image_hashes, *, training):
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
            output = json.loads(messages[2]["content"])
            action = check_action(output)
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
        rows.append((action, image_hashes[image], {
            "sample_id": f"{source}:{sample_id}",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": instruction},
                {"role": "assistant", "content": json.dumps(output, ensure_ascii=False, separators=(",", ":"))},
            ],
            "images": [str(image)],
        }))
    if not rows:
        raise ValueError(f"Empty manifest: {path}")
    return rows


def deduplicate(rows):
    unique = {}
    conflicting = set()
    for record in rows:
        _, checksum, row = record
        key = (checksum, row["messages"][1]["content"])
        previous = unique.setdefault(key, record)
        if previous[2]["messages"][2]["content"] != row["messages"][2]["content"]:
            conflicting.add(key)
    result = [record for key, record in unique.items() if key not in conflicting]
    return result, {"repeated": len(rows) - len(unique), "conflicting_pairs_removed": len(conflicting)}


def prepare(data_root, scratch, bundles, seed=42, dev_limit=None, expected_archives=None):
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
            raise ValueError(f"Size differs from the Option A manifest: {source.name}")
        staged = scratch / f"{name}-{source.name}"
        shutil.copyfile(source, staged)
        checksum = digest(staged)
        if expected and checksum != expected["sha256"]:
            raise ValueError(f"SHA-256 differs from the Option A manifest: {source.name}")
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
        train_rows = load_sft(root, train_manifest, name, image_hashes, training=True)
        val_rows = load_sft(root, val_manifest, name, image_hashes, training=False) if val_manifest else []
        if set(row[1] for row in train_rows) & set(row[1] for row in val_rows):
            raise ValueError(f"Train/validation image overlap in {name}")
        train_rows, duplicates = deduplicate(sorted(train_rows, key=lambda row: row[2]["sample_id"]))
        if train_cap is not None:
            if type(train_cap) is not int or train_cap < 1:
                raise ValueError("train_cap must be a positive integer or None")
            train_rows = rng.sample(train_rows, min(train_cap, len(train_rows)))
        training.extend(train_rows)
        validation.extend(val_rows)
        receipt[name] = {"archive": relative, "sha256": checksum, "train_manifest": train_manifest,
                         "validation_manifest": val_manifest, "train_actions": dict(Counter(row[0] for row in train_rows)),
                         "validation_actions_before_cap": dict(Counter(row[0] for row in val_rows)),
                         "training_deduplication": duplicates}
    if not training or not validation:
        raise ValueError("An explicit training split and an explicit development/validation split are required")
    if set(row[1] for row in training) & set(row[1] for row in validation):
        raise ValueError("Train/validation image hashes overlap across sources")
    training, train_merged_duplicates = deduplicate(training)
    validation, val_merged_duplicates = deduplicate(validation)
    if not training or not validation:
        raise ValueError("Train or development set is empty after removing conflicting duplicates")
    if dev_limit is not None:
        if type(dev_limit) is not int or dev_limit < 1:
            raise ValueError("dev_limit must be a positive integer or None")
        validation = rng.sample(validation, min(dev_limit, len(validation)))
    rng.shuffle(training)
    write_jsonl(scratch / "train.jsonl", (row[2] for row in training))
    write_jsonl(scratch / "validation.jsonl", (row[2] for row in validation))
    report = {"seed": seed, "sources": receipt, "train_count": len(training), "validation_count": len(validation),
              "train_actions": dict(Counter(row[0] for row in training)), "validation_actions": dict(Counter(row[0] for row in validation)),
              "merged_deduplication": {"training": train_merged_duplicates, "validation": val_merged_duplicates},
              "train_groups_in_capture_manifests": sorted(train_groups),
              "train_hashes": sorted({row[1] for row in training}), "validation_hashes": sorted({row[1] for row in validation})}
    write_json(scratch / "data_receipt.json", report)
    return report


def swift_command(scratch, output, *, run_mode, model_revision, seed, rank, batch, accumulation,
                  image_tokens, learning_rate, freeze_aligner, initialize_adapter=None):
    if run_mode not in {"smoke", "pilot", "full"}:
        raise ValueError("run_mode must be smoke, pilot, or full")
    if min(rank, batch, accumulation, image_tokens) < 1 or not 0 < learning_rate <= 0.01:
        raise ValueError("Invalid LoRA, batch, image budget, or learning rate")
    cadence = {"smoke": 4, "pilot": 20, "full": 50}[run_mode]
    command = [
        "swift", "sft", "--model", BASE_MODEL, "--use_hf", "true", "--tuner_type", "lora",
        "--target_modules", "all-linear", "--lora_rank", str(rank), "--lora_alpha", str(rank * 2),
        "--lora_dropout", "0.05", "--freeze_vit", "true", "--freeze_aligner", str(freeze_aligner).lower(),
        "--freeze_llm", "false", "--add_non_thinking_prefix", "true", "--loss_scale", "ignore_empty_think",
        "--dataset", str(scratch / "train.jsonl"), "--val_dataset", str(scratch / "validation.jsonl"),
        "--split_dataset_ratio", "0", "--torch_dtype", "bfloat16", "--attn_impl", "sdpa",
        "--per_device_train_batch_size", str(batch), "--per_device_eval_batch_size", str(batch),
        "--gradient_accumulation_steps", str(accumulation), "--learning_rate", str(learning_rate),
        "--lr_scheduler_type", "cosine", "--warmup_ratio", "0.05", "--num_train_epochs", "1", "--max_length", "4096",
        "--eval_steps", str(cadence), "--save_steps", str(cadence), "--save_total_limit", "3", "--logging_steps", "1",
        "--dataloader_num_workers", "0", "--seed", str(seed), "--output_dir", str(output),
    ]
    if run_mode == "smoke":
        command += ["--max_steps", "8"]
    if model_revision:
        if len(model_revision) != 40 or any(char not in "0123456789abcdef" for char in model_revision):
            raise ValueError("model_revision must be an exact Hugging Face commit")
        command += ["--model_revision", model_revision]
    if initialize_adapter:
        config = json.loads((initialize_adapter / "adapter_config.json").read_text(encoding="utf-8"))
        if config.get("base_model_name_or_path") != BASE_MODEL or config.get("r") != rank:
            raise ValueError("Adapter does not match the configured base model and LoRA rank")
        if not (initialize_adapter / "adapter_model.safetensors").is_file():
            raise ValueError("Adapter safetensors not found; deployment GGUF is not a training checkpoint")
        command += ["--adapters", str(initialize_adapter)]
    return command, {"IMAGE_MAX_TOKEN_NUM": str(image_tokens), "PYTHONUNBUFFERED": "1",
                     "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True", "CUDA_VISIBLE_DEVICES": "0"}


def install_colab_stack(install_cuda_accelerators):
    pip = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    subprocess.run(pip + ["--index-url", "https://download.pytorch.org/whl/cu130", "torch==2.14.1+cu130",
                          "torchvision==0.29.1+cu130"], check=True)
    subprocess.run(pip + ["ms-swift==4.6.1", "transformers==5.22.2", "qwen-vl-utils==0.0.14", "peft==0.20.0",
                          "torchao==0.18.0", "Pillow==11.3.0", "flash-linear-attention==0.6.0"], check=True)
    if install_cuda_accelerators:
        subprocess.run(pip + ["--no-build-isolation", "git+https://github.com/Dao-AILab/causal-conv1d@cd81f0413cad2fc1e6f17e785ac39f59aae690cd"], check=True)
    imports = "import swift, transformers, torch, torchvision, peft, torchao; from peft.import_utils import is_torchao_available; assert is_torchao_available()"
    subprocess.run([sys.executable, "-c", imports], check=True)


def install_cpu_export_stack():
    from importlib.metadata import PackageNotFoundError, version

    packages = {"ms-swift": "4.6.1", "transformers": "5.22.2", "peft": "0.20.0",
                "qwen-vl-utils": "0.0.14", "torchao": "0.18.0"}
    requirements = []
    for name, pinned in packages.items():
        try:
            if version(name) == pinned:
                continue
        except PackageNotFoundError:
            pass
        requirements.append(f"{name}=={pinned}")
    if requirements:
        subprocess.run([sys.executable, "-m", "pip", "install", *requirements], check=True)


def checkpoints(output):
    return sorted((path for path in output.rglob("checkpoint-*") if (path / "adapter_model.safetensors").is_file()),
                  key=lambda path: int(path.name.removeprefix("checkpoint-")))


def require_a100():
    hardware = subprocess.check_output(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], text=True).strip()
    if "A100" not in hardware:
        raise RuntimeError(f"This pilot requires an A100, found {hardware!r}")
    print(f"GPU: {hardware}")


def pilot_receipt(drive_root, report, sources, model_revision, *, training):
    if not all(report["train_actions"].get(action, 0) for action in ("left_click", "type", "scroll", "not_found")):
        raise ValueError("Pilot is missing required action coverage")
    path = drive_root / "m3_runs" / "pilot_cpu_preflight.json"
    summary = {key: value for key, value in report.items() if not key.endswith("hashes")}
    payload = {"model_revision": model_revision, "sources_config": sources, "summary": summary}
    if training:
        if not path.is_file():
            raise FileNotFoundError("Run this notebook with RUN_TRAINING=False on Colab CPU first")
        previous = json.loads(path.read_text(encoding="utf-8"))
        if previous["payload"] != json.loads(json.dumps(payload)):
            raise ValueError("Data/config differ from the CPU preflight; repeat it before training")
        print(f"Matched CPU preflight from {previous['checked_at']}")
    else:
        gpu = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True) if shutil.which("nvidia-smi") else None
        if gpu and gpu.returncode == 0 and gpu.stdout.strip():
            raise RuntimeError("Switch the runtime to CPU to issue the pilot preflight receipt")
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, {"checked_at": datetime.now(timezone.utc).isoformat(), "payload": payload})
        print(f"CPU pilot preflight passed. Receipt: {path}")


def run_training(command, environment, scratch, run_dir):
    gpu_check = "import torch; assert torch.cuda.is_available() and torch.cuda.is_bf16_supported(); print(torch.cuda.get_device_name(0))"
    subprocess.run([sys.executable, "-c", gpu_check], check=True)
    run_dir.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(scratch / "data_receipt.json", run_dir / "data_receipt.json")
    write_json(run_dir / "training_config.json", {"command": command, "environment": environment,
                                                 "started_at": datetime.now(timezone.utc).isoformat()})
    with (run_dir / "pip_freeze.txt").open("w", encoding="utf-8") as stream:
        subprocess.run([sys.executable, "-m", "pip", "freeze"], stdout=stream, check=True)
    with (run_dir / "sft.log").open("w", encoding="utf-8", buffering=1) as log:
        with subprocess.Popen(command, env={**os.environ, **environment}, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace") as process:
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
            if process.wait():
                raise RuntimeError(f"SFT failed; inspect {run_dir / 'sft.log'}")
    available = checkpoints(run_dir / "checkpoints")
    if not available:
        raise RuntimeError("No complete LoRA checkpoint was written")
    return available[-1]


def export_gguf(adapter, run_dir, scratch, revision, device="cpu"):
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        raise ValueError("Pin the verified llama.cpp commit before export")
    if not (adapter / "adapter_config.json").is_file() or not (adapter / "adapter_model.safetensors").is_file():
        raise ValueError("Export requires a complete PEFT LoRA checkpoint")
    if device not in {"cpu", "auto"}:
        raise ValueError("Export device must be cpu or auto")
    config = json.loads((adapter / "adapter_config.json").read_text(encoding="utf-8"))
    if config.get("base_model_name_or_path", "").split("/")[-1] not in {BASE_MODEL.rsplit("/", 1)[-1], BASE_REVISION}:
        raise ValueError("Adapter does not identify the expected Qwen base")
    root = scratch / "llama.cpp"
    build = root / "build"
    merged = scratch / "merged"
    bundle = scratch / "gguf"
    bundle.mkdir()
    subprocess.run(["swift", "export", "--adapters", str(adapter), "--use_hf", "true", "--merge_lora", "true",
                    "--model", BASE_MODEL, "--model_revision", BASE_REVISION, "--device_map", device,
                    "--torch_dtype", "bfloat16", "--output_dir", str(merged)], check=True)
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "fetch", "--depth", "1", "https://github.com/ggml-org/llama.cpp.git", revision], check=True)
    subprocess.run(["git", "-C", str(root), "checkout", "--detach", "FETCH_HEAD"], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "gguf", "sentencepiece"], check=True)
    subprocess.run(["cmake", "-S", str(root), "-B", str(build), "-DGGML_CUDA=OFF", "-DLLAMA_BUILD_TESTS=OFF",
                    "-DLLAMA_BUILD_SERVER=OFF", "-DLLAMA_BUILD_EXAMPLES=OFF", "-DLLAMA_BUILD_TOOLS=ON"], check=True)
    subprocess.run(["cmake", "--build", str(build), "--target", "llama-quantize", "-j4"], check=True)
    converter = [sys.executable, str(root / "convert_hf_to_gguf.py"), str(merged)]
    bf16 = bundle / "m3-bf16.gguf"
    projector = bundle / "m3-mmproj-f16.gguf"
    subprocess.run(converter + ["--outfile", str(bf16), "--outtype", "bf16"], check=True)
    subprocess.run(converter + ["--mmproj", "--outfile", str(projector), "--outtype", "f16"], check=True)
    files = [projector]
    for quant in ("Q8_0", "Q4_K_M"):
        target = bundle / f"m3-qwen35-2b-{quant}.gguf"
        subprocess.run([str(build / "bin" / "llama-quantize"), str(bf16), str(target), quant], check=True)
        files.append(target)
    for path in files:
        with path.open("rb") as stream:
            if stream.read(4) != b"GGUF":
                raise ValueError(f"Invalid GGUF: {path.name}")
    output = run_dir / "gguf"
    output.mkdir(parents=True)
    for path in files:
        shutil.copyfile(path, output / path.name)
    write_json(output / "export_receipt.json", {"revision": revision, "model_revision": BASE_REVISION,
                                                "adapter": str(adapter), "deployment_validated": False,
                                                "sha256": {path.name: digest(path) for path in files}})


def score_fixture_holdout(archive_path, predictions_path, data_receipt_path, scratch):
    data_receipt = json.loads(data_receipt_path.read_text(encoding="utf-8"))
    scratch.mkdir(parents=True, exist_ok=False)
    extract_bundle(archive_path, scratch)
    capture_meta = json.loads((scratch / "capture.json").read_text(encoding="utf-8"))
    if capture_meta.get("purpose") != "HOLDOUT_DO_NOT_TRAIN":
        raise ValueError("Expected the independent fixture holdout bundle")
    if set(capture_meta.get("groups", [])) & set(data_receipt.get("train_groups_in_capture_manifests", [])):
        raise ValueError("Holdout page families overlap with captured training families")
    train_dev_hashes = set(data_receipt["train_hashes"]) | set(data_receipt["validation_hashes"])
    truth = {}
    for row in jsonl(scratch / "holdout_chrome.jsonl"):
        checksum = digest(inside(scratch, row["image"]))
        if checksum != row["image_sha256"] or checksum in train_dev_hashes or row["sample_id"] in truth:
            raise ValueError("Independent holdout has a bad, duplicate, or leaked sample")
        check_action(row["expected"])
        truth[row["sample_id"]] = row
    if not truth or len(truth) != capture_meta["count"]:
        raise ValueError("Holdout count does not match its capture manifest")
    predictions = {}
    for row in jsonl(predictions_path):
        identifier = row["sample_id"]
        if identifier not in truth or identifier in predictions:
            raise ValueError(f"Unknown or duplicate prediction ID: {identifier}")
        predictions[identifier] = row["prediction"]
    if set(predictions) != set(truth):
        raise ValueError(f"Predictions must cover all {len(truth)} holdout samples")
    metrics = {name: Counter() for name in sorted({row["expected"]["action"] for row in truth.values()})}
    for identifier, row in truth.items():
        expected = row["expected"]
        counters = metrics[expected["action"]]
        counters["total"] += 1
        try:
            predicted = predictions[identifier]
            if isinstance(predicted, str):
                predicted = json.loads(predicted)
            check_action(predicted)
        except (ValueError, TypeError):
            continue
        counters["valid_json_action"] += 1
        if predicted["action"] != expected["action"]:
            counters["false_not_found"] += predicted["action"] == "not_found"
            continue
        counters["correct_action"] += 1
        hit = True
        if row["bbox"] is not None:
            left, top, right, bottom = row["bbox"]
            point = predicted.get("coordinate")
            hit = point is not None and left <= point[0] <= right and top <= point[1] <= bottom
            counters["point_in_box"] += hit
        for field in ("text", "direction", "amount"):
            if field in expected:
                matches = predicted.get(field) == expected[field]
                counters[f"correct_{field}"] += matches
                hit = hit and matches
        counters["functional_match"] += hit
    return {"sample_count": len(truth), "by_expected_action": {key: dict(counts) for key, counts in metrics.items()}}


def build_notebook(destination):
    source = Path(__file__).read_text(encoding="utf-8").split("\ndef build_notebook(", 1)[0]

    def cell(kind, identifier, text):
        extra = {"execution_count": None, "outputs": []} if kind == "code" else {}
        return {"cell_type": kind, "id": identifier, "metadata": {}, "source": text.strip().splitlines(keepends=True), **extra}

    config = '''from google.colab import drive
drive.mount("/content/drive")

DRIVE_ROOT = Path("/content/drive/MyDrive/chrome-gui-pilot")
MODE = "pilot"
RUN_ID = datetime.now(timezone.utc).strftime("m3-%Y%m%d-%H%M%S") + "-" + MODE
SEED = 42
MODEL_REVISION = BASE_REVISION
INCLUDE_FIXTURE = True
EXTRA_BUNDLES = OPTION_A_BUNDLES if MODE == "pilot" else []
INITIAL_ADAPTER = None
FREEZE_ALIGNER = True
INSTALL_STACK = False
INSTALL_CUDA_ACCELERATORS = False
RUN_TRAINING = False
RUN_EXPORT = False
EXPORT_ADAPTER = None
LLAMA_CPP_REVISION = LLAMA_CPP_EXPORT_REVISION
EXPORT_ONLY = RUN_EXPORT and not RUN_TRAINING

assert MODE in {"smoke", "pilot", "full"}
assert MODE != "full" or not RUN_TRAINING or MODEL_REVISION, "Pin the HF model revision for a full run"
WORK = Path("/content/chrome-pilot-m3") / RUN_ID
RUN_DIR = DRIVE_ROOT / "m3_runs" / RUN_ID
assert not (RUN_TRAINING and RUN_DIR.exists()), "Choose a fresh run ID"
assert not RUN_EXPORT or RUN_TRAINING or EXPORT_ADAPTER, "Set EXPORT_ADAPTER to a saved PEFT checkpoint"
SOURCES = [("m1_web", "m1_web_bundle.tar", "train_open_web.jsonl", "val_open_web.jsonl",
            {"smoke": 500, "pilot": 1000, "full": None}[MODE])]
if INCLUDE_FIXTURE:
    SOURCES.append(("zh_fixture", FIXTURE_TRAIN, "train_chrome.jsonl", None, None))
SOURCES.extend(EXTRA_BUNDLES)
expected_archives = {}
if MODE == "pilot" and not EXPORT_ONLY:
    manifest = json.loads((DRIVE_ROOT / "data" / "m3_option_a_manifest.json").read_text(encoding="utf-8"))
    expected_archives = {relative: manifest["bundles"][relative] for _, relative, _, _, _ in OPTION_A_BUNDLES}
'''
    sections = [
        cell("markdown", "intro", """# M3 — Chrome GUI Pilot SFT
默认只校验数据并打印命令，不安装、不训练、不导出。先用 Colab CPU 检查，确认配比再切 A100。训练、缓存、下载都在 Colab；日志、LoRA checkpoint 和显式导出的模型进 Drive 的 `m3_runs/`。

- 默认采用稳妥的 Option A `pilot`：M1 最多 1,000 条 + 中文原始补样 60 条 + SATraj Chrome、中文同义改写、困难负样本；两份独立考题不参与。CPU 核对归档哈希和数据后写入回执，A100 会重新核对一致性再安装或训练。
- 其他公共源须先在 Colab 转成明确的单图 SFT JSONL（`sample_id`, `messages`, `images`），验证许可、版本、标注、近重复及页面族分割后才写进 `EXTRA_BUNDLES`。格式为 `(来源名, data/ 中压缩包相对路径, 训练清单, 开发清单或 None, 训练上限或 None)`。不把手机录屏当 Chrome 原生栏数据，不自动制造未经核实的拒识真值。
- 冻结视觉塔及对齐层，先对照 M2。解冻对齐层、提高视觉 token 或混入新数据分别做对照，勿一次同时改多个变量。全量 M1 会稀释 60 条补样，先检查动作分布。
- `HOLDOUT_DO_NOT_TRAIN` 与 ScreenSpot 不输入此训练 Notebook。它们只用于锁定方案之后的验收，不用于选择 checkpoint。GGUF 导出成功不等于验收通过；还必须与原模型同图比较落框、精确文字、滚动和拒识。
- `smoke` 取最多 500 条 M1 和 100 条开发集，跑 8 步；`pilot` 训练 1 个 epoch；`full` 全量且要求固定 HF revision。`INITIAL_ADAPTER` 只接受真实 PEFT LoRA 目录，不能用 M2 GGUF 续训。
- 如需 A100，安装/训练开关需要显式开启；无自动启动、关机或变更已有模型。可用 `EXPORT_ADAPTER` 单独导出已有 LoRA，不必重训。"""),
        cell("code", "implementation", source),
        cell("code", "configure", config),
        cell("code", "preflight", '''if not EXPORT_ONLY:
    if RUN_TRAINING and MODE == "pilot":
        require_a100()
    report = prepare(DRIVE_ROOT / "data", WORK, SOURCES, SEED,
                     dev_limit=100 if MODE == "smoke" else None, expected_archives=expected_archives)
    print(json.dumps({key: value for key, value in report.items() if not key.endswith("hashes")}, ensure_ascii=False, indent=2))
    if MODE == "pilot":
        pilot_receipt(DRIVE_ROOT, report, SOURCES, MODEL_REVISION, training=RUN_TRAINING)
    command, run_env = swift_command(WORK, RUN_DIR / "checkpoints", run_mode=MODE, model_revision=MODEL_REVISION,
        seed=SEED, rank=32, batch=4, accumulation=8, image_tokens=1024, learning_rate=1e-4,
        freeze_aligner=FREEZE_ALIGNER, initialize_adapter=INITIAL_ADAPTER)
    print(command)
else:
    print("Export-only mode: no datasets are read.")
'''),
        cell("code", "train", '''if INSTALL_STACK:
    if EXPORT_ONLY:
        install_cpu_export_stack()
    else:
        install_colab_stack(INSTALL_CUDA_ACCELERATORS)
adapter = run_training(command, run_env, WORK, RUN_DIR) if RUN_TRAINING else None
print(f"LoRA checkpoint: {adapter}" if adapter else "Data preflight only; no training was launched.")'''),
        cell("code", "export", '''if RUN_EXPORT:
    WORK.mkdir(parents=True, exist_ok=not EXPORT_ONLY)
    export_gguf(adapter if adapter is not None else Path(EXPORT_ADAPTER), RUN_DIR, WORK, LLAMA_CPP_REVISION)
    print(f"GGUF artifacts: {RUN_DIR / 'gguf'} (deployment validation pending)")'''),
        cell("markdown", "holdout-note", """## 独立验收（锁定模型之后再运行）
用同一套截图、提示和推理参数分别预测基座、M2、M3。预测 JSONL 每行形如 `{"sample_id":"…","prediction":"{…}"}`，也可将 `prediction` 直接写成对象。验收读取本轮数据校验回执，检查截图哈希和已知页面族是否泄漏；分动作报告落框、打字原文、滚动方向/量和拒识，不启动浏览器或发送输入。"""),
        cell("code", "holdout", '''HOLDOUT_PREDICTIONS = None
if HOLDOUT_PREDICTIONS:
    metrics = score_fixture_holdout(DRIVE_ROOT / "data" / FIXTURE_HOLDOUT, Path(HOLDOUT_PREDICTIONS),
        WORK / "data_receipt.json", WORK / ("holdout-" + datetime.now(timezone.utc).strftime("%H%M%S")))
    print(json.dumps(metrics, ensure_ascii=False, indent=2))'''),
    ]
    notebook = {"cells": sections, "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}},
                "nbformat": 4, "nbformat_minor": 5}
    write_json(destination, notebook)


if __name__ == "__main__":
    build_notebook(Path(__file__).resolve().parents[1] / "notebooks" / "M3_Colab_A100_Chrome_SFT.ipynb")
