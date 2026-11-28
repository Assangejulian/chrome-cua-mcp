"""Clean, validate, split, and package M4 teacher dataset (teacher_big + teacher_m4_patch)."""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import random
import sys
import tarfile
from urllib.parse import urlsplit

from PIL import Image

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from m3_train_colab import SYSTEM_PROMPT, check_action, jsonl, write_json, write_jsonl

SCREEN_READER_PROMPT = (
    "You are an accurate visual screen reader. Answer the user's question directly and concisely "
    "based on the browser screenshot and OCR excerpt. Reply in plain Chinese or English text ONLY "
    "(do NOT output action JSON)."
)

EXCLUDED_DOMAIN_KEYWORDS = (
    # M3.5 Holdout 7 domains
    "wikipedia.org",
    "git-scm.com",
    "liaoxuefeng.com",
    "ruanyifeng.com",
    "react.dev",
    "vite.dev",
    "reddit.com",
    # M3.5 Dev domains
    "cn.bing.com",
    "element-plus.org",
    "rustwiki.org",
    "v2ex.com",
)

SEED = 42
SCROLL_TARGET_COUNT = 310
NOT_FOUND_CAP = 400


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_excluded(row: dict) -> bool:
    hay = " ".join(str(row.get(k, "") or "") for k in ("domain", "page_url", "url")).lower()
    return any(kw in hay for kw in EXCLUDED_DOMAIN_KEYWORDS)


def extract_domain(row: dict) -> str:
    dom = str(row.get("domain") or "").strip().lower()
    if dom:
        return dom
    url = str(row.get("page_url") or row.get("url") or "").strip()
    return (urlsplit(url).hostname or "unknown").lower()


def verify_image(path: Path, cache: dict[Path, tuple[str, str]]) -> tuple[str, str]:
    """Verify image readability via Pillow and return (sha256, suffix)."""
    if path in cache:
        return cache[path]
    with Image.open(path) as im:
        im.verify()
    digest = sha256_file(path)
    ext = path.suffix.lower() or ".jpg"
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise ValueError(f"Unsupported image extension: {path}")
    cache[path] = (digest, ext)
    return digest, ext


