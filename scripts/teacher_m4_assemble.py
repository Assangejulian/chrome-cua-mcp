"""Assemble teacher_m4_patch QA records from subagent fragments.

Validates length/charset/format, joins metadata + OCR, injects the exact
_SCREEN_READER_PROMPT, writes records_qa.jsonl. Usage: teacher_m4_assemble.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cua_engine import M35Grounder  # noqa: E402

PATCH = Path("artifacts/teacher_m4_patch")
PROMPT = M35Grounder._SCREEN_READER_PROMPT

QTYPE_NAME = {1: "page_summary", 2: "wall_detect", 3: "region_read", 4: "visibility"}



HOLDOUT_PAT = re.compile(r"(?i)(wikipedia|维基百科|git-scm|廖雪峰|阮一峰|ruanyifeng|liaoxuefeng|react\.dev|vite\.dev|vitejs|reddit)")

def valid_answer(a: str) -> str | None:
    a = a.strip()
    if not (15 <= len(a) <= 80):
        return f"len:{len(a)}"
    if any(ch in a for ch in "{}[]\"\\"):
        return "json_chars"
    if re.search(r"(?i)(ocr|screenshot|截图文件|作为AI|无法确定)", a):
        return "meta_leak"
    if HOLDOUT_PAT.search(a):
        return "holdout_leak"
    return None


def main() -> int:
    spec = {s["frame"]: s for s in json.loads((PATCH / "qa_spec.json").read_text(encoding="utf-8"))}
    ocr = json.loads((PATCH / "ocr_cache.json").read_text(encoding="utf-8"))
    recs, dropped = [], []
    seen = set()
    for frag in sorted((PATCH / "frags").glob("out_*.jsonl")):
        for line in frag.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                qa = json.loads(line)
            except json.JSONDecodeError:
                dropped.append((frag.name, "bad_json")); continue
            fr = qa.get("frame")
            qt = int(qa.get("qtype", 0))
            q, a = str(qa.get("question", "")).strip(), str(qa.get("answer", "")).strip()
            if fr not in spec or qt not in QTYPE_NAME or not q:
                dropped.append((frag.name, "missing_fields")); continue
            key = (fr, qt)
            if key in seen:
                dropped.append((frag.name, "dup")); continue
            seen.add(key)
            if HOLDOUT_PAT.search(q) or HOLDOUT_PAT.search(str(spec[fr].get("domain",""))) or HOLDOUT_PAT.search(str(spec[fr].get("page_url",""))):
                dropped.append((frag.name, "holdout_meta")); continue
            why = valid_answer(a)
            if why:
                dropped.append((frag.name, f"{why}:{a[:30]}")); continue
            if not (5 <= len(q) <= 60):
                dropped.append((frag.name, "q_len")); continue
            meta = spec[fr]
            ctx = (ocr.get(fr) or "")[:1000]
            inst = f"问题：{q}\n\n屏幕文字参考：\n{ctx}" if ctx else f"问题：{q}"
            recs.append({
                "sample_id": f"qa_{fr}_t{qt}",
                "split": "train",
                "image": f"images/{fr}.jpg",
                "source_type": "teacher_ask_screen",
                "kind": "screen_reader",
                "qtype": qt,
                "qtype_name": QTYPE_NAME[qt],
                "site": meta["site"],
                "domain": meta["domain"],
                "page_url": meta.get("page_url"),
                "system_prompt": PROMPT,
                "question": q,
                "ocr_context": ctx,
                "instruction": inst,
                "answer": a,
                "answer_len": len(a),
                "answer_chars": "zh",
                "teacher": "devin-vision",
                "verified": True,
            })
    with (PATCH / "records_qa.jsonl").open("w", encoding="utf-8") as fp:
        for r in recs:
            fp.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"kept {len(recs)}, dropped {len(dropped)}")
    from collections import Counter
    print("by qtype:", dict(Counter(r["qtype"] for r in recs)))
    print("drop reasons:", dict(Counter(d[1].split(":")[0] for d in dropped)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
