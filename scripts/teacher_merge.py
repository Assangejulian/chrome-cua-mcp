"""Merge a secondary teacher collection dir into the main one with frame renumbering.

Usage: teacher_merge.py --src artifacts/teacher_big_b --dst artifacts/teacher_big
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    args = ap.parse_args()
    src, dst = Path(args.src), Path(args.dst)

    src_recs = [json.loads(l) for l in (src / "records.jsonl").read_text(encoding="utf-8").splitlines()
                if l.strip()]
    if not src_recs:
        print("nothing to merge")
        return 0
    existing = [int(p.stem[1:]) for p in (dst / "frames" / "train").glob("f*.png")
                if p.stem[1:].isdigit()]
    seq = max(existing, default=0)

    # map old frame -> new frame preserving order of first appearance
    order: list[str] = []
    for r in src_recs:
        fr = r["image"].split("/")[-1].split(".")[0]
        if fr not in order:
            order.append(fr)
    remap = {fr: f"f{seq + 1 + i:04d}" for i, fr in enumerate(order)}

    merged = 0
    with (dst / "records.jsonl").open("a", encoding="utf-8") as fp:
        for r in src_recs:
            old_fr = r["image"].split("/")[-1].split(".")[0]
            new_fr = remap[old_fr]
            for sub, ext in (("images", ".jpg"), (str(Path("frames") / "train"), ".png")):
                s = src / sub / f"{old_fr}{ext}"
                d = dst / sub / f"{new_fr}{ext}"
                if s.exists() and not d.exists():
                    shutil.copy2(s, d)
            old_sid = r.get("sample_id", "")
            r["sample_id"] = old_sid.replace(old_fr, new_fr, 1)
            r["image"] = f"images/{new_fr}.jpg"
            r["merged_from"] = src.name
            fp.write(json.dumps(r, ensure_ascii=False) + "\n")
            merged += 1
    print(f"merged {merged} records, frames remapped {len(remap)} (seq now {seq + len(remap)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