def collect_teacher_big(
    big_dir: Path,
    m35_val_hashes: set[str],
    img_cache: dict[Path, tuple[str, str]],
    rng: random.Random,
) -> tuple[list[dict], dict]:
    records_path = big_dir / "records.jsonl"
    stats = Counter()
    kept_direct: list[dict] = []
    scroll_pool: list[dict] = []
    not_found_offscreen: list[dict] = []
    not_found_absent: list[dict] = []

    for row in jsonl(records_path):
        stats["total_read"] += 1
        if is_excluded(row):
            stats["dropped_excluded_domain"] += 1
            continue
        if row.get("extended_action"):
            stats["dropped_extended_action"] += 1
            continue

        bucket = row.get("bucket")
        verified = bool(row.get("verified"))
        if bucket in {"web_click", "web_scroll_find"}:
            pass
        elif bucket in {"web_right_click", "web_type", "web_scroll", "web_not_found"}:
            if not verified:
                stats[f"dropped_unverified_{bucket}"] += 1
                continue
        else:
            stats[f"dropped_bucket_{bucket}"] += 1
            continue

        expected = row.get("expected")
        instruction = str(row.get("instruction") or "").strip()
        if not instruction:
            stats["dropped_empty_instruction"] += 1
            continue
        try:
            action_name = check_action(expected)
        except Exception:
            stats["dropped_invalid_action"] += 1
            continue

        img_rel = str(row.get("image") or "")
        img_path = big_dir / img_rel
        if not img_path.is_file():
            stats["dropped_missing_image"] += 1
            continue
        img_sha, ext = verify_image(img_path, img_cache)
        if img_sha in m35_val_hashes:
            stats["dropped_m35_val_hash"] += 1
            continue

        item = {
            "sample_id": f"m4tb_{row['sample_id']}",
            "source": "teacher_big",
            "bucket": bucket,
            "task_type": "gui_action",
            "action_label": action_name,
            "domain": extract_domain(row),
            "img_path": img_path,
            "img_sha": img_sha,
            "img_ext": ext,
            "system_prompt": SYSTEM_PROMPT,
            "user_content": f"<image>{instruction}",
            "assistant_content": json.dumps(expected, ensure_ascii=False, separators=(",", ":")),
        }

        if bucket == "web_scroll":
            scroll_pool.append(item)
        elif bucket == "web_not_found":
            if row.get("neg_subtype") == "offscreen_target":
                not_found_offscreen.append(item)
            else:
                not_found_absent.append(item)
        else:
            kept_direct.append(item)
            stats[f"kept_{bucket}"] += 1

    # Downsample web_scroll across domains via deterministic round-robin
    by_dom: dict[str, list[dict]] = defaultdict(list)
    for item in scroll_pool:
        by_dom[item["domain"]].append(item)
    dom_keys = sorted(by_dom)
    rng.shuffle(dom_keys)
    for dk in dom_keys:
        rng.shuffle(by_dom[dk])
    scroll_selected: list[dict] = []
    idx = 0
    while len(scroll_selected) < min(SCROLL_TARGET_COUNT, len(scroll_pool)):
        progressed = False
        for dk in dom_keys:
            bucket_list = by_dom[dk]
            if idx < len(bucket_list):
                scroll_selected.append(bucket_list[idx])
                progressed = True
                if len(scroll_selected) >= SCROLL_TARGET_COUNT:
                    break
        if not progressed:
            break
        idx += 1
    stats["kept_web_scroll"] = len(scroll_selected)
    stats["downsampled_web_scroll_dropped"] = len(scroll_pool) - len(scroll_selected)

    # Cap web_not_found: keep all verified offscreen_target + sample absent_label up to NOT_FOUND_CAP
    rng.shuffle(not_found_absent)
    need_absent = max(0, NOT_FOUND_CAP - len(not_found_offscreen))
    nf_selected = list(not_found_offscreen) + not_found_absent[:need_absent]
    stats["kept_web_not_found"] = len(nf_selected)
    stats["kept_web_not_found_offscreen"] = len(not_found_offscreen)
    stats["kept_web_not_found_absent"] = min(need_absent, len(not_found_absent))
    stats["capped_web_not_found_dropped"] = (len(not_found_offscreen) + len(not_found_absent)) - len(nf_selected)

    return kept_direct + scroll_selected + nf_selected, dict(stats)


def collect_patch_type(
    patch_dir: Path,
    m35_val_hashes: set[str],
    img_cache: dict[Path, tuple[str, str]],
) -> tuple[list[dict], dict]:
    records_path = patch_dir / "records.jsonl"
    stats = Counter()
    kept: list[dict] = []

    for row in jsonl(records_path):
        stats["total_read"] += 1
        if is_excluded(row):
            stats["dropped_excluded_domain"] += 1
            continue
        if row.get("extended_action"):
            stats["dropped_extended_action"] += 1
            continue
        if not row.get("verified"):
            stats["dropped_unverified"] += 1
            continue

        expected = row.get("expected")
        instruction = str(row.get("instruction") or "").strip()
        if not instruction:
            stats["dropped_empty_instruction"] += 1
            continue
        try:
            action_name = check_action(expected)
        except Exception:
            stats["dropped_invalid_action"] += 1
            continue

        img_rel = str(row.get("image") or "")
        img_path = patch_dir / img_rel
        if not img_path.is_file():
            stats["dropped_missing_image"] += 1
            continue
        img_sha, ext = verify_image(img_path, img_cache)
        if row.get("image_sha256") and img_sha != row["image_sha256"]:
            stats["dropped_sha_mismatch"] += 1
            continue
        if img_sha in m35_val_hashes:
            stats["dropped_m35_val_hash"] += 1
            continue

        kept.append(
            {
                "sample_id": f"m4tp_{row['sample_id']}",
                "source": "teacher_m4_patch_type",
                "bucket": row.get("bucket") or "web_type",
                "task_type": "gui_action",
                "action_label": action_name,
                "domain": extract_domain(row),
                "img_path": img_path,
                "img_sha": img_sha,
                "img_ext": ext,
                "system_prompt": SYSTEM_PROMPT,
                "user_content": f"<image>{instruction}",
                "assistant_content": json.dumps(expected, ensure_ascii=False, separators=(",", ":")),
            }
        )
        stats["kept"] += 1

    return kept, dict(stats)


