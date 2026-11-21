"""Build and validate the Colab Free-CPU Option A data preparation pipeline for M3."""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import random
import re
import tarfile
import zipfile

from PIL import Image

SYSTEM_PROMPT = (
    "You are a Chrome browser GUI action assistant. Given a Chrome window screenshot and a user instruction, "
    "output strictly one JSON object for the next action using [0, 1000] normalized integer coordinates. "
    "Do not output explanations or markdown."
)

SATRAJ_REPO = "AI45Research/SATraj-OS"
SATRAJ_REV = "74deb768ee16733b952b789e91b69e7450df5dea"
VENUS_REPO = "inclusionAI/VenusBench-GD"
VENUS_REV = "7e5714566f912203c9989b06ef4c08bdf913e3fa"

MAX_IMAGE_TOKENS = 1024
GRID = 32
MAX_PIXELS = MAX_IMAGE_TOKENS * GRID * GRID

GENERIC_NEG_TERMS = {
    "button",
    "link",
    "icon",
    "section",
    "text",
    "image",
    "input",
    "search",
    "menu",
    "close",
    "back",
    "home",
    "next",
    "submit",
    "ok",
    "cancel",
    "login",
    "sign in",
    "settings",
    "help",
}

ZH_PARAPHRASE_TEMPLATES = {
    "left_click": [
        "请帮我点击界面中的「{target}」按钮。",
        "帮我点一下「{target}」。",
        "定位并左键点击「{target}」按钮。",
        "下一步操作：点击「{target}」按钮。",
    ],
    "type": [
        "请在「{label}」输入框里填入「{text}」，无需提交。",
        "帮我定位「{label}」并输入文字：{text}。",
        "点击「{label}」并键入「{text}」。",
        "把「{text}」输入到「{label}」框中。",
    ],
    "scroll": [
        "请将「{topic}」区域向{dir_zh}滚动 {amount} 格。",
        "在「{topic}」列表处向{dir_zh}滑动 {amount} 格。",
        "帮我把「{topic}」内容向{dir_zh}滚动 {amount} 次。",
        "请对「{topic}」执行向{dir_zh}滚动（幅度 {amount}）。",
    ],
    "not_found": [
        "请帮我点击「{target}」按钮。",
        "定位并点击界面上的「{target}」。",
        "在当前页面找到并点击「{target}」按钮。",
        "帮我点一下「{target}」。",
    ],
}


def resize_for_qwen35(img: Image.Image) -> Image.Image:
    """Resize RGB image to stay within 1024 visual tokens aligned to 32px grid."""
    w, h = img.size
    if w * h > MAX_PIXELS:
        scale = (MAX_PIXELS / (w * h)) ** 0.5
        w = max(GRID, int(w * scale))
        h = max(GRID, int(h * scale))
    new_w = max(GRID, round(w / GRID) * GRID)
    new_h = max(GRID, round(h / GRID) * GRID)
    while new_w * new_h > MAX_PIXELS and new_w > GRID and new_h > GRID:
        if new_w >= new_h:
            new_w -= GRID
        else:
            new_h -= GRID
    if (new_w, new_h) != img.size:
        return img.resize((new_w, new_h), Image.Resampling.BICUBIC)
    return img


def make_sft_record(sample_id: str, rel_img: str, instruction: str, assistant_obj: dict, split: str | None = None) -> dict:
    """Build a strict single-image SFT JSONL record matching m3_train_colab.load_sft."""
    row = {
        "sample_id": sample_id,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"<image>{instruction}"},
            {"role": "assistant", "content": json.dumps(assistant_obj, ensure_ascii=False, separators=(",", ":"))},
        ],
        "images": [rel_img],
    }
    if split is not None:
        row["split"] = split
    return row


def clamp_norm_coord(pair: list[float] | tuple[float, float]) -> list[int]:
    x = min(max(int(round(float(pair[0]))), 0), 1000)
    y = min(max(int(round(float(pair[1]))), 0), 1000)
    return [x, y]


def parse_satraj_tool_calls(content: str) -> tuple[str, list[dict[str, str]]]:
    """Extract leading step description and list of parsed XML <tool_call> parameter dicts."""
    desc = content.split("<tool_call>", 1)[0].strip()
    if desc.startswith("Action:"):
        desc = desc[len("Action:"):].strip()
    calls = []
    for block in re.findall(r"<tool_call>(.*?)</tool_call>", content, re.S):
        params = {
            k.strip(): v.strip()
            for k, v in re.findall(r"<parameter=([^>]+)>(.*?)</parameter>", block, re.S)
        }
        if params:
            calls.append(params)
    return desc, calls


