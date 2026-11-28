"""Compare local GGUF models on the same saved screenshots without sending GUI input."""

import argparse
import base64
from collections import Counter
import io
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time
import zipfile

from PIL import Image

from m0_bench_vlm import call_vlm, percentile, wait_for_server
from m3_train_colab import SYSTEM_PROMPT, check_action, digest, score_fixture_holdout, write_json, write_jsonl


ROOT = Path(__file__).resolve().parents[1]
MODEL_FILES = {
    "base": ("qwen35_2b_gguf", "Qwen3.5-2B-Q4_K_M.gguf", "mmproj-F16.gguf"),
    "m2": ("m2_gguf", "m2-qwen35-2b-Q4_K_M.gguf", "m2-mmproj-f16.gguf"),
    "m3": ("m3_gguf", "m3-qwen35-2b-Q4_K_M.gguf", "m3-mmproj-f16.gguf"),
    "m35": ("m35_gguf", "m35-qwen35-2b-Q4_K_M.gguf", "m35-mmproj-f16.gguf"),
    "m4": ("m4_gguf", "m4-qwen35-2b-Q4_K_M.gguf", "m4-mmproj-f16.gguf"),
}


def load_cases(archive, suite="fixture"):
    with zipfile.ZipFile(archive) as bundle:
        metadata = json.loads(bundle.read("capture.json"))
        expected_purpose = {"fixture": "HOLDOUT_DO_NOT_TRAIN", "native": "NATIVE_CHROME_SMOKE", "venus": "VENUS_FIXED_SAMPLE"}[suite]
        if metadata["purpose"] != expected_purpose:
            raise ValueError("Not the independent Chrome holdout")
        cases = [json.loads(line) for line in bundle.read("holdout_chrome.jsonl").splitlines() if line.strip()]
        images, hashes = {}, {}
        for case in cases:
            name = case["image"]
            if name not in hashes:
                raw = bundle.read(name)
                with Image.open(io.BytesIO(raw)) as image:
                    mime = {"PNG": "image/png", "JPEG": "image/jpeg"}.get(image.format)
                    if mime is None:
                        raise ValueError("Unexpected holdout image format")
                    image.verify()
                hashes[name] = (sha256(raw).hexdigest(), f"data:{mime};base64," + base64.b64encode(raw).decode("ascii"))
            checksum, images[case["sample_id"]] = hashes[name]
            if checksum != case["image_sha256"]:
                raise ValueError(f"Holdout hash mismatch: {name}")
    if len(cases) != metadata["count"] or len(images) != len(cases):
        raise ValueError("Holdout count or IDs are inconsistent")
    return cases, images


def _check_export(model_key, expected_adapter_suffix=None):
    """Verify local GGUF model and mmproj SHA-256 digests against export_receipt.json."""
    model_dir, model, projector = MODEL_FILES[model_key]
    directory = ROOT / "models" / model_dir
    export = json.loads((directory / "export_receipt.json").read_text(encoding="utf-8"))
    if expected_adapter_suffix and not export["adapter"].endswith(expected_adapter_suffix):
        raise ValueError(f"Expected adapter ending with {expected_adapter_suffix}, got {export['adapter']}")
    for name in (model, projector):
        path = directory / name
        if digest(path) != export["sha256"][name]:
            raise ValueError(f"GGUF checksum mismatch: {path}")


def check_m3_export():
    _check_export("m3", expected_adapter_suffix="/checkpoint-110")


def check_m35_export():
    """Verify local M3.5 Q4_K_M GGUF and mmproj against export_receipt.json."""
    _check_export("m35")


