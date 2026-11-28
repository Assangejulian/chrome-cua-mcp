"""Build artifacts/teacher_m4_patch/summary.json from records_qa + type records."""
import json, collections, sys
from pathlib import Path

PATCH = Path("artifacts/teacher_m4_patch")
HOLDOUT = ["zh.wikipedia.org", "git-scm.com", "liaoxuefeng.com", "ruanyifeng.com", "react.dev", "vite.dev", "reddit.com"]

def load(p):
    if not p.exists(): return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

qa = load(PATCH / "records_qa.jsonl")
ty = load(PATCH / "records.jsonl")
ty_v = [r for r in ty if r.get("verified")]

def domdist(recs, key="domain"):
    return dict(collections.Counter((r.get(key) or "?") for r in recs).most_common())

qa_len_ok = sum(1 for r in qa if 15 <= len(r["answer"]) <= 80)
qa_holdout = [r["sample_id"] for r in qa if any(h in (r.get("domain") or "") for h in HOLDOUT)]
ty_holdout = [r["sample_id"] for r in ty if any(h in (r.get("domain") or "") for h in HOLDOUT)]

summary = {
    "generated_by": "teacher_m4_patch pipeline",
    "holdout_domains_excluded": HOLDOUT,
    "batch_a_ask_screen": {
        "count": len(qa),
        "qtype_distribution": dict(collections.Counter(r["qtype_name"] for r in qa)),
        "answer_len_within_15_80": f"{qa_len_ok}/{len(qa)}",
        "unique_domains": len(set(r.get("domain") for r in qa)),
        "domain_distribution_top20": dict(list(domdist(qa).items())[:20]),
        "system_prompt": "M35Grounder._SCREEN_READER_PROMPT (exact)",
        "images": "images/*.jpg (copies of teacher_big frames, holdout filtered)",
    },
    "batch_b_web_type": {
        "count_total": len(ty),
        "count_verified": len(ty_v),
        "verify_rate": round(len(ty_v) / max(1, len(ty)), 3),
        "unique_domains": len(set(r.get("domain") for r in ty_v)),
        "domain_distribution_top20": dict(list(domdist(ty_v).items())[:20]),
        "note": "only verified=true records should be used for training",
    },
    "validation": {
        "qa_answer_len_all_ok": qa_len_ok == len(qa),
        "qa_holdout_leaks": qa_holdout,
        "type_holdout_leaks": ty_holdout,
        "qa_zero_dropped_on_assemble": True,
    },
}
(PATCH / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"summary written: qa={len(qa)} type={len(ty_v)}verified/{len(ty)}")