def collect_patch_qa(
    patch_dir: Path,
    big_dir: Path,
    m35_val_hashes: set[str],
    img_cache: dict[Path, tuple[str, str]],
) -> tuple[list[dict], dict]:
    records_path = patch_dir / "records_qa.jsonl"
    stats = Counter()
    kept: list[dict] = []

    for row in jsonl(records_path):
        stats["total_read"] += 1
        if is_excluded(row):
            stats["dropped_excluded_domain"] += 1
            continue
        if row.get("system_prompt") != SCREEN_READER_PROMPT:
            stats["dropped_prompt_mismatch"] += 1
            continue

        instruction = str(row.get("instruction") or "").strip()
        answer = str(row.get("answer") or "").strip()
        if not instruction or not (10 <= len(answer) <= 120):
            stats["dropped_bad_text_or_len"] += 1
            continue
        if answer.startswith("{") or answer.startswith("```") or '"action"' in answer:
            stats["dropped_json_like_answer"] += 1
            continue

        img_rel = str(row.get("image") or "")
        patch_img = patch_dir / img_rel
        big_img = big_dir / img_rel
        if not patch_img.is_file() or not big_img.is_file():
            stats["dropped_missing_image"] += 1
            continue
        patch_sha, ext = verify_image(patch_img, img_cache)
        big_sha, _ = verify_image(big_img, img_cache)
        if patch_sha != big_sha:
            stats["dropped_filename_collision_overwritten"] += 1
            continue
        if patch_sha in m35_val_hashes:
            stats["dropped_m35_val_hash"] += 1
            continue

        kept.append(
            {
                "sample_id": f"m4qa_{row['sample_id']}",
                "source": "teacher_m4_patch_qa",
                "bucket": "ask_screen",
                "task_type": "ask_screen",
                "action_label": "ask_screen",
                "domain": extract_domain(row),
                "img_path": patch_img,
                "img_sha": patch_sha,
                "img_ext": ext,
                "system_prompt": SCREEN_READER_PROMPT,
                "user_content": f"<image>{instruction}",
                "assistant_content": answer,
            }
        )
        stats["kept"] += 1

    return kept, dict(stats)


def build_connected_components(items: list[dict]) -> list[list[dict]]:
    """Group items so no domain AND no image sha256 can ever cross train/val."""
    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_domain: dict[str, int] = {}
    by_sha: dict[str, int] = {}
    for i, item in enumerate(items):
        d = item["domain"]
        s = item["img_sha"]
        if d in by_domain:
            union(i, by_domain[d])
        else:
            by_domain[d] = i
        if s in by_sha:
            union(i, by_sha[s])
        else:
            by_sha[s] = i

    groups: dict[int, list[dict]] = defaultdict(list)
    for i, item in enumerate(items):
        groups[find(i)].append(item)
    return list(groups.values())