def satraj_step_to_action(desc: str, calls: list[dict[str, str]], task_desc: str) -> tuple[str, dict] | None:
    """Map one SATraj Chrome assistant turn into (instruction, closed_action_json) or None."""
    if not calls:
        return None
    acts = [c.get("action", "") for c in calls]

    if acts == ["mouse_move", "scroll"]:
        coord = json.loads(calls[0]["coordinate"])
        pix = int(round(float(calls[1].get("pixels", "-3"))))
        if pix == 0:
            return None
        direction = "down" if pix < 0 else "up"
        amount = min(max(abs(pix), 1), 20)
        if not desc:
            desc = f"Scroll {direction} on the Chrome page."
        return desc, {"action": "scroll", "coordinate": clamp_norm_coord(coord), "direction": direction, "amount": amount}

    if len(calls) != 1:
        return None

    c = calls[0]
    act = c.get("action", "")

    if act in {"left_click", "right_click", "double_click", "mouse_move"}:
        coord = json.loads(c["coordinate"])
        mapped = "move" if act == "mouse_move" else act
        if not desc:
            return None
        return desc, {"action": mapped, "coordinate": clamp_norm_coord(coord)}

    if act == "scroll":
        pix = int(round(float(c.get("pixels", "-3"))))
        if pix == 0:
            return None
        direction = "down" if pix < 0 else "up"
        amount = min(max(abs(pix), 1), 20)
        if not desc:
            desc = f"Scroll {direction} on the Chrome page."
        return desc, {"action": "scroll", "coordinate": [500, 500], "direction": direction, "amount": amount}

    if act == "type":
        text = c.get("text", "")
        if not text or not desc:
            return None
        return desc, {"action": "type", "text": text}

    if act == "key":
        raw_keys = json.loads(c.get("keys", "[]"))
        if not isinstance(raw_keys, list) or not raw_keys or not desc:
            return None
        norm_keys = ["enter" if str(k).strip().lower() == "return" else str(k).strip().lower() for k in raw_keys]
        if any(not k for k in norm_keys):
            return None
        return desc, {"action": "hotkey", "keys": norm_keys}

    if act == "wait":
        sec = float(c.get("time", "1.0"))
        sec = round(min(max(sec, 0.1), 10.0), 2)
        if not desc:
            desc = "Wait briefly for the Chrome page to finish loading."
        return desc, {"action": "wait", "seconds": sec}

    if act == "terminate":
        if c.get("status", "").lower() != "success" or not task_desc:
            return None
        inst = f"Task: {task_desc}\nInspect the current Chrome window and decide if the task is already finished."
        return inst, {"action": "done"}

    return None


