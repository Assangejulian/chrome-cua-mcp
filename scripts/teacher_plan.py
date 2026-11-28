"""Draft plan.jsonl for teacher_collect run from a survey.jsonl.

Rules per site (up to `max_tasks`):
  - click      : nav-ish visible link (short label, near top) or content link
  - type       : first plausible input (search box etc.)
  - scroll_click: off-screen link (inview=false) 4-24 chars -> two-sample chain
  - right_click: a longer content link (8-30 chars)
  - checkbox   : inview checkable
  - not_found  : a label absent from candidates (pool, category-aware)
  - not_found_offscreen: a label that exists but is below the fold

Output goes to stdout review; rerun with --write to emit plan_draft.jsonl.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

NAVY = {"news", "portal", "gov", "university", "edu", "company", "finance"}
SHOP = {"ecommerce", "life", "mom"}
MEDIA = {"video", "tv", "music", "game", "sports"}

TYPE_TEXT = {
    "music": "周杰伦", "video": "热门电影", "tv": "综艺节目", "ecommerce": "手机",
    "news": "今日要闻", "portal": "今日要闻", "reading": "科幻小说",
    "travel": "北京旅游攻略", "jobs": "前端工程师", "realestate": "二手房",
    "health": "感冒发烧怎么办", "auto": "新能源汽车", "food": "火锅做法",
    "game": "最新网游", "sports": "足球直播", "edu": "考研资料",
    "dev": "Python教程", "download": "办公软件",
}
DEFAULT_TYPE_TEXT = "最新资讯"

ABSENT_POOL = [
    "会员充值", "购物车", "话费充值", "在线客服", "优惠券领取", "积分商城",
    "签到打卡", "企业采购", "广告投放", "帮助中心",
]
# labels that plausibly exist -> not safe as negatives per category hint
RISKY_LABELS = {"购物车", "在线客服", "帮助中心", "优惠券领取", "积分商城", "签到打卡"}


def short_label(s: str, lo: int = 2, hi: int = 10) -> bool:
    return lo <= len(s) <= hi


def pick_tasks(rec: dict, excluded: set[str] | None = None) -> list[dict]:
    """Dense task mix (~11/site): click, right_click x2, scroll_click x3 chains,
    type, checkbox, scrolls, 2 negatives + offscreen negative.
    `excluded` = labels already collected for this site in round 1."""
    excluded = excluded or set()
    cat = rec.get("category", "")
    cands = rec.get("candidates", [])
    inview = [c for c in cands if c.get("inview", True)]
    offview = [c for c in cands if not c.get("inview", True)]
    links = [c for c in inview if c["tag"] == "a" and c.get("label")]
    inputs = [c for c in inview if c.get("input")]
    checks = [c for c in inview if c.get("checkable")]
    tasks: list[dict] = []
    used: set[str] = set(excluded)

    def take(c: dict) -> dict:
        used.add(c["label"])
        return c

    SKIP_WORDS = ("首页", "下载", "APP", "客户端", "登录", "注册", "设为", "关闭",
                  "无障碍", "关怀版", "ICP", "举报", "隐私", "协议", "反馈")
    nav = [c for c in links if short_label(c["label"], 2, 8) and c["rect"][1] < 280
           and c["label"] not in used
           and not any(w in c["label"] for w in SKIP_WORDS)]
    content = [c for c in links if 4 <= len(c["label"]) <= 24 and c["label"] not in used
               and c["rect"][1] >= 200]
    seed = int(rec.get("id", 0))

    # ordering: non-navigating first (type, scrolls, negatives, right_click),
    # chains next, plain nav click last — runner re-navs anyway but this raises yield
    # 1. type into input
    if inputs:
        c = inputs[seed % len(inputs)]
        if c["label"] not in used:
            take(c)
            lbl = c["label"]
            searchy = any(w in lbl for w in ("搜索", "搜", "Search", "search", "查询", "关键字"))                 or lbl.lower() in ("q", "wd", "kw", "srch", "keyword", "query")
            texts = list(TYPE_TEXT.values()) or [DEFAULT_TYPE_TEXT]
            tasks.append({"kind": "type", "match": lbl,
                          "label": lbl if (searchy and 2 <= len(lbl) <= 10) else "搜索",
                          "text": TYPE_TEXT.get(cat, texts[seed % len(texts)])})

    # 2. plain scrolls: one down, one up
    tasks.append({"kind": "scroll", "direction": "down", "amount": 3 + seed % 3, "topic": "页面"})
    if seed % 2 == 0:
        tasks.append({"kind": "scroll", "direction": "up", "amount": 2, "topic": "页面"})

    # 3. negatives: two distinct absent labels
    all_labels = {c["label"] for c in cands}
    for i in range(len(ABSENT_POOL)):
        lbl = ABSENT_POOL[(seed + i * 3) % len(ABSENT_POOL)]
        if lbl in RISKY_LABELS and cat in SHOP:
            continue
        if lbl in used:
            continue
        if not any(lbl in L or L in lbl for L in all_labels):
            tasks.append({"kind": "not_found", "label": lbl})
            used.add(lbl)
            break

    # 4. right_click x2 on longer content links (spread picks)
    rl = [c for c in content if 8 <= len(c["label"]) <= 30 and c["label"] not in used]
    for pick in sorted({0, len(rl) // 3, (2 * len(rl)) // 3, seed % max(len(rl), 1)}):
        if len(rl) > pick and rl[pick]["label"] not in used:
            c = take(rl[pick])
            tasks.append({"kind": "right_click", "match": c["label"], "label": c["label"]})
        if sum(1 for t in tasks if t["kind"] == "right_click") >= 3:
            break

    # 5. checkbox
    if checks:
        c = take(checks[0])
        tasks.append({"kind": "checkbox", "match": c["label"], "label": c["label"][:12] or "选项"})

    # 6. scroll_click chains x3 — distinct offscreen targets spread across the page
    off_links = [c for c in offview if c["tag"] == "a" and 4 <= len(c["label"]) <= 24
                 and c["label"] not in used]
    picks = []
    if off_links:
        idxs = sorted({len(off_links) // 5, len(off_links) // 3, len(off_links) // 2,
                       (2 * len(off_links)) // 3, (4 * len(off_links)) // 5,
                       seed % len(off_links)})
        picks = [off_links[i] for i in idxs if i < len(off_links)]
    for c in picks[:5]:
        take(c)
        tasks.append({"kind": "scroll_click", "match": c["label"], "label": c["label"],
                      "amount": 3 + (seed % 3)})

    # 7. not_found_offscreen
    off_labels = sorted({c["label"] for c in offview if c["tag"] == "a" and len(c["label"]) >= 4}
                        - used)
    if off_labels:
        lbl = off_labels[seed % len(off_labels)]
        tasks.append({"kind": "not_found_offscreen", "match": lbl, "label": lbl})

    # 8. nav click LAST (most likely to navigate away)
    pool = nav if nav else content
    for pick in sorted({seed % len(pool), (seed + len(pool) // 2) % len(pool)}) if pool else []:
        if pool[pick]["label"] in used:
            continue
        c = take(pool[pick])
        tasks.append({"kind": "click", "match": c["label"], "label": c["label"]})

    return tasks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--survey", required=True)
    ap.add_argument("--write", default="")
    ap.add_argument("--max-per-site", type=int, default=12)
    ap.add_argument("--exclude", default="",
                    help="records.jsonl of earlier rounds — skip already-collected labels per site")
    args = ap.parse_args()

    excl: dict[str, set[str]] = {}
    if args.exclude and Path(args.exclude).exists():
        for line in Path(args.exclude).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            desc = str(r.get("target_desc") or "")
            lbl = desc.split(":", 1)[1] if ":" in desc else desc
            if lbl:
                excl.setdefault(str(r.get("site")), set()).add(lbl)

    out_items = []
    for line in Path(args.survey).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if not rec.get("shot") or not rec.get("candidates"):
            continue
        tasks = pick_tasks(rec, excl.get(str(rec.get("site")), set()))
        tasks = tasks[: args.max_per_site]
        if tasks:
            out_items.append({"id": rec["id"], "site": rec["site"], "url": rec["url"],
                              "category": rec.get("category"), "tasks": tasks})
    n_tasks = sum(len(i["tasks"]) for i in out_items)
    print(f"sites={len(out_items)} tasks={n_tasks}", file=sys.stderr)
    if args.write:
        Path(args.write).write_text(
            "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in out_items),
            encoding="utf-8")
        print(f"written -> {args.write}", file=sys.stderr)
    else:
        for i in out_items[:10]:
            print(json.dumps(i, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