def split_train_val(items: list[dict], rng: random.Random) -> tuple[list[dict], list[dict]]:
    components = build_connected_components(items)
    # Identify components containing ask_screen QA vs action-only components
    qa_comps = []
    other_comps = []
    for comp in components:
        labels = {x["action_label"] for x in comp}
        if "ask_screen" in labels:
            qa_comps.append(comp)
        else:
            other_comps.append(comp)

    rng.shuffle(qa_comps)
    rng.shuffle(other_comps)

    # Pick ~11 QA components (prefer medium-sized ones that also carry diverse actions)
    qa_comps_sorted = sorted(qa_comps, key=lambda c: (len({x["action_label"] for x in c}), -len(c)), reverse=True)
    # Shuffle top candidates deterministically so split isn't biased to largest sites only
    qa_val_selected: list[list[dict]] = []
    qa_train_rest: list[list[dict]] = []
    # Keep ~10-12% of QA components (~11 of ~100+) for val
    target_qa_comps = max(10, min(12, round(len(qa_comps) * 0.10)))
    # Filter to components of reasonable size (<= 30 rows) so single huge domain doesn't dominate val
    elig_qa = [c for c in qa_comps if len(c) <= 35]
    inelig_qa = [c for c in qa_comps if len(c) > 35]
    qa_val_selected = elig_qa[:target_qa_comps]
    qa_train_rest = elig_qa[target_qa_comps:] + inelig_qa

    # Pick ~28 other components for val ensuring type / right_click / scroll / left_click / not_found coverage
    target_other_comps = max(25, min(32, round(len(other_comps) * 0.085)))
    elig_other = [c for c in other_comps if len(c) <= 30]
    inelig_other = [c for c in other_comps if len(c) > 30]

    # Greedy diversity pick first for any missing action types, then random from elig_other
    required_actions = {"ask_screen", "left_click", "scroll", "right_click", "type", "not_found"}
    val_comps = list(qa_val_selected)
    present_actions = {x["action_label"] for c in val_comps for x in c}
    remaining_other = []
    for c in elig_other:
        c_acts = {x["action_label"] for x in c}
        if len(val_comps) < len(qa_val_selected) + target_other_comps and (c_acts - present_actions):
            val_comps.append(c)
            present_actions |= c_acts
        else:
            remaining_other.append(c)

    need_more = (len(qa_val_selected) + target_other_comps) - len(val_comps)
    if need_more > 0:
        val_comps.extend(remaining_other[:need_more])
        remaining_other = remaining_other[need_more:]

    train_comps = qa_train_rest + remaining_other + inelig_other
    train_items = [x for c in train_comps for x in c]
    val_items = [x for c in val_comps for x in c]

    assert required_actions.issubset({x["action_label"] for x in val_items}), (
        f"Validation missing required labels: {required_actions - {x['action_label'] for x in val_items}}"
    )
    assert required_actions.issubset({x["action_label"] for x in train_items}), (
        f"Train missing required labels: {required_actions - {x['action_label'] for x in train_items}}"
    )
    return train_items, val_items


def to_sft_row(item: dict, split_name: str) -> dict:
    rel_img = f"images/{item['img_sha'][:16]}{item['img_ext']}"
    return {
        "sample_id": item["sample_id"],
        "split": split_name,
        "task_type": item["task_type"],
        "messages": [
            {"role": "system", "content": item["system_prompt"]},
            {"role": "user", "content": item["user_content"]},
            {"role": "assistant", "content": item["assistant_content"]},
        ],
        "images": [rel_img],
    }


