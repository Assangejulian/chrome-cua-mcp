"""Pack M4 SFT bundle: teacher_big + teacher_pilot + teacher_m4_patch -> ms-swift format.

Action samples use the action SYSTEM_PROMPT (exact, from m3_train_colab);
ask_screen QA samples use the screen-reader prompt with plain-text answers.
Output: artifacts/m4_bundle/ (images + train.jsonl + validation.jsonl + receipt).
"""
import hashlib, json, random, sys, tarfile, io
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from m3_train_colab import SYSTEM_PROMPT, check_action
from cua_engine import M35Grounder

ROOT = Path(".")
OUT = Path("artifacts/m4_bundle")
IMG = OUT / "images"
SCREEN_PROMPT = M35Grounder._SCREEN_READER_PROMPT
VAL_RATIO = 0.04
random.seed(42)

def digest_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def load_jsonl(p):
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]

def action_samples(path, source, scroll_downsample=False):
    rows, drops = [], {"unverified": 0, "bad_action": 0, "extended": 0, "dup_scroll": 0, "missing_img": 0}
    recs = load_jsonl(path)
    scroll_seen = {}
    for r in recs:
        if not r.get("verified"):
            drops["unverified"] += 1; continue
        if r.get("extended_action"):
            drops["extended"] += 1; continue
        exp = r.get("expected")
        try:
            check_action(exp)
        except Exception:
            drops["bad_action"] += 1; continue
        if scroll_downsample and exp["action"] == "scroll" and r.get("bucket") == "web_scroll":
            key = (r.get("site"), exp.get("direction"))
            scroll_seen[key] = scroll_seen.get(key, 0) + 1
            if scroll_seen[key] % 3 != 1:
                drops["dup_scroll"] += 1; continue
        img_src = path.parent / r["image"]
        if not img_src.is_file():
            drops["missing_img"] += 1; continue
        rows.append((r, exp, img_src, source))
    return rows, drops

def main():
    if IMG.exists():
        import shutil; shutil.rmtree(OUT)
    IMG.mkdir(parents=True)
    all_rows = []
    stats = {}

    for path, source, ds in [
        (Path("artifacts/teacher_big/records.jsonl"), "tb", True),
        (Path("artifacts/teacher_pilot/records.jsonl"), "tp", False),
        (Path("artifacts/teacher_m4_patch/records.jsonl"), "m4t", False),
    ]:
        rows, drops = action_samples(path, source, scroll_downsample=ds)
        stats[source] = {"kept": len(rows), "drops": drops}
        all_rows.extend(rows)

    out_rows, img_map = [], {}
    for r, exp, img_src, source in all_rows:
        data = img_src.read_bytes()
        h = digest_bytes(data)[:16]
        img_name = f"{h}{img_src.suffix.lower()}"
        dst = IMG / img_name
        if not dst.exists():
            dst.write_bytes(data)
        inst = r["instruction"].strip()
        if not inst.startswith("<image>"):
            inst = "<image>" + inst
        out_rows.append({
            "sample_id": f"{source}:{r['sample_id']}",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": inst},
                {"role": "assistant", "content": json.dumps(exp, ensure_ascii=False, separators=(",", ":"))},
            ],
            "images": [f"images/{img_name}"],
        })

    qa_drops = {"bad_len": 0}
    n_qa = 0
    for r in load_jsonl(Path("artifacts/teacher_m4_patch/records_qa.jsonl")):
        img_src = Path("artifacts/teacher_m4_patch") / r["image"]
        if not img_src.is_file():
            qa_drops["bad_len"] += 1; continue
        data = img_src.read_bytes()
        h = digest_bytes(data)[:16]
        img_name = f"{h}{img_src.suffix.lower()}"
        dst = IMG / img_name
        if not dst.exists():
            dst.write_bytes(data)
        out_rows.append({
            "sample_id": f"m4q:{r['sample_id']}",
            "messages": [
                {"role": "system", "content": SCREEN_PROMPT},
                {"role": "user", "content": "<image>" + r["instruction"]},
                {"role": "assistant", "content": r["answer"]},
            ],
            "images": [f"images/{img_name}"],
        })
        n_qa += 1
    stats["m4q"] = {"kept": n_qa, "drops": qa_drops}

    random.shuffle(out_rows)
    n_val = max(40, int(len(out_rows) * VAL_RATIO))
    val, train = out_rows[:n_val], out_rows[n_val:]
    write = lambda p, rows: open(p, "w", encoding="utf-8").write("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows))
    write(OUT / "train.jsonl", train)
    write(OUT / "validation.jsonl", val)

    receipt = {
        "train": len(train), "validation": len(val), "images": len(list(IMG.iterdir())),
        "per_source": stats,
        "action_system_prompt": SYSTEM_PROMPT,
        "screen_reader_system_prompt": SCREEN_PROMPT,
    }
    (OUT / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")

    tar_path = Path("artifacts/m4_bundle.tar")
    with tarfile.open(tar_path, "w") as tf:
        tf.add(OUT, arcname="m4_bundle")
    sha = digest_bytes(tar_path.read_bytes())
    (Path("artifacts/m4_bundle.tar.sha256")).write_text(sha, encoding="utf-8")
    print(f"train={len(train)} val={len(val)} images={receipt['images']} tar={tar_path.stat().st_size>>20}MB sha={sha[:12]}")
    print("per-source:", json.dumps({k: v["kept"] for k, v in stats.items()}))
    print("drops:", json.dumps({k: v["drops"] for k, v in stats.items() if v["drops"]}, ensure_ascii=False)[:400])

if __name__ == "__main__":
    main()