def convert_satraj_chrome(
    traj_jsonl: Path,
    images_zip: Path,
    out_dir: Path,
    seed: int = 42,
    val_ratio: float = 0.10,
) -> dict:
    """Convert SATraj-OS Capability/chrome trajectories into leak-free train/val SFT splits."""
    img_out = out_dir / "images"
    img_out.mkdir(parents=True, exist_ok=True)

    trajs = []
    with open(traj_jsonl, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                trajs.append(json.loads(line))

    pending_by_tid: dict[str, list[dict]] = defaultdict(list)
    seen_img_info: dict[str, tuple[str, str]] = {}  # img_url -> (out_rel, img_sha256)
    img_to_tids: dict[str, set[str]] = defaultdict(set)
    stats = Counter()

    with zipfile.ZipFile(images_zip, "r") as zf:
        zf_names = set(zf.namelist())
        for traj in trajs:
            tid = traj["trajectory_id"]
            task_desc = str((traj.get("task") or {}).get("description") or "").strip()
            msgs = traj.get("messages", [])
            step_idx = 0
            for i in range(len(msgs) - 1):
                u_msg, a_msg = msgs[i], msgs[i + 1]
                if u_msg.get("role") != "user" or a_msg.get("role") != "assistant":
                    continue
                img_url = None
                for part in u_msg.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        img_url = (part.get("image_url") or {}).get("url")
                if not img_url or img_url not in zf_names:
                    stats["skip_missing_img"] += 1
                    continue
                desc, calls = parse_satraj_tool_calls(a_msg.get("content", ""))
                converted = satraj_step_to_action(desc, calls, task_desc)
                if converted is None:
                    stats["skip_unsupported_action"] += 1
                    continue
                inst, action_obj = converted

                if img_url not in seen_img_info:
                    with zf.open(img_url) as zimg:
                        with Image.open(zimg) as raw_im:
                            rgb = raw_im.convert("RGB")
                            resized = resize_for_qwen35(rgb)
                    buf = io.BytesIO()
                    resized.save(buf, format="JPEG", quality=92)
                    jpg_bytes = buf.getvalue()
                    img_sha = hashlib.sha256(jpg_bytes).hexdigest()
                    out_rel = f"images/satraj_{img_sha[:10]}_{tid[:8]}_{PurePosixPath(img_url).stem}.jpg"
                    (out_dir / out_rel).write_bytes(jpg_bytes)
                    seen_img_info[img_url] = (out_rel, img_sha)

                out_rel, img_sha = seen_img_info[img_url]
                img_to_tids[img_sha].add(tid)
                step_idx += 1
                pending_by_tid[tid].append({
                    "sample_id": f"satraj_chrome_{tid[:12]}_{step_idx:03d}",
                    "out_rel": out_rel,
                    "img_sha": img_sha,
                    "inst": inst,
                    "action_obj": action_obj,
                })

    # Union-find components so trajectories sharing ANY image hash stay in the same split
    parent = {tid: tid for tid in pending_by_tid}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for shared_tids in img_to_tids.values():
        ordered = sorted(shared_tids)
        root0 = find(ordered[0])
        for other in ordered[1:]:
            parent[find(other)] = root0

    components: dict[str, list[str]] = defaultdict(list)
    for tid in sorted(pending_by_tid):
        components[find(tid)].append(tid)

    comp_list = [components[k] for k in sorted(components)]
    rng = random.Random(seed)
    rng.shuffle(comp_list)
    n_val_comps = max(1, round(len(comp_list) * val_ratio)) if len(comp_list) > 1 else 0
    val_traj_set = {tid for comp in comp_list[:n_val_comps] for tid in comp}

    splits: dict[str, list[dict]] = {"train": [], "val": []}
    meta_rows: list[dict] = []
    for tid in sorted(pending_by_tid):
        split = "val" if tid in val_traj_set else "train"
        for item in pending_by_tid[tid]:
            splits[split].append(make_sft_record(item["sample_id"], item["out_rel"], item["inst"], item["action_obj"]))
            meta_rows.append({
                "sample_id": item["sample_id"],
                "split": split,
                "trajectory_id": tid,
                "image_path": item["out_rel"],
                "action": item["action_obj"]["action"],
                "instruction": item["inst"],
            })
            stats[f"{split}_{item['action_obj']['action']}"] += 1

    write_jsonl(out_dir / "train_satraj_chrome.jsonl", splits["train"])
    write_jsonl(out_dir / "val_satraj_chrome.jsonl", splits["val"])
    write_jsonl(out_dir / "metadata_satraj_chrome.jsonl", meta_rows)
    return {"train_count": len(splits["train"]), "val_count": len(splits["val"]), "stats": dict(stats)}


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def pack_tar(src_dir: Path, tar_path: Path) -> int:
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "w") as tar:
        tar.add(src_dir, arcname=".")
    return tar_path.stat().st_size


