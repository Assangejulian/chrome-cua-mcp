"""Diagnose VenusBench-GD 320-case M2 vs M3 predictions by domain/category, distance, size, and failure modes."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import io
import json
import math
from pathlib import Path
import re
import statistics
import sys
import zipfile

from PIL import Image, ImageDraw, ImageFont

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from m3_train_colab import check_action

PROMPT_EVAL_RE = re.compile(r"prompt eval time\s*=\s*[\d.]+\s*ms\s*/\s*(\d+)\s*tokens")


def load_font(size: int = 18) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for candidate in (
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
        Path(r"C:\Windows\Fonts\arial.ttf"),
    ):
        if candidate.is_file():
            try:
                return ImageFont.truetype(str(candidate), size=size)
            except OSError:
                pass
    return ImageFont.load_default()


def parse_domain_category(case: dict) -> tuple[str, str]:
    domain = case.get("domain")
    category = case.get("category")
    if domain and category:
        return str(domain), str(category)
    parts = str(case["sample_id"]).split("_")
    # venus_zh_<category>_<domain>_<idx>
    if len(parts) >= 5 and parts[0] == "venus":
        return parts[-2], "_".join(parts[2:-2])
    return str(domain or "unknown"), str(category or "unknown")


def point_to_box_distance(point: list[int], bbox: list[int]) -> float:
    px, py = float(point[0]), float(point[1])
    x1, y1, x2, y2 = [float(v) for v in bbox]
    dx = max(x1 - px, 0.0, px - x2)
    dy = max(y1 - py, 0.0, py - y2)
    return math.hypot(dx, dy)


def parse_prediction(raw_pred: str) -> dict | None:
    try:
        obj = json.loads(raw_pred)
        check_action(obj)
        return obj
    except (ValueError, TypeError):
        return None


def parse_server_prompt_tokens(log_path: Path) -> dict:
    if not log_path.is_file():
        return {"available": False}
    text = log_path.read_text(encoding="utf-8", errors="replace")
    tokens = [int(m.group(1)) for m in PROMPT_EVAL_RE.finditer(text)]
    if not tokens:
        return {"available": True, "count": 0}
    # If warmup request was prepended (321 = 1 warmup + 320 cases), also slice the last 320
    eval_tokens = tokens[-320:] if len(tokens) == 321 else tokens
    below_950 = sum(1 for t in eval_tokens if t < 950)
    below_1024 = sum(1 for t in eval_tokens if t < 1024)
    return {
        "available": True,
        "raw_request_count": len(tokens),
        "eval_request_count": len(eval_tokens),
        "min_tokens": min(eval_tokens),
        "median_tokens": statistics.median(eval_tokens),
        "max_tokens": max(eval_tokens),
        "below_950_count": below_950,
        "below_1024_count": below_1024,
    }


def render_overlay(
    img_bytes: bytes,
    out_path: Path,
    banner_lines: list[str],
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
    *,
    crosshair_norm: list[int] | None = None,
    bbox_norm: list[int] | None = None,
) -> None:
    with Image.open(io.BytesIO(img_bytes)) as im:
        rgb = im.convert("RGB")
    w, h = rgb.size
    banner_h = 26 * len(banner_lines) + 12
    canvas = Image.new("RGB", (w, h + banner_h), (22, 26, 34))
    canvas.paste(rgb, (0, banner_h))
    draw = ImageDraw.Draw(canvas)
    for idx, line in enumerate(banner_lines):
        draw.text((10, 6 + idx * 26), line, fill=(245, 248, 255), font=font)

    if bbox_norm is not None:
        x1 = min(max(round(bbox_norm[0] / 1000.0 * (w - 1)), 0), w - 1)
        y1 = min(max(round(bbox_norm[1] / 1000.0 * (h - 1)), 0), h - 1) + banner_h
        x2 = min(max(round(bbox_norm[2] / 1000.0 * (w - 1)), 0), w - 1)
        y2 = min(max(round(bbox_norm[3] / 1000.0 * (h - 1)), 0), h - 1) + banner_h
        draw.rectangle([(x1, y1), (x2, y2)], outline=(0, 230, 118), width=3)

    if crosshair_norm is not None:
        cx = min(max(round(crosshair_norm[0] / 1000.0 * (w - 1)), 0), w - 1)
        cy = min(max(round(crosshair_norm[1] / 1000.0 * (h - 1)), 0), h - 1) + banner_h
        r = 14
        draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], outline=(255, 56, 56), width=3)
        draw.line([(cx - 22, cy), (cx + 22, cy)], fill=(255, 56, 56), width=3)
        draw.line([(cx, cy - 22), (cx, cy + 22)], fill=(255, 56, 56), width=3)
        draw.ellipse([(cx - 3, cy - 3), (cx + 3, cy + 3)], fill=(255, 230, 0))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, format="PNG")


def analyze_model(cases: list[dict], preds: dict[str, str]) -> dict:
    by_dom_cat: dict[tuple[str, str], Counter] = defaultdict(Counter)
    miss_distances: list[float] = []
    miss_distances_by_domain: dict[str, list[float]] = defaultdict(list)
    area_buckets: dict[str, Counter] = defaultdict(Counter)
    totals = Counter()

    for case in cases:
        sid = case["sample_id"]
        domain, category = parse_domain_category(case)
        expected = case["expected"]
        exp_act = expected["action"]
        bbox = case.get("bbox")
        c = by_dom_cat[(domain, category)]
        c["total"] += 1
        totals["total"] += 1
        if exp_act == "not_found":
            totals["neg_total"] += 1
        else:
            totals["pos_total"] += 1

        parsed = parse_prediction(preds.get(sid, ""))
        if parsed is None:
            c["invalid"] += 1
            totals["invalid"] += 1
            continue

        c["valid"] += 1
        totals["valid"] += 1
        pred_act = parsed["action"]

        if exp_act == "not_found":
            if pred_act == "not_found":
                c["refusal_recall_hits"] += 1
                c["correct_action"] += 1
                totals["refusal_hits"] += 1
            elif pred_act not in {"not_found", "done", "wait"}:
                c["unsafe_action_proposed"] += 1
                totals["unsafe_action_proposed"] += 1
        else:
            area_bucket = "unknown"
            if bbox is not None:
                area = max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])
                area_bucket = "small_le_1500" if area <= 1500 else "regular_gt_1500"
                area_buckets[area_bucket]["total"] += 1

            if pred_act == "not_found":
                c["false_refusal"] += 1
                totals["false_refusal"] += 1
                if area_bucket != "unknown":
                    area_buckets[area_bucket]["false_refusal"] += 1
                continue

            if pred_act != exp_act:
                continue

            c["correct_action"] += 1
            totals["pos_correct_action"] += 1
            point = parsed.get("coordinate")
            if bbox is not None and point is not None:
                dist = point_to_box_distance(point, bbox)
                if dist == 0.0:
                    c["point_in_box"] += 1
                    totals["point_in_box"] += 1
                    area_buckets[area_bucket]["point_in_box"] += 1
                else:
                    miss_distances.append(dist)
                    miss_distances_by_domain[domain].append(dist)
                    if dist <= 50.0:
                        c["near_miss_le_50"] += 1
                        totals["near_miss_le_50"] += 1
                    else:
                        c["far_miss_gt_50"] += 1
                        totals["far_miss_gt_50"] += 1

    table = {}
    for (domain, category), cnt in sorted(by_dom_cat.items()):
        total = cnt["total"]
        row = dict(cnt)
        if category == "refusal_grounding":
            row["refusal_recall_pct"] = round(cnt["refusal_recall_hits"] / total * 100.0, 1) if total else 0.0
            row["unsafe_pct"] = round(cnt["unsafe_action_proposed"] / total * 100.0, 1) if total else 0.0
        else:
            row["point_in_box_pct"] = round(cnt["point_in_box"] / total * 100.0, 1) if total else 0.0
            row["false_refusal_pct"] = round(cnt["false_refusal"] / total * 100.0, 1) if total else 0.0
        table[f"{domain}/{category}"] = row

    miss_stats = {
        "count": len(miss_distances),
        "median_dist": round(statistics.median(miss_distances), 1) if miss_distances else None,
        "near_le_50": sum(1 for d in miss_distances if d <= 50.0),
        "far_gt_50": sum(1 for d in miss_distances if d > 50.0),
        "near_pct": round(sum(1 for d in miss_distances if d <= 50.0) / len(miss_distances) * 100.0, 1)
        if miss_distances
        else 0.0,
        "by_domain": {
            dom: {
                "count": len(vals),
                "median_dist": round(statistics.median(vals), 1),
                "near_le_50": sum(1 for d in vals if d <= 50.0),
                "far_gt_50": sum(1 for d in vals if d > 50.0),
            }
            for dom, vals in sorted(miss_distances_by_domain.items())
        },
    }

    size_stats = {}
    for bucket, cnt in sorted(area_buckets.items()):
        t = cnt["total"]
        size_stats[bucket] = {
            **dict(cnt),
            "point_in_box_pct": round(cnt["point_in_box"] / t * 100.0, 1) if t else 0.0,
        }

    return {
        "totals": dict(totals),
        "by_domain_category": table,
        "missed_positive_distance": miss_stats,
        "by_bbox_area": size_stats,
    }


def classify_instruction_pattern(inst: str) -> str:
    s = inst.strip()
    if any(k in s for k in ("登录", "注册", "账号", "密码", "退出", "个人中心", "头像")):
        return "account_auth"
    if any(k in s for k in ("关闭", "返回", "最小化", "最大化", "刷新", "新建", "删除", "保存", "导出", "下载", "上传", "打印", "设置")):
        return "action_button"
    if any(k in s for k in ("搜索", "查找", "筛选", "输入")):
        return "search_filter"
    if any(k in s for k in ("图标", "按钮", "菜单", "标签", "栏", "开关")):
        return "ui_widget"
    return "other"


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose VenusBench-GD 320-case M2 vs M3 outputs.")
    parser.add_argument("--bundle", type=Path, default=PROJECT_ROOT / "artifacts" / "venus_zh_eval_seed20261119_n320.zip")
    parser.add_argument(
        "--comparison-dir",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "m3_venus_comparison_20261119_165819",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "m3_venus_diagnosis_20261119",
    )
    args = parser.parse_args()

    with zipfile.ZipFile(args.bundle, "r") as zf:
        cases = [json.loads(line) for line in zf.read("holdout_chrome.jsonl").decode("utf-8").splitlines() if line.strip()]
        img_cache: dict[str, bytes] = {}
        for case in cases:
            img_name = case["image"]
            if img_name not in img_cache:
                img_cache[img_name] = zf.read(img_name)

    preds_by_model: dict[str, dict[str, str]] = {}
    for model in ("base", "m2", "m3"):
        pfile = args.comparison_dir / f"{model}-predictions.jsonl"
        rows = [json.loads(line) for line in pfile.read_text(encoding="utf-8").splitlines() if line.strip()]
        preds_by_model[model] = {r["sample_id"]: r["prediction"] for r in rows}

    m2_analysis = analyze_model(cases, preds_by_model["m2"])
    m3_analysis = analyze_model(cases, preds_by_model["m3"])
    token_stats = parse_server_prompt_tokens(args.comparison_dir / "m3-server.log")

    # Export refusal failures & false refusals
    refusal_failures: list[dict] = []
    false_refusals: list[dict] = []
    for case in cases:
        sid = case["sample_id"]
        domain, category = parse_domain_category(case)
        exp = case["expected"]
        m3_raw = preds_by_model["m3"][sid]
        m2_raw = preds_by_model["m2"][sid]
        m3_obj = parse_prediction(m3_raw)
        m2_obj = parse_prediction(m2_raw)

        if exp["action"] == "not_found" and m3_obj is not None and m3_obj["action"] != "not_found":
            refusal_failures.append(
                {
                    "sample_id": sid,
                    "domain": domain,
                    "category": category,
                    "instruction": case["instruction"],
                    "pattern": classify_instruction_pattern(case["instruction"]),
                    "m3_prediction": m3_obj,
                    "m2_prediction": m2_obj,
                    "image": case["image"],
                }
            )
        elif exp["action"] == "left_click" and m3_obj is not None and m3_obj["action"] == "not_found":
            bbox = case.get("bbox")
            m2_hit = False
            if m2_obj and m2_obj.get("action") == "left_click" and m2_obj.get("coordinate") and bbox:
                m2_hit = point_to_box_distance(m2_obj["coordinate"], bbox) == 0.0
            false_refusals.append(
                {
                    "sample_id": sid,
                    "domain": domain,
                    "category": category,
                    "instruction": case["instruction"],
                    "bbox": bbox,
                    "m3_prediction": m3_obj,
                    "m2_hit": m2_hit,
                    "image": case["image"],
                }
            )

    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "refusal_failures.jsonl").open("w", encoding="utf-8") as f:
        for row in refusal_failures:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (args.out / "false_refusals.jsonl").open("w", encoding="utf-8") as f:
        for row in false_refusals:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # Render error overlays (up to 8 web + 8 desktop refusal failures, plus all 11 false refusals)
    font = load_font(18)
    overlays_dir = args.out / "error_overlays"
    web_rf = [r for r in refusal_failures if r["domain"] == "web"][:8]
    desk_rf = [r for r in refusal_failures if r["domain"] == "desktop"][:8]
    for r in web_rf + desk_rf:
        coord = r["m3_prediction"].get("coordinate")
        render_overlay(
            img_cache[r["image"]],
            overlays_dir / f"refusal_fail_{r['sample_id']}.png",
            [
                f"[REFUSAL FAIL | {r['domain']}] {r['sample_id']}",
                f"Inst (NOT FOUND expected): {r['instruction']}",
                f"M3 proposed: {json.dumps(r['m3_prediction'], ensure_ascii=False)}",
            ],
            font,
            crosshair_norm=coord,
        )

    for r in false_refusals:
        render_overlay(
            img_cache[r["image"]],
            overlays_dir / f"false_refusal_{r['sample_id']}.png",
            [
                f"[FALSE REFUSAL | {r['domain']}/{r['category']}] {r['sample_id']} (m2_hit={r['m2_hit']})",
                f"Inst (POS expected): {r['instruction']}",
                f"M3 refused: {json.dumps(r['m3_prediction'], ensure_ascii=False)}",
            ],
            font,
            bbox_norm=r["bbox"],
        )

    rf_by_domain = dict(Counter(r["domain"] for r in refusal_failures))
    rf_by_pattern = dict(Counter(r["pattern"] for r in refusal_failures))
    rf_by_action = dict(Counter(r["m3_prediction"]["action"] for r in refusal_failures))
    fr_by_dom_cat = dict(Counter(f"{r['domain']}/{r['category']}" for r in false_refusals))

    summary = {
        "bundle": str(args.bundle),
        "comparison_dir": str(args.comparison_dir),
        "sample_count": len(cases),
        "m2": m2_analysis,
        "m3": m3_analysis,
        "m3_prompt_tokens": token_stats,
        "refusal_failures_summary": {
            "count": len(refusal_failures),
            "by_domain": rf_by_domain,
            "by_predicted_action": rf_by_action,
            "by_instruction_pattern": rf_by_pattern,
        },
        "false_refusals_summary": {
            "count": len(false_refusals),
            "by_domain_category": fr_by_dom_cat,
            "m2_hit_on_these_cases": sum(1 for r in false_refusals if r["m2_hit"]),
        },
    }
    (args.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Build Markdown report
    lines = [
        "# VenusBench-GD 中文 320 题深度诊断报告（M2 vs M3 Q4）",
        "",
        f"- 评测总题数：`{len(cases)}`（正向 `240`，缺失 `80`）",
        f"- M3 总计对齐复核：正向落框 `{m3_analysis['totals'].get('point_in_box', 0)}/240`，正确拒识 `{m3_analysis['totals'].get('refusal_hits', 0)}/80`，正向误拒 `{m3_analysis['totals'].get('false_refusal', 0)}/240`",
        f"- M2 总计对齐复核：正向落框 `{m2_analysis['totals'].get('point_in_box', 0)}/240`，正确拒识 `{m2_analysis['totals'].get('refusal_hits', 0)}/80`，正向误拒 `{m2_analysis['totals'].get('false_refusal', 0)}/240`",
        "",
        "## 一、按 Domain × Category 分桶成绩",
        "",
        "| Domain / Category | 题数 | M2 命中/拒识 | M3 命中/拒识 | M3 正向误拒 | M3 近偏(≤50) | M3 远偏(>50) |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]

    for key in sorted(m3_analysis["by_domain_category"]):
        m2r = m2_analysis["by_domain_category"][key]
        m3r = m3_analysis["by_domain_category"][key]
        tot = m3r["total"]
        if key.endswith("/refusal_grounding"):
            m2_val = f"{m2r.get('refusal_recall_hits', 0)}/{tot} ({m2r.get('refusal_recall_pct', 0)}%)"
            m3_val = f"{m3r.get('refusal_recall_hits', 0)}/{tot} ({m3r.get('refusal_recall_pct', 0)}%)"
            fr = "-"
            near = "-"
            far = "-"
        else:
            m2_val = f"{m2r.get('point_in_box', 0)}/{tot} ({m2r.get('point_in_box_pct', 0)}%)"
            m3_val = f"{m3r.get('point_in_box', 0)}/{tot} ({m3r.get('point_in_box_pct', 0)}%)"
            fr = str(m3r.get("false_refusal", 0))
            near = str(m3r.get("near_miss_le_50", 0))
            far = str(m3r.get("far_miss_gt_50", 0))
        lines.append(f"| `{key}` | {tot} | {m2_val} | {m3_val} | {fr} | {near} | {far} |")

    lines.extend(
        [
            "",
            "## 二、正向未命中距离分布 & 控件大小分桶",
            "",
            f"- **M3 错失正例**（输出 `left_click` 但未落框）：共 `{m3_analysis['missed_positive_distance']['count']}` 题，"
            f"到目标框最近边中位归一化距离 `{m3_analysis['missed_positive_distance']['median_dist']}`（0–1000 尺度）；"
            f"近偏 `dist <= 50` 占 `{m3_analysis['missed_positive_distance']['near_le_50']}` 题 (`{m3_analysis['missed_positive_distance']['near_pct']}%`)，"
            f"远偏 `dist > 50` 占 `{m3_analysis['missed_positive_distance']['far_gt_50']}` 题。",
            f"- **M2 错失正例**：共 `{m2_analysis['missed_positive_distance']['count']}` 题，"
            f"中位距离 `{m2_analysis['missed_positive_distance']['median_dist']}`，"
            f"近偏 `dist <= 50` 为 `{m2_analysis['missed_positive_distance']['near_le_50']}` 题 (`{m2_analysis['missed_positive_distance']['near_pct']}%`)。",
            "",
            "| 归一化框面积分桶 | 题数 | M2 落框率 | M3 落框率 | M3 正向误拒 |",
            "| :--- | ---: | ---: | ---: | ---: |",
        ]
    )
    for bkt in sorted(m3_analysis["by_bbox_area"]):
        b2 = m2_analysis["by_bbox_area"][bkt]
        b3 = m3_analysis["by_bbox_area"][bkt]
        lines.append(
            f"| `{bkt}` | {b3['total']} | {b2.get('point_in_box',0)}/{b2['total']} ({b2['point_in_box_pct']}%) "
            f"| {b3.get('point_in_box',0)}/{b3['total']} ({b3['point_in_box_pct']}%) | {b3.get('false_refusal',0)} |"
        )

    lines.extend(
        [
            "",
            "## 三、M3 服务端视觉输入 Token 统计（`m3-server.log`）",
            "",
            f"- 解析请求数：`{token_stats.get('eval_request_count')}`（总日志行数 `{token_stats.get('raw_request_count')}`）",
            f"- Prompt Tokens（含固定系统提示+指令+图像）：min=`{token_stats.get('min_tokens')}` / median=`{token_stats.get('median_tokens')}` / max=`{token_stats.get('max_tokens')}`",
            f"- `< 950` tokens 请求数：`{token_stats.get('below_950_count')}`；`< 1024` tokens 请求数：`{token_stats.get('below_1024_count')}`",
            "",
            "## 四、72 条缺失题误操作与 11 条正向误拒拆解",
            "",
            f"- **72 条缺失题误操作**：按 domain `{rf_by_domain}`，模型全部输出 `{rf_by_action}`；按指令语义粗分 `{rf_by_pattern}`。",
            f"- **11 条正向误拒**：按分桶 `{fr_by_dom_cat}`；其中 M2 在这 11 题上命中了 `{summary['false_refusals_summary']['m2_hit_on_these_cases']}` 题。",
            "",
            "### 部分错例清单（完整清单见 `refusal_failures.jsonl` 与 `false_refusals.jsonl`）",
            "",
            "**11 条正向误拒全表：**",
            "",
            "| sample_id | 分桶 | 指令 | M2 是否命中 | M3 理由 |",
            "| :--- | :--- | :--- | :---: | :--- |",
        ]
    )
    for r in false_refusals:
        lines.append(
            f"| `{r['sample_id']}` | `{r['domain']}/{r['category']}` | {r['instruction']} | {'✅' if r['m2_hit'] else '❌'} | `{r['m3_prediction'].get('reason', '')}` |"
        )

    lines.extend(["", "**缺失题误操作抽样（前 20 条）：**", "", "| sample_id | domain | 指令 | M3 误操作坐标 |", "| :--- | :--- | :--- | :--- |"])
    for r in refusal_failures[:20]:
        lines.append(f"| `{r['sample_id']}` | `{r['domain']}` | {r['instruction']} | `{r['m3_prediction'].get('coordinate')}` |")

    (args.out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary["refusal_failures_summary"], ensure_ascii=False))
    print(json.dumps(summary["false_refusals_summary"], ensure_ascii=False))
    print(f"Report written to {args.out / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
