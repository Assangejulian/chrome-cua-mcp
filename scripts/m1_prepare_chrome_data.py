"""Convert annotated Chrome recording JSONL files into leak-free SFT splits, metadata index, and visual review overlays."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import random
import sys

from PIL import Image, ImageDraw

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from m0_bench_vlm import SYSTEM_PROMPT, resize_to_token_budget


def validate_norm_box(bbox: list[int] | None) -> list[int] | None:
    if bbox is None:
        return None
    if len(bbox) != 4:
        return None
    x1, y1, x2, y2 = [int(round(v)) for v in bbox]
    if not (0 <= x1 < x2 <= 1000 and 0 <= y1 < y2 <= 1000):
        return None
    if (x2 - x1) < 2 or (y2 - y1) < 2:
        return None
    return [x1, y1, x2, y2]


def build_assistant_payload(rec: dict) -> dict | None:
    action = rec.get("action")
    if action in ("left_click", "double_click", "right_click", "move"):
        coord = rec.get("coordinate")
        if not isinstance(coord, list) or len(coord) != 2:
            return None
        x, y = int(round(coord[0])), int(round(coord[1]))
        if not (0 <= x <= 1000 and 0 <= y <= 1000):
            return None
        return {"action": action, "coordinate": [x, y]}
    if action == "scroll":
        coord = rec.get("coordinate")
        if not isinstance(coord, list) or len(coord) != 2:
            return None
        return {
            "action": "scroll",
            "coordinate": [int(round(coord[0])), int(round(coord[1]))],
            "direction": rec.get("direction", "down"),
            "amount": int(rec.get("amount", 3)),
        }
    if action == "type":
        text = str(rec.get("text") or "").strip()
        if not text:
            return None
        payload = {"action": "type", "text": text}
        if isinstance(rec.get("coordinate"), list) and len(rec["coordinate"]) == 2:
            payload["coordinate"] = [int(round(rec["coordinate"][0])), int(round(rec["coordinate"][1]))]
        return payload
    if action == "hotkey":
        keys = rec.get("keys")
        if not isinstance(keys, list) or not keys:
            return None
        return {"action": "hotkey", "keys": [str(k).lower() for k in keys]}
    if action == "wait":
        return {"action": "wait", "seconds": float(rec.get("seconds", 1.0))}
    if action == "done":
        return {"action": "done"}
    if action == "not_found":
        return {"action": "not_found", "reason": str(rec.get("reason") or "Target element not found")}
    return None


def draw_review_overlay(img: Image.Image, rec: dict, out_path: Path) -> None:
    """Save diagnostic PNG showing bbox (blue) and target click point (green) for visual QC."""
    vis = img.copy()
    draw = ImageDraw.Draw(vis)
    w, h = vis.size
    bbox = rec.get("bbox")
    if isinstance(bbox, list) and len(bbox) == 4:
        x1 = round(bbox[0] / 1000.0 * (w - 1))
        y1 = round(bbox[1] / 1000.0 * (h - 1))
        x2 = round(bbox[2] / 1000.0 * (w - 1))
        y2 = round(bbox[3] / 1000.0 * (h - 1))
        draw.rectangle([x1, y1, x2, y2], outline=(30, 144, 255), width=2)
    coord = rec.get("coordinate")
    if isinstance(coord, list) and len(coord) == 2:
        cx = round(coord[0] / 1000.0 * (w - 1))
        cy = round(coord[1] / 1000.0 * (h - 1))
        draw.line([(cx - 8, cy), (cx + 8, cy)], fill=(0, 220, 0), width=2)
        draw.line([(cx, cy - 8), (cx, cy + 8)], fill=(0, 220, 0), width=2)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    vis.save(out_path, format="PNG")


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare annotated Chrome records into train/val/test SFT splits.")
    parser.add_argument("--input-dir", default=str(PROJECT_ROOT / "data" / "recorded_chrome"))
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "data" / "prepared_chrome"))
    parser.add_argument("--max-image-tokens", type=int, default=1024)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--visualize", action="store_true", help="Render QC overlays for visual inspection.")
    args = parser.parse_args()

    in_dir = Path(args.input_dir)
    out_dir = Path(args.output_dir)
    img_out = out_dir / "images"
    img_out.mkdir(parents=True, exist_ok=True)
    review_dir = out_dir / "review_overlays"

    jsonl_files = sorted(in_dir.glob("*.jsonl"))
    if not jsonl_files:
        print(f"[INFO] No JSONL files found in {in_dir}")
        return 0

    groups: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    skipped = 0

    for jf in jsonl_files:
        with open(jf, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                inst = str(rec.get("instruction") or "").strip()
                if not inst:
                    skipped += 1
                    continue
                assistant_obj = build_assistant_payload(rec)
                if assistant_obj is None:
                    skipped += 1
                    continue

                src_img = in_dir / rec["image_path"]
                if not src_img.exists():
                    skipped += 1
                    continue
                with Image.open(src_img) as raw_img:
                    rgb = raw_img.convert("RGB")
                    resized = resize_to_token_budget(rgb, max_tokens=args.max_image_tokens)

                rel_img = f"images/{rec['sample_id']}.jpg"
                resized.save(out_dir / rel_img, format="JPEG", quality=94)

                bbox = validate_norm_box(rec.get("bbox"))
                sft_row = {
                    "sample_id": rec["sample_id"],
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": f"<image>{inst}"},
                        {"role": "assistant", "content": json.dumps(assistant_obj, ensure_ascii=False)},
                    ],
                    "images": [rel_img],
                }
                meta_row = {**rec, "image_path": rel_img, "bbox": bbox, "img_w": resized.size[0], "img_h": resized.size[1]}
                if args.visualize:
                    draw_review_overlay(resized, meta_row, review_dir / f"{rec['sample_id']}_qc.png")

                group_key = f"{rec.get('site_domain', 'unknown')}::{rec.get('session_id', jf.stem)}"
                groups[group_key].append((sft_row, meta_row))

    # Leak-free split strictly by (site_domain, session_id)
    group_keys = sorted(groups.keys())
    rng = random.Random(args.seed)
    rng.shuffle(group_keys)

    n_groups = len(group_keys)
    n_test = max(1, round(n_groups * 0.15)) if n_groups >= 4 else 0
    n_val = max(1, round(n_groups * 0.10)) if n_groups >= 3 else 0
    test_keys = set(group_keys[:n_test])
    val_keys = set(group_keys[n_test : n_test + n_val])

    splits = {"train": [], "val": [], "test": []}
    meta_all = []
    for k in group_keys:
        bucket = "test" if k in test_keys else ("val" if k in val_keys else "train")
        for sft_row, meta_row in groups[k]:
            splits[bucket].append(sft_row)
            meta_all.append({**meta_row, "split": bucket})

    for name, rows in splits.items():
        with open(out_dir / f"{name}_chrome.jsonl", "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(out_dir / "metadata_chrome.jsonl", "w", encoding="utf-8") as f:
        for r in meta_all:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(
        f"[DONE] groups={n_groups} train={len(splits['train'])} val={len(splits['val'])} "
        f"test={len(splits['test'])} skipped={skipped}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