def build_zh_fixture_boost(fixture_zip: Path, out_dir: Path) -> dict:
    """Create diverse Chinese prompt paraphrases for zh_fixture_train_60 without touching HOLDOUT."""
    img_out = out_dir / "images"
    img_out.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    meta_rows: list[dict] = []

    with zipfile.ZipFile(fixture_zip, "r") as zf:
        cap = json.loads(zf.read("capture.json").decode("utf-8"))
        if cap.get("purpose") != "TRAIN_ONLY":
            raise ValueError("Refusing to build boost from non-TRAIN_ONLY capture zip")
        for member in zf.namelist():
            if member.startswith("images/") and member.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                rel = member
                target = out_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(member))

        train_lines = zf.read("train_chrome.jsonl").decode("utf-8").splitlines()
        for line in train_lines:
            if not line.strip():
                continue
            orig = json.loads(line)
            sid = orig["sample_id"]
            rel_img = orig["images"][0]
            orig_inst = orig["messages"][1]["content"].removeprefix("<image>")
            ans = json.loads(orig["messages"][2]["content"])
            act = ans["action"]

            paraphrases: list[str] = []
            if act == "left_click":
                m = re.match(r"点击「(.+?)」按钮。", orig_inst)
                if m:
                    paraphrases = [t.format(target=m.group(1)) for t in ZH_PARAPHRASE_TEMPLATES["left_click"]]
            elif act == "type":
                m = re.match(r"在「(.+?)」输入「(.+?)」，不要提交。", orig_inst)
                if m:
                    paraphrases = [t.format(label=m.group(1), text=m.group(2)) for t in ZH_PARAPHRASE_TEMPLATES["type"]]
            elif act == "scroll":
                m = re.match(r"把「(.+?)」列表向(上|下)滚动\s+(\d+)\s+格。", orig_inst)
                if m:
                    paraphrases = [
                        t.format(topic=m.group(1), dir_zh=m.group(2), amount=m.group(3))
                        for t in ZH_PARAPHRASE_TEMPLATES["scroll"]
                    ]
            elif act == "not_found":
                m = re.match(r"点击「(.+?)」按钮。", orig_inst)
                if m:
                    paraphrases = [t.format(target=m.group(1)) for t in ZH_PARAPHRASE_TEMPLATES["not_found"]]

            for p_idx, p_inst in enumerate(paraphrases, 1):
                if p_inst == orig_inst:
                    continue
                new_sid = f"{sid}_para_{p_idx}"
                rows.append(make_sft_record(new_sid, rel_img, p_inst, ans))
                meta_rows.append({"sample_id": new_sid, "base_id": sid, "action": act, "image_path": rel_img})

    write_jsonl(out_dir / "train_zh_boost.jsonl", rows)
    write_jsonl(out_dir / "metadata_zh_boost.jsonl", meta_rows)
    return {"count": len(rows), "actions": dict(Counter(m["action"] for m in meta_rows))}


def _extract_zh_quoted_terms(text: str) -> list[str]:
    return re.findall(r"「([^」]+)」", text)