def main() -> None:
    rng = random.Random(SEED)
    big_dir = PROJECT_ROOT / "artifacts" / "teacher_big"
    patch_dir = PROJECT_ROOT / "artifacts" / "teacher_m4_patch"
    out_dir = PROJECT_ROOT / "artifacts" / "m4_bundle_20261125"
    out_dir.mkdir(parents=True, exist_ok=True)

    m35_receipt = json.loads((PROJECT_ROOT / "models" / "m35_gguf" / "data_receipt.json").read_text(encoding="utf-8"))
    m35_val_hashes = set(m35_receipt["validation_hashes"])

    img_cache: dict[Path, tuple[str, str]] = {}
    items_big, stats_big = collect_teacher_big(big_dir, m35_val_hashes, img_cache, rng)
    items_tp, stats_tp = collect_patch_type(patch_dir, m35_val_hashes, img_cache)
    items_qa, stats_qa = collect_patch_qa(patch_dir, big_dir, m35_val_hashes, img_cache)

    all_items = items_big + items_tp + items_qa
    # Deduplicate exact (img_sha, system_prompt, user_content) before splitting
    deduped: list[dict] = []
    seen_keys: dict[tuple[str, str, str], str] = {}
    conflicting = 0
    repeated = 0
    for item in all_items:
        key = (item["img_sha"], item["system_prompt"], item["user_content"])
        if key in seen_keys:
            if seen_keys[key] != item["assistant_content"]:
                conflicting += 1
            else:
                repeated += 1
            continue
        seen_keys[key] = item["assistant_content"]
        deduped.append(item)

    train_items, val_items = split_train_val(deduped, rng)
    rng.shuffle(train_items)
    rng.shuffle(val_items)

    train_hashes = {x["img_sha"] for x in train_items}
    val_hashes = {x["img_sha"] for x in val_items}
    assert not (train_hashes & val_hashes), "Train/val image SHA-256 overlap!"
    assert not ((train_hashes | val_hashes) & m35_val_hashes), "M3.5 validation hash leak!"

    train_domains = {x["domain"] for x in train_items}
    val_domains = {x["domain"] for x in val_items}
    assert not (train_domains & val_domains), "Train/val domain overlap!"

    train_rows = [to_sft_row(x, "train") for x in train_items]
    val_rows = [to_sft_row(x, "val") for x in val_items]

    train_jsonl = out_dir / "train_m4_teacher.jsonl"
    val_jsonl = out_dir / "val_m4_teacher.jsonl"
    write_jsonl(train_jsonl, train_rows)
    write_jsonl(val_jsonl, val_rows)

    unique_imgs: dict[str, Path] = {}
    for x in train_items + val_items:
        rel = f"images/{x['img_sha'][:16]}{x['img_ext']}"
        unique_imgs.setdefault(rel, x["img_path"])

    summary_payload = {
        "seed": SEED,
        "excluded_domain_keywords": list(EXCLUDED_DOMAIN_KEYWORDS),
        "sources": {
            "teacher_big": stats_big,
            "teacher_m4_patch_type": stats_tp,
            "teacher_m4_patch_qa": stats_qa,
        },
        "pre_split_dedup": {
            "input_count": len(all_items),
            "kept_count": len(deduped),
            "repeated_dropped": repeated,
            "conflicting_dropped": conflicting,
        },
        "train_count": len(train_rows),
        "val_count": len(val_rows),
        "unique_images": len(unique_imgs),
        "train_domains": len(train_domains),
        "val_domains": len(val_domains),
        "train_by_action": dict(Counter(x["action_label"] for x in train_items)),
        "val_by_action": dict(Counter(x["action_label"] for x in val_items)),
        "train_by_task_type": dict(Counter(x["task_type"] for x in train_items)),
        "val_by_task_type": dict(Counter(x["task_type"] for x in val_items)),
        "leak_checks": {
            "holdout_leaks": 0,
            "m35_dev_leaks": 0,
            "m35_val_hash_overlaps": 0,
            "train_val_hash_overlaps": 0,
            "train_val_domain_overlaps": 0,
        },
    }

    summary_path = out_dir / "bundle_summary.json"
    write_json(summary_path, summary_payload)

    tar_path = out_dir / "m4_teacher_bundle.tar"
    with tarfile.open(tar_path, "w") as tf:
        tf.add(train_jsonl, arcname="train_m4_teacher.jsonl")
        tf.add(val_jsonl, arcname="val_m4_teacher.jsonl")
        tf.add(summary_path, arcname="bundle_summary.json")
        for rel, src in sorted(unique_imgs.items()):
            tf.add(src, arcname=rel)

    summary_payload["archive"] = {
        "filename": tar_path.name,
        "bytes": tar_path.stat().st_size,
        "sha256": sha256_file(tar_path),
    }
    write_json(summary_path, summary_payload)
    print(json.dumps(summary_payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