def run_model(name, cases, images, port, directory, min_image_tokens=None):
    server = ROOT / "tools" / "llama_cpp" / "bin" / "llama-server.exe"
    model_dir, model, projector = MODEL_FILES[name]
    model_path = ROOT / "models" / model_dir / model
    projector_path = ROOT / "models" / model_dir / projector
    for path in (server, model_path, projector_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    url = f"http://127.0.0.1:{port}"
    command = [str(server), "--model", str(model_path), "--mmproj", str(projector_path),
               "--host", "127.0.0.1", "--port", str(port), "--ctx-size", "8192",
               "--n-gpu-layers", "99", "--image-max-tokens", "1024", "--reasoning", "off", "--jinja",
               "--no-cache-prompt", "--parallel", "1"]
    if min_image_tokens is not None:
        command.extend(["--image-min-tokens", str(min_image_tokens)])
    predictions = []
    first_case = cases[0]
    with (directory / f"{name}-server.log").open("w", encoding="utf-8") as log:
        with subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW) as proc:
            try:
                load_s = wait_for_server(url, proc, timeout_s=90)
                started = time.perf_counter()
                call_vlm(url, images[first_case["sample_id"]], first_case["instruction"], max_tokens=96,
                         system_prompt=SYSTEM_PROMPT)
                warmup_s = time.perf_counter() - started
                for case in cases:
                    identifier = case["sample_id"]
                    started = time.perf_counter()
                    raw, _ = call_vlm(url, images[identifier], case["instruction"], max_tokens=96,
                                      system_prompt=SYSTEM_PROMPT)
                    predictions.append({"sample_id": identifier, "prediction": raw, "request_s": time.perf_counter() - started})
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
    write_jsonl(directory / f"{name}-predictions.jsonl", predictions)
    latency = [row["request_s"] for row in predictions]
    return predictions, {"server_load_s": load_s, "warmup_request_s": warmup_s,
                         "warm_request_p50_s": percentile(latency, 50), "warm_request_p95_s": percentile(latency, 95)}


def score(cases, predictions, by_domain=False):
    actual = {row["sample_id"]: row["prediction"] for row in predictions}
    buckets = {}
    for case in cases:
        expected = case["expected"]
        action = expected["action"]
        key = f"{case['domain']}/{action}" if by_domain else action
        group = buckets.setdefault(key, Counter())
        group["total"] += 1
        try:
            value = json.loads(actual[case["sample_id"]])
            check_action(value)
        except (ValueError, TypeError):
            group["invalid"] += 1
            continue
        group["valid"] += 1
        group["false_refusal"] += action != "not_found" and value["action"] == "not_found"
        group["unsafe_action_proposed"] += action == "not_found" and value["action"] not in {"not_found", "done", "wait"}
        if value["action"] != action:
            continue
        group["correct_action"] += 1
        point_ok = False
        if case.get("bbox") is not None:
            left, top, right, bottom = case["bbox"]
            point = value.get("coordinate")
            point_ok = point is not None and left <= point[0] <= right and top <= point[1] <= bottom
            group["point_in_box"] += point_ok
        if action == "type":
            group["exact_text"] += value["text"] == expected["text"]
            point_ok = point_ok and value["text"] == expected["text"]
        elif action == "scroll":
            direction_ok = value["direction"] == expected["direction"]
            amount_ok = value["amount"] == expected["amount"]
            group["correct_direction"] += direction_ok
            group["exact_amount"] += amount_ok
            point_ok = point_ok and direction_ok and amount_ok
        group["functional_match"] += point_ok or action == "not_found"
    return {key: dict(value) for key, value in buckets.items()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--holdout", required=True, type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--models", nargs="+", choices=MODEL_FILES, default=list(MODEL_FILES))
    parser.add_argument("--port", type=int, default=18144)
    parser.add_argument("--native-smoke", action="store_true")
    parser.add_argument("--venus", action="store_true")
    parser.add_argument("--min-image-tokens", type=int, default=None)
    args = parser.parse_args()
    if args.venus and args.native_smoke:
        parser.error("Choose one test suite")
    if ("m3" in args.models or "m35" in args.models) and not args.receipt and not args.native_smoke:
        parser.error("--receipt from the original training run is required for M3/M3.5")
    if "m3" in args.models:
        check_m3_export()
    if "m35" in args.models:
        check_m35_export()
    suite = "venus" if args.venus else "native" if args.native_smoke else "fixture"
    cases, images = load_cases(args.holdout, suite=suite)
    if args.venus and args.receipt:
        receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
        seen = set(receipt["train_hashes"]) | set(receipt["validation_hashes"])
        if any(case["image_sha256"] in seen for case in cases):
            raise ValueError("Venus images overlap with known train/development hashes")
    args.out.mkdir(parents=True, exist_ok=False)
    report = {}
    for index, name in enumerate(args.models):
        predictions, latency = run_model(name, cases, images, args.port + index, args.out, min_image_tokens=args.min_image_tokens)
        if name in {"m3", "m35"} and suite == "fixture":
            with TemporaryDirectory(prefix=f"{name}_holdout_") as scratch:
                score_fixture_holdout(args.holdout, args.out / f"{name}-predictions.jsonl", args.receipt, Path(scratch) / "holdout")
        report[name] = {"by_action": score(cases, predictions), "latency": latency}
        if args.venus:
            report[name]["by_domain_action"] = score(cases, predictions, by_domain=True)
        write_json(args.out / "report.json", report)
        print(json.dumps({name: report[name]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