def build_hard_negatives(
    m1_work_dir: Path,
    fixture_zip: Path,
    out_dir: Path,
    seed: int = 42,
    m1_neg_target: int = 560,
    zh_negs_per_img: int = 4,
) -> dict:
    """Synthesize verified train-only hard negatives (disjoint Chinese Chrome + cross-domain M1 train)."""
    img_out = out_dir / "images"
    img_out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    rows: list[dict] = []
    meta_rows: list[dict] = []

    # Collect val/ss hashes from M1 strictly to prevent ANY image leakage into train hard negatives
    val_img_paths: set[str] = set()
    for eval_file in ("val_open_web.jsonl", "val_screenspot_v2.jsonl"):
        ep = m1_work_dir / eval_file
        if ep.is_file():
            with open(ep, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        val_img_paths.update(json.loads(line)["images"])

    val_sha256: set[str] = set()
    for rel in val_img_paths:
        p = m1_work_dir / rel
        if p.is_file():
            val_sha256.add(hashlib.sha256(p.read_bytes()).hexdigest())

    # Part 1: Mathematically verified disjoint-family Chinese 125% DPI Chrome negatives (train_60 only)
    with zipfile.ZipFile(fixture_zip, "r") as zf:
        cap = json.loads(zf.read("capture.json").decode("utf-8"))
        if cap.get("purpose") != "TRAIN_ONLY":
            raise ValueError("Refusing to synthesize negatives from non-TRAIN_ONLY zip")
        train_rows = [json.loads(ln) for ln in zf.read("train_chrome.jsonl").decode("utf-8").splitlines() if ln.strip()]

        family_terms: dict[str, set[str]] = defaultdict(set)
        family_pool: dict[str, list[str]] = defaultdict(list)
        for r in train_rows:
            fam = r["sample_id"].split("-")[1]
            inst = r["messages"][1]["content"].removeprefix("<image>")
            ans = json.loads(r["messages"][2]["content"])
            for term in _extract_zh_quoted_terms(inst):
                family_terms[fam].add(term)
            if ans["action"] != "not_found":
                family_pool[fam].append(inst)

        zh_neg_count = 0
        all_families = sorted(family_pool.keys())
        for r in train_rows:
            fam = r["sample_id"].split("-")[1]
            rel = r["images"][0]
            img_bytes = zf.read(rel)
            if hashlib.sha256(img_bytes).hexdigest() in val_sha256:
                continue
            dest = out_dir / rel
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(img_bytes)

            other_insts = []
            for other_fam in all_families:
                if other_fam == fam:
                    continue
                for cand in family_pool[other_fam]:
                    terms = _extract_zh_quoted_terms(cand)
                    if terms and not any(t in family_terms[fam] for t in terms):
                        other_insts.append((other_fam, cand))

            rng.shuffle(other_insts)
            picked = []
            seen_inst = {r["messages"][1]["content"].removeprefix("<image>")}
            for other_fam, cand in other_insts:
                if cand in seen_inst:
                    continue
                seen_inst.add(cand)
                picked.append((other_fam, cand))
                if len(picked) >= zh_negs_per_img:
                    break

            for idx, (other_fam, cand) in enumerate(picked, 1):
                sid = f"hardneg_{r['sample_id']}_{idx}"
                ans = {"action": "not_found", "reason": "Requested Chinese control is absent from this page"}
                rows.append(make_sft_record(sid, rel, cand, ans))
                meta_rows.append({"sample_id": sid, "source": "zh_fixture_disjoint", "image_path": rel, "other_family": other_fam})
                zh_neg_count += 1

    # Part 2: Cross-domain specific-text M1 train negatives (never touching val images or generic labels)
    m1_train_ids = set()
    with open(m1_work_dir / "train_open_web.jsonl", "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                m1_train_ids.add(json.loads(line)["sample_id"])

    m1_imgs: dict[str, dict] = {}
    specific_pool: list[tuple[str, str, str, str]] = []  # (img_path, platform, site_domain, inst)
    with open(m1_work_dir / "metadata_open_web.jsonl", "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            m = json.loads(line)
            if m["sample_id"] not in m1_train_ids:
                continue
            rel = m["image_path"]
            if rel in val_img_paths:
                continue
            p_abs = m1_work_dir / rel
            if not p_abs.is_file():
                continue
            info = m1_imgs.setdefault(
                rel,
                {"platform": m.get("platform", "web"), "site_domain": m.get("site_domain", ""), "insts": set(), "full_insts": set()},
            )
            raw_inst = str(m.get("raw_instruction") or "").strip()
            full_inst = str(m.get("instruction") or "").strip()
            if raw_inst:
                info["insts"].add(raw_inst.lower())
            if full_inst:
                info["full_insts"].add(full_inst.lower())
            if m.get("action") == "left_click" and len(raw_inst) >= 14:
                low = raw_inst.lower()
                if low not in GENERIC_NEG_TERMS:
                    specific_pool.append((rel, info["platform"], info["site_domain"], full_inst))

    train_rel_list = sorted(m1_imgs.keys())
    rng.shuffle(train_rel_list)
    rng.shuffle(specific_pool)

    m1_neg_count = 0
    used_pairs: set[tuple[str, str]] = set()
    pool_idx = 0
    for rel in train_rel_list:
        if m1_neg_count >= m1_neg_target or not specific_pool:
            break
        src_p = m1_work_dir / rel
        img_bytes = src_p.read_bytes()
        img_hash = hashlib.sha256(img_bytes).hexdigest()
        if img_hash in val_sha256:
            continue
        t_info = m1_imgs[rel]
        chosen_inst = None
        for _ in range(30):
            o_rel, o_plat, o_dom, o_inst = specific_pool[pool_idx % len(specific_pool)]
            pool_idx += 1
            if o_rel == rel:
                continue
            if o_dom and o_dom == t_info["site_domain"] and o_plat == t_info["platform"]:
                continue
            o_low = o_inst.lower()
            if o_low in t_info["full_insts"] or any(known in o_low for known in t_info["insts"] if len(known) >= 6):
                continue
            if (rel, o_inst) in used_pairs:
                continue
            chosen_inst = o_inst
            used_pairs.add((rel, o_inst))
            break
        if chosen_inst is None:
            continue

        out_rel = f"images/m1neg_{img_hash[:12]}_{PurePosixPath(rel).name}"
        dest = out_dir / out_rel
        if not dest.exists():
            dest.write_bytes(img_bytes)
        m1_neg_count += 1
        sid = f"hardneg_m1_{m1_neg_count:05d}"
        ans = {"action": "not_found", "reason": "Target element is not present in the current screenshot"}
        rows.append(make_sft_record(sid, out_rel, chosen_inst, ans))
        meta_rows.append({"sample_id": sid, "source": "m1_cross_domain", "image_path": out_rel})

    write_jsonl(out_dir / "train_hard_neg.jsonl", rows)
    write_jsonl(out_dir / "metadata_hard_neg.jsonl", meta_rows)
    return {"total": len(rows), "zh_disjoint": zh_neg_count, "m1_cross_domain": m1_neg_count}


def convert_venus_zh_eval(venus_dir: Path, out_dir: Path) -> dict:
    """Convert VenusBench-GD web+desktop Chinese tasks (incl. refusal) into eval-only SFT JSONL."""
    img_out = out_dir / "images"
    img_out.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    meta_rows: list[dict] = []
    seen_imgs: dict[str, tuple[str, int, int]] = {}
    counts = Counter()

    for jf_name in [
        "element_grounding.json",
        "functional_grounding.json",
        "reason_grounding.json",
        "spatial_grounding.json",
        "visual_grounding.json",
        "refusal_grounding.json",
    ]:
        jf = venus_dir / "instruction" / jf_name
        if not jf.is_file():
            continue
        items = json.loads(jf.read_text(encoding="utf-8"))
        for idx, item in enumerate(items):
            img_rel = str(item.get("img_filename") or "")
            domain = img_rel.split("/", 1)[0]
            if domain not in {"web", "desktop"}:
                continue
            zh_inst = str(item.get("instruction_zh") or item.get("instruction") or "").strip()
            if not zh_inst:
                continue
            src_img = venus_dir / "images" / img_rel
            if not src_img.is_file():
                counts["skip_missing_img"] += 1
                continue

            if img_rel not in seen_imgs:
                with Image.open(src_img) as raw_im:
                    rgb = raw_im.convert("RGB")
                    orig_w, orig_h = rgb.size
                    resized = resize_for_qwen35(rgb)
                safe_name = f"images/venus_{domain}_{len(seen_imgs):05d}.jpg"
                resized.save(out_dir / safe_name, format="JPEG", quality=95)
                seen_imgs[img_rel] = (safe_name, orig_w, orig_h)

            out_rel, orig_w, orig_h = seen_imgs[img_rel]
            cat = item.get("category") or jf_name.removesuffix(".json")
            if cat == "refusal_grounding":
                ans = {"action": "not_found", "reason": "Target element is absent in screenshot"}
                norm_box = None
            else:
                bbox = item.get("bbox")
                if not isinstance(bbox, list) or len(bbox) != 4:
                    continue
                x1, y1, x2, y2 = [float(v) for v in bbox]
                if x2 <= x1 or y2 <= y1:
                    continue
                nx1 = min(max(round(x1 / max(1, orig_w - 1) * 1000.0), 0), 1000)
                ny1 = min(max(round(y1 / max(1, orig_h - 1) * 1000.0), 0), 1000)
                nx2 = min(max(round(x2 / max(1, orig_w - 1) * 1000.0), 0), 1000)
                ny2 = min(max(round(y2 / max(1, orig_h - 1) * 1000.0), 0), 1000)
                if nx2 <= nx1 or ny2 <= ny1:
                    continue
                norm_box = [nx1, ny1, nx2, ny2]
                ans = {"action": "left_click", "coordinate": [(nx1 + nx2) // 2, (ny1 + ny2) // 2]}

            sid = f"venus_zh_{cat}_{domain}_{idx:05d}"
            rows.append(make_sft_record(sid, out_rel, zh_inst, ans, split="eval_only"))
            meta_rows.append({
                "sample_id": sid,
                "split": "eval_only",
                "domain": domain,
                "category": cat,
                "image_path": out_rel,
                "instruction": zh_inst,
                "expected": ans,
                "bbox": norm_box,
            })
            counts[f"{domain}_{ans['action']}"] += 1

    write_jsonl(out_dir / "eval_venus_zh.jsonl", rows)
    write_jsonl(out_dir / "metadata_venus_zh.jsonl", meta_rows)
    return {"total": len(rows), "counts": dict(counts)}


def build_option_a_notebook(dest: Path) -> None:
    """Generate and AST-validate the standalone Colab Free-CPU Option A prep notebook."""
    import ast

    core_src = Path(__file__).read_text(encoding="utf-8").split("\ndef build_option_a_notebook(", 1)[0]

    def md_cell(txt: str) -> dict:
        return {"cell_type": "markdown", "metadata": {}, "source": [ln + "\n" for ln in txt.strip().splitlines()]}

    def code_cell(txt: str) -> dict:
        lines = txt.strip().splitlines()
        py_lines = [ln for ln in lines if not ln.lstrip().startswith(("!", "%"))]
        ast.parse("\n".join(py_lines))
        return {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": [ln + "\n" for ln in lines]}

    c1 = md_cell("""
# M3 Option A: Cloud Free-CPU Data Prep (0 CCU)
- **IMPORTANT**: Make sure Colab Runtime is **CPU (None accelerator)** so **0 CCU** are consumed!
- Downloads pinned Hugging Face commits only:
  - `AI45Research/SATraj-OS` (`Capability/chrome/` ~800 MiB, pinned rev `74deb768ee16733b952b789e91b69e7450df5dea`) -> `m3_satraj_chrome.tar` (`train` / `val` split by `trajectory_id`)
  - `inclusionAI/VenusBench-GD` (`web` + `desktop` Chinese tasks incl. `refusal_grounding`, pinned rev `7e5714566f912203c9989b06ef4c08bdf913e3fa`) -> `m3_venus_zh_eval.tar` (**eval-only, never trained**)
- Reads existing Drive bundles (`m1_web_bundle.tar` & `M3_zh_chrome_fixture_train_60.zip`) without touching `HOLDOUT` or `val` images to create:
  - `m3_option_a_zh_boost.tar` (4x diverse Chinese prompt phrasings per train fixture sample)
  - `m3_option_a_hard_neg.tar` (mathematically disjoint Chinese Chrome negatives + cross-domain specific `not_found`)
""")

    c2 = code_cell("""
import os, shutil, subprocess
from pathlib import Path
from google.colab import drive

if shutil.which("nvidia-smi") is not None:
    if subprocess.run(["nvidia-smi"], capture_output=True).returncode == 0:
        raise RuntimeError("GPU runtime detected! Switch Runtime -> Change runtime type -> CPU (0 CCU).")
print("[OK] CPU-only runtime confirmed (0 CCU).")

drive.mount("/content/drive")
DATA_DRIVE = Path("/content/drive/MyDrive/chrome-gui-pilot/data")
assert (DATA_DRIVE / "m1_web_bundle.tar").is_file(), "Missing m1_web_bundle.tar in Drive data/"
assert (DATA_DRIVE / "M3_zh_chrome_fixture_train_60.zip").is_file(), "Missing M3_zh_chrome_fixture_train_60.zip in Drive data/"
""")

    c3 = code_cell("""
!pip install -q -U huggingface-hub pillow
""")

    c4 = code_cell(core_src)

    c5 = code_cell("""
from huggingface_hub import hf_hub_download, snapshot_download

WORK = Path("/content/m3_option_a_work")
if WORK.exists():
    shutil.rmtree(WORK)
WORK.mkdir(parents=True)

# 1. Extract existing M1 bundle (for verified train-only image exclusion & negatives)
m1_dir = WORK / "m1_web"
m1_dir.mkdir()
with tarfile.open(DATA_DRIVE / "m1_web_bundle.tar", "r:*") as tar:
    kw = {"filter": "data"} if "filter" in tar.extractall.__code__.co_varnames else {}
    tar.extractall(m1_dir, **kw)

fixture_zip = WORK / "M3_zh_chrome_fixture_train_60.zip"
shutil.copy2(DATA_DRIVE / "M3_zh_chrome_fixture_train_60.zip", fixture_zip)

# 2. Download SATraj-OS Chrome subset (pinned commit, ~800 MiB)
satraj_jsonl = Path(hf_hub_download(SATRAJ_REPO, "Capability/chrome/trajectories.jsonl", repo_type="dataset", revision=SATRAJ_REV))
satraj_zip = Path(hf_hub_download(SATRAJ_REPO, "Capability/chrome/images.zip", repo_type="dataset", revision=SATRAJ_REV))

satraj_dir = WORK / "satraj_chrome"
satraj_res = convert_satraj_chrome(satraj_jsonl, satraj_zip, satraj_dir, seed=42, val_ratio=0.10)
satraj_tar = WORK / "m3_satraj_chrome.tar"
satraj_bytes = pack_tar(satraj_dir, satraj_tar)
print("[OK] SATraj Chrome:", satraj_res, f"({satraj_bytes / 1024**2:.1f} MiB)")

# 3. Build Chinese prompt-paraphrase boost (from train_60 only)
zh_boost_dir = WORK / "zh_boost"
zh_boost_res = build_zh_fixture_boost(fixture_zip, zh_boost_dir)
zh_boost_tar = WORK / "m3_option_a_zh_boost.tar"
zh_boost_bytes = pack_tar(zh_boost_dir, zh_boost_tar)
print("[OK] ZH Boost:", zh_boost_res, f"({zh_boost_bytes / 1024**2:.1f} MiB)")

# 4. Build verified train-only hard negatives (disjoint Chinese Chrome + M1 cross-domain train)
hard_neg_dir = WORK / "hard_neg"
hard_neg_res = build_hard_negatives(m1_dir, fixture_zip, hard_neg_dir, seed=42, m1_neg_target=560, zh_negs_per_img=4)
hard_neg_tar = WORK / "m3_option_a_hard_neg.tar"
hard_neg_bytes = pack_tar(hard_neg_dir, hard_neg_tar)
print("[OK] Hard Negatives:", hard_neg_res, f"({hard_neg_bytes / 1024**2:.1f} MiB)")

# 5. Download & pack VenusBench-GD web+desktop Chinese evaluation benchmark (eval-only!)
venus_local = Path(snapshot_download(
    VENUS_REPO,
    repo_type="dataset",
    revision=VENUS_REV,
    allow_patterns=["instruction/*.json", "images/web/*", "images/desktop/*"],
))
venus_dir = WORK / "venus_zh_eval"
venus_res = convert_venus_zh_eval(venus_local, venus_dir)
venus_tar = WORK / "m3_venus_zh_eval.tar"
venus_bytes = pack_tar(venus_dir, venus_tar)
print("[OK] VenusBench-GD ZH Eval (eval_only):", venus_res, f"({venus_bytes / 1024**2:.1f} MiB)")
""")

    c6 = code_cell("""
def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

outputs = [
    ("m3_satraj_chrome.tar", satraj_tar, satraj_res),
    ("m3_option_a_zh_boost.tar", zh_boost_tar, zh_boost_res),
    ("m3_option_a_hard_neg.tar", hard_neg_tar, hard_neg_res),
    ("m3_venus_zh_eval.tar", venus_tar, venus_res),
]
manifest = {"satraj_revision": SATRAJ_REV, "venus_revision": VENUS_REV, "bundles": {}}
for name, src, info in outputs:
    dst = DATA_DRIVE / name
    shutil.copy2(src, dst)
    manifest["bundles"][name] = {"bytes": dst.stat().st_size, "sha256": sha256_file(dst), "summary": info}
    print(f"[UPLOADED] {dst} ({dst.stat().st_size / 1024**2:.1f} MiB)")

manifest["recommended_m3_configure"] = {
    "pilot_note": "In M3_Colab_A100_Chrome_SFT.ipynb MODE='pilot' already caps m1_web at 1000; just set EXTRA_BUNDLES.",
    "full_note": "If using MODE='full', cap m1_web at 3500 in SOURCES[0] so left_click doesn't drown multi-action/ZH.",
    "m1_web_train_cap_full": 3500,
    "EXTRA_BUNDLES": [
        ["satraj_chrome", "m3_satraj_chrome.tar", "train_satraj_chrome.jsonl", "val_satraj_chrome.jsonl", None],
        ["zh_boost", "m3_option_a_zh_boost.tar", "train_zh_boost.jsonl", None, None],
        ["hard_neg", "m3_option_a_hard_neg.tar", "train_hard_neg.jsonl", None, None],
    ],
    "eval_only_never_train": "m3_venus_zh_eval.tar -> eval_venus_zh.jsonl",
}
(DATA_DRIVE / "m3_option_a_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
print("Paste into M3_Colab_A100_Chrome_SFT.ipynb configure cell:")
print("EXTRA_BUNDLES = [")
for item in manifest["recommended_m3_configure"]["EXTRA_BUNDLES"]:
    print("   ", tuple(item), ",")
print("]")
print("[DONE] Option A prep finished (0 GPU CCU used).")
""")

    nb = {
        "cells": [c1, c2, c3, c4, c5, c6],
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.11"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(nb, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[PASS] Built and AST-validated: {dest}")


if __name__ == "__main__":
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    NB_DIR = PROJECT_ROOT / "notebooks"
    build_option_a_notebook(NB_DIR / "M3_Data_Prep_OptionA_CPU.ipynb")
