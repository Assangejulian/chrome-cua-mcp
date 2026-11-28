"""Compare M3.5 vs M4 on ask_screen validation samples via the production M35Grounder path.

Reports per model: JSON-leak rate, length compliance [15,80], char-bigram F1 vs reference.
Usage: m4_eval_askscreen.py --limit 48
"""
import argparse, io, json, sys, tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cua_engine import M35Grounder
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
PROMPT = M35Grounder._SCREEN_READER_PROMPT


def bigram_f1(a: str, b: str) -> float:
    def bg(s):
        return [s[i:i+2] for i in range(len(s)-1)] or [s]
    from collections import Counter
    A, B = Counter(bg(a)), Counter(bg(b))
    inter = sum((A & B).values())
    if not inter:
        return 0.0
    p, r = inter/max(1, sum(A.values())), inter/max(1, sum(B.values()))
    return 2*p*r/(p+r) if (p+r) else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=48)
    ap.add_argument("--out", type=Path, default=ROOT/"artifacts/m4_askscreen_eval")
    ap.add_argument("--port", type=int, default=18310)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    rows = [json.loads(l) for l in open(ROOT/"artifacts/m4_bundle_20261125/val_m4_teacher.jsonl", encoding="utf-8")]
    qa = [r for r in rows if r["messages"][0]["content"] == PROMPT][:args.limit]
    img_dir = ROOT/"artifacts/m4_bundle/images"
    names = {p.name: p for p in img_dir.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")}
    samples = []
    for r in qa:
        nm = Path(r["images"][0]).name
        if nm in names:
            samples.append((r, names[nm].read_bytes()))
    print(f"loaded {len(samples)} qa samples", flush=True)

    grounder = M35Grounder(port=args.port)
    results = {}
    try:
        for label, key in (("M3.5 · Q4_K_M", "m35"), ("M4 · Q4_K_M", "m4")):
            grounder.set_model(label)
            grounder.ensure_started()
            out_rows = []
            for i, (r, raw) in enumerate(samples):
                img = Image.open(io.BytesIO(raw)).convert("RGB")
                user = r["messages"][1]["content"]
                if user.startswith("<image>"):
                    user = user[7:]
                question, sep, ctx = user.partition("\n\n屏幕文字参考：\n")
                question = question.removeprefix("问题：")
                try:
                    ans, infer_s, usage = grounder.ask_screen(img, question, ctx if sep else "")
                except Exception as exc:
                    ans, infer_s = f"ERROR:{exc}", 0.0
                ans = (ans or "").strip()
                leak = ans.startswith("{") or ans.startswith("```") or '"action"' in ans
                ref = r["messages"][2]["content"].strip()
                out_rows.append({
                    "sample_id": r["sample_id"], "question": question[:80],
                    "reference": ref, "answer": ans, "json_leak": leak,
                    "len_ok": 15 <= len(ans) <= 80, "f1": round(bigram_f1(ans, ref), 3),
                    "infer_s": round(infer_s, 2),
                })
                if i % 12 == 0:
                    print(f"  {key} {i+1}/{len(samples)}", flush=True)
            results[key] = out_rows
    finally:
        grounder.shutdown()

    summary = {}
    for key, rows_out in results.items():
        n = max(1, len(rows_out))
        summary[key] = {
            "n": len(rows_out),
            "json_leak_rate": round(sum(r["json_leak"] for r in rows_out)/n, 3),
            "len_ok_rate": round(sum(r["len_ok"] for r in rows_out)/n, 3),
            "mean_f1": round(sum(r["f1"] for r in rows_out)/n, 3),
            "mean_infer_s": round(sum(r["infer_s"] for r in rows_out)/n, 2),
        }
    (args.out/"results.json").write_text(json.dumps({"summary": summary, "rows": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
