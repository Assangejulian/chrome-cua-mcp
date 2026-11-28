"""Teacher-mode data collector: a human/agent-curated task plan is executed on
real Chinese sites in a managed Chrome. Each verified step yields one
SFT sample in the exact m35_collect_dataset schema:

    frames/train/f0001.png   (raw full-window capture)
    images/f0001.jpg         (resize_for_qwen35 + JPEG q93, final bundle form)
    records.jsonl            (sample rows: instruction / expected / bbox / meta)

Phases:
    survey  --navigate sites, save window PNG + candidate element list
    run     --execute plan.jsonl items, verify effects, emit samples

Plan item schema (one per line):
  {"site": "...", "url": "...", "tasks": [
      {"kind": "click",  "match": "国际", "label": "国际", "inst_kind": "web_click"},
      {"kind": "type",   "match": "搜索", "label": "搜索", "text": "今日要闻"},
      {"kind": "scroll", "direction": "down", "amount": 4, "topic": "页面"},
      {"kind": "not_found", "label": "会员中心"}
  ]}
`match` is resolved against the runtime candidate list (substring, first hit).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import cua_engine  # noqa: E402
from m3_auto_collect_chrome import center_norm, px_box_to_norm  # noqa: E402
from m3_train_colab import check_action  # noqa: E402
from m35_collect_dataset import make_instruction_v2  # noqa: E402
from m3_prepare_option_a import resize_for_qwen35  # noqa: E402
from benchmark_cn500 import is_blocked  # noqa: E402
from urllib.parse import urldefrag, urlparse  # noqa: E402

JS_CANDIDATES = r"""
(() => {
  const vw = innerWidth, vh = innerHeight;
  const out = []; const seen = new Set();
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const st = getComputedStyle(el);
    return st.display !== 'none' && st.visibility !== 'hidden' && parseFloat(st.opacity || '1') > 0.05; };
  // allow elements up to 2.5 viewports below the fold (scroll targets / off-screen negatives)
  const onscreen = r => r.width >= 8 && r.height >= 8 && r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
  const near = r => r.width >= 8 && r.height >= 8 && r.bottom > 0 && r.top < vh * 2.5 && r.right > -vw && r.left < vw * 2;
  const push = (el, label, extra) => {
    const r = el.getBoundingClientRect();
    if (!near(r)) return;
    const key = label + '@' + Math.round(r.left / 8) + ',' + Math.round(r.top / 8);
    if (seen.has(key)) return; seen.add(key);
    out.push(Object.assign({tag: el.tagName.toLowerCase(), label: label.slice(0, 60),
                            inview: onscreen(r),
                            rect: [r.left, r.top, r.right, r.bottom]}, extra));
  };
  document.querySelectorAll('a[href]').forEach(el => {
    if (!vis(el)) return;
    const t = clean(el.innerText || el.getAttribute('aria-label') || el.title || '');
    if (t.length >= 2 && t.length <= 40)
      push(el, t, {href: (el.getAttribute('href') || '').slice(0, 160)});
  });
  document.querySelectorAll('button,[role="button"],input[type="button"],input[type="submit"],summary,[role="tab"],[role="link"]').forEach(el => {
    if (!vis(el)) return;
    const t = clean(el.innerText || el.value || el.getAttribute('aria-label') || '');
    if (t && t.length <= 30) push(el, t, {});
  });
  document.querySelectorAll('input[type="checkbox"],input[type="radio"]').forEach(el => {
    if (!vis(el)) return;
    let t = clean(el.getAttribute('aria-label') || '');
    if (!t && el.id) { const lb = document.querySelector('label[for="' + el.id + '"]'); if (lb) t = clean(lb.innerText); }
    if (!t && el.parentElement) t = clean(el.parentElement.innerText || '');
    push(el, t || el.type, {checkable: true, ctype: el.type});
  });
  document.querySelectorAll('input:not([type="hidden"]):not([type="button"]):not([type="submit"]):not([type="checkbox"]):not([type="radio"]),textarea,select').forEach(el => {
    if (!vis(el)) return;
    const t = clean(el.getAttribute('placeholder') || el.getAttribute('aria-label') || el.name || el.id || el.type || '输入框');
    push(el, t || '输入框', {input: true, type: el.type || el.tagName.toLowerCase()});
  });
  return out.slice(0, 120);
})()
"""

JS_BODY_HASH = "(()=>{const t=(document.body?document.body.innerText:'').slice(0,20000);let h=0;for(let i=0;i<t.length;i++)h=(h*31+t.charCodeAt(i))|0;return [h,document.title];})()"

NAV_TIMEOUT_S = 15.0


def _min_jerk(u: float) -> float:
    """Minimum-jerk distance profile: zero velocity at both ends, peak mid-way.
    Classic model of human reaching movements (Flash & Hogan 1985)."""
    return u * u * u * (10.0 - 15.0 * u + 6.0 * u * u)


def gen_path(sx: float, sy: float, ex: float, ey: float, rng: random.Random) -> list[tuple[float, float]]:
    """Human-ish pointer path in window px: quadratic bezier (side control
    offset) sampled at minimum-jerk time params, so points cluster at the
    ends and spread out mid-flight — i.e. non-uniform speed."""
    dist = math.hypot(ex - sx, ey - sy)
    n = max(6, min(30, int(dist / 40)))
    nx, ny = -(ey - sy), (ex - sx)
    nl = math.hypot(nx, ny) or 1.0
    off = rng.uniform(-0.12, 0.12) * dist
    cx = (sx + ex) / 2 + nx / nl * off
    cy = (sy + ey) / 2 + ny / nl * off
    pts = []
    for i in range(1, n + 1):
        s = _min_jerk(i / n)          # wall-clock -> distance fraction
        u = 1 - s
        x = u * u * sx + 2 * u * s * cx + s * s * ex + rng.gauss(0, 1.5)
        y = u * u * sy + 2 * u * s * cy + s * s * ey + rng.gauss(0, 1.5)
        pts.append((x, y))
    pts[-1] = (ex, ey)
    return pts


class Session:
    """Thin wrapper around ChromeCUAEngine for teacher collection."""

    def __init__(self, tmp: tempfile.TemporaryDirectory):
        self.tmp = tmp
        self.engine = cua_engine.ChromeCUAEngine(profile_dir=Path(tmp.name))
        self.chrome = self.engine.chrome

    def close(self) -> None:
        try:
            self.engine.close()
        except Exception:
            pass
        try:
            self.tmp.cleanup()
        except Exception:
            pass

    def cdp(self, expr: str, timeout: float = 10.0):
        return self.chrome.cdp.evaluate(expr, timeout=timeout)

    def nav(self, url: str) -> dict:
        return self.chrome.navigate(url, timeout_s=NAV_TIMEOUT_S)

    def page_targets(self) -> list[dict]:
        raw = urllib.request.urlopen(
            f"http://127.0.0.1:{self.chrome.cdp_port}/json/list", timeout=5).read()
        return [t for t in json.loads(raw) if t.get("type") == "page"]

    def target_ids(self) -> set[str]:
        return {str(t["id"]) for t in self.page_targets()}

    def sync_active_tab(self) -> None:
        """Bring the CDP-attached target back to front so captures match the DOM."""
        tid = self.chrome.active_target_id
        if not tid:
            return
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.chrome.cdp_port}/json/activate/{tid}", timeout=5).read()
        except Exception:
            pass
        time.sleep(0.15)

    def close_extra_tabs(self, keep: set[str] | None = None) -> None:
        keep = keep or set()
        for t in self.page_targets():
            if t["id"] == self.chrome.active_target_id or t["id"] in keep:
                continue
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{self.chrome.cdp_port}/json/close/{t['id']}", timeout=5).read()
            except Exception:
                pass

    def shot(self):
        return self.chrome.capture_full_window()

    # pointer starts at a plausible resting spot (window px)
    _pointer = (640.0, 420.0)

    def mouse_path(self, wx: float, wy: float, vp_left: float, vp_top: float, dpr: float,
                   rng: random.Random, step_s: float = 0.012,
                   pressed: bool = False) -> list[list[float]]:
        """Emit a human-ish mouseMoved trajectory to window-px (wx, wy).
        Returns [[x_norm1000, y_norm1000, t_ms], ...] for the sample."""
        sx, sy = self._pointer
        pts = gen_path(sx, sy, wx, wy, rng)
        traj: list[list[float]] = []
        t_ms = 0.0
        for i, (px, py) in enumerate(pts):
            self.chrome.cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": (px - vp_left) / dpr,
                "y": (py - vp_top) / dpr,
                "button": "left" if pressed else "none",
                "buttons": 1 if pressed else 0})
            # jittered frame pacing — humans don't emit at a fixed rate
            dt = step_s * min(max(rng.gauss(1.0, 0.3), 0.4), 2.5)
            time.sleep(dt)
            t_ms += dt * 1000.0
            traj.append([round(px, 1), round(py, 1), round(t_ms)])
        self._pointer = (wx, wy)
        return traj

    def norm_traj(self, traj: list[list[float]], w: int, h: int) -> list[list[float]]:
        return [[min(max(round(p[0] / max(1, w - 1) * 1000), 0), 1000),
                 min(max(round(p[1] / max(1, h - 1) * 1000), 0), 1000), p[2]] for p in traj]

    def vp_map(self, info) -> tuple[float, float, float]:
        """Return (vp_left, vp_top, dpr): CSS px -> window px mapping."""
        m = self.cdp("[window.innerWidth, window.innerHeight, window.devicePixelRatio || 1]")
        iw, ih, dpr = float(m[0]), float(m[1]), float(m[2])
        vp_left = info.client_rect.left - info.dwm_rect.left
        vp_top = (info.client_rect.bottom - info.dwm_rect.top) - round(ih * dpr)
        return vp_left, vp_top, dpr


def elem_box_px(rect_css: list[float], vp_left: float, vp_top: float, dpr: float) -> list[int]:
    """Element CSS rect -> full-window pixel box (matching the captured image)."""
    x1 = vp_left + rect_css[0] * dpr
    y1 = vp_top + rect_css[1] * dpr
    x2 = vp_left + rect_css[2] * dpr
    y2 = vp_top + rect_css[3] * dpr
    return [round(x1), round(y1), round(x2), round(y2)]


def find_candidate(cands: list[dict], match: str) -> dict | None:
    m = match.strip()
    for c in cands:
        if c["label"] == m:
            return c
    for c in cands:
        if m in c["label"] or c["label"] in m:
            return c
    return None


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def cmd_survey(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from cn500_sites import CASES
    ok_ids: set[int] | None = None
    res_path = args.results
    if res_path and Path(res_path).exists():
        ok_ids = {json.loads(l)["id"] for l in Path(res_path).read_text(encoding="utf-8").splitlines()
                  if l.strip() and json.loads(l).get("outcome") == "passed"}
    sites = [c for c in CASES if ok_ids is None or c["id"] in ok_ids]
    if args.ids:
        want = {int(v) for v in args.ids.split(",") if v.strip()}
        sites = [c for c in sites if c["id"] in want]
    sites = sites[: args.limit]

    out = Path(args.out)
    (out / "shots").mkdir(parents=True, exist_ok=True)
    fp = (out / "survey.jsonl").open("w", encoding="utf-8", buffering=1)
    sess = Session(tempfile.TemporaryDirectory(prefix="teacher_survey_", ignore_cleanup_errors=True))
    try:
        for i, site in enumerate(sites):
            cid, url, name = site["id"], site["url"], site["name"]
            rec = {"id": cid, "site": name, "url": url, "category": site["category"]}
            try:
                nav = sess.nav(url)
                rec["nav_status"] = "ok" if str(nav.get("url", "")).startswith("http") else "no_page"
                if rec["nav_status"] == "ok":
                    def _blocked() -> str | None:
                        return is_blocked({
                            "title": sess.cdp("document.title") or "",
                            "url": sess.cdp("location.href") or "",
                            "text_excerpt": sess.cdp("(document.body?document.body.innerText:'').slice(0,1200)") or ""})
                    blocked = _blocked()
                    if blocked is None:
                        time.sleep(0.3)
                        blocked = _blocked()
                    rec["blocked"] = blocked
                    if not blocked:
                        sess.sync_active_tab()
                        info, img = sess.shot()
                        shot_rel = f"shots/{cid:03d}_{i:03d}.png"
                        img.save(out / shot_rel)
                        rec["shot"] = shot_rel
                        rec["img_size"] = list(img.size)
                        cands = sess.cdp(JS_CANDIDATES) or []
                        rec["candidates"] = cands[:40]
                        rec["title"] = sess.cdp("document.title") or ""
            except Exception as exc:
                rec["error"] = (type(exc).__name__ + ": " + str(exc))[:160]
            finally:
                sess.close_extra_tabs()
            fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(json.dumps({"id": cid, "site": name, "ok": "shot" in rec,
                              "cands": len(rec.get("candidates", []))}, ensure_ascii=False), flush=True)
    finally:
        fp.close()
        sess.close()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    out = Path(args.out)
    (out / "frames" / "train").mkdir(parents=True, exist_ok=True)
    (out / "images").mkdir(parents=True, exist_ok=True)
    rec_path = out / "records.jsonl"
    rng = random.Random(args.seed)

    existing = [int(p.stem[1:]) for p in (out / "frames" / "train").glob("f*.png")
                if p.stem[1:].isdigit()]
    seq = max(existing, default=0)
    log = []

    plan_items = [json.loads(l) for l in Path(args.plan).read_text(encoding="utf-8").splitlines()
                  if l.strip()]
    done_keys: set[tuple[str, int]] = set()
    if getattr(args, "skip_done", False) and rec_path.exists():
        tag = getattr(args, "tag", "") or None
        for l in rec_path.read_text(encoding="utf-8").splitlines():
            if not l.strip():
                continue
            r = json.loads(l)
            if (r.get("campaign") or None) != tag:
                continue
            m = re.match(r"f\d+_(\d+)", str(r.get("sample_id", "")))
            if m and r.get("site"):
                done_keys.add((str(r["site"]), int(m.group(1))))
        print(f"[skip-done] {len(done_keys)} task slots already collected", flush=True)
    sess = Session(tempfile.TemporaryDirectory(prefix="teacher_run_", ignore_cleanup_errors=True))
    samples_total = 0

    def emit(rec: dict, verify: dict, img, ti: int = 0, sub: int = 0) -> None:
        nonlocal seq, samples_total, site, url, domain
        if rec["expected"]["action"] == "drag":
            if not (isinstance(rec["expected"].get("to"), list) and len(rec["expected"]["to"]) == 2):
                raise ValueError("drag requires 'to' coordinate")
        else:
            check_action(rec["expected"])
        seq += 1
        frame_id = f"f{seq:04d}"
        png_path = out / "frames" / "train" / f"{frame_id}.png"
        img.convert("RGB").save(png_path, format="PNG", compress_level=4)
        jpg_path = out / "images" / f"{frame_id}.jpg"
        resize_for_qwen35(img.convert("RGB")).save(jpg_path, format="JPEG", quality=93)
        jhash = hashlib.sha256(jpg_path.read_bytes()).hexdigest()
        sample = {
            "sample_id": f"{frame_id}_{ti + 1:02d}{'-' + str(sub) if sub else ''}_{rec['bucket']}",
            "split": "train",
            "image": f"images/{frame_id}.jpg",
            "image_sha256": jhash,
            "domain": domain,
            "source_type": "teacher_real_site",
            "profile": "teacher",
            "bucket": rec["bucket"],
            "neg_subtype": rec.get("neg_subtype"),
            "target_desc": rec["target_desc"],
            "instruction": rec["instruction"],
            "expected": rec["expected"],
            "bbox": rec["bbox"],
            "bbox_px": rec["bbox_px"],
            "site": site,
            "page_url": str(sess.cdp("location.href") or url),
            "verified": rec["verified"],
            "verify": verify,
            "trajectory": rec.get("trajectory"),
            "extended_action": rec["expected"]["action"] not in
                ("left_click", "right_click", "double_click", "move", "scroll",
                 "type", "hotkey", "wait", "done", "not_found"),
            "collected_at": datetime.now().isoformat(timespec="seconds"),
            "campaign": getattr(args, "tag", "") or None,
        }
        with rec_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(sample, ensure_ascii=False) + "\n")
        samples_total += 1
        log.append({"site": site, "url": url, "domain": domain, "task": ti, "frame": frame_id,
                    "bucket": rec["bucket"], "verified": rec["verified"],
                    "instruction": rec["instruction"]})
        print(json.dumps(log[-1], ensure_ascii=False), flush=True)

    try:
        for item in plan_items:
            site, url = item["site"], item["url"]
            domain = urlparse(url).netloc
            ctx = {"site": site, "url": url, "domain": domain}
            try:
                try:
                    nav = sess.nav(url)
                except Exception as exc:
                    log.append({**ctx, "skip": f"nav_error:{exc}"})
                    continue
                if not str(nav.get("url", "")).startswith("http"):
                    log.append({**ctx, "skip": f"nav:{nav}"})
                    continue
                blocked = is_blocked({
                    "title": sess.cdp("document.title") or "",
                    "url": sess.cdp("location.href") or "",
                    "text_excerpt": sess.cdp("(document.body?document.body.innerText:'').slice(0,1200)") or ""})
                if blocked:
                    log.append({**ctx, "skip": f"blocked:{blocked}"})
                    continue
                sess.sync_active_tab()
                sess.close_extra_tabs()
                time.sleep(0.2)
                base_url = str(sess.cdp("location.href") or url)

                for ti, task in enumerate(item["tasks"]):
                    if (site, ti + 1) in done_keys:
                        continue
                    # re-navigate if a previous action walked the main tab away
                    cur_url = str(sess.cdp("location.href") or "")
                    if urldefrag(cur_url)[0] != urldefrag(base_url)[0]:
                        try:
                            sess.nav(url)
                            sess.sync_active_tab()
                            sess.close_extra_tabs()
                            time.sleep(0.4)
                        except Exception as exc:
                            log.append({**ctx, "task": ti, "skip": f"renav:{exc}"})
                            break
                    # refresh candidates + shot per step (page state evolves)
                    try:
                        info, img = sess.shot()
                    except Exception as exc:
                        log.append({**ctx, "task": ti, "skip": f"shot:{exc}"})
                        break
                    cands = sess.cdp(JS_CANDIDATES) or []
                    vp_left, vp_top, dpr = sess.vp_map(info)
                    W, H = img.size
                    rec = None
                    verify = {"kind": task["kind"]}

                    if task["kind"] == "click":
                        c = find_candidate(cands, task["match"])
                        if not c:
                            log.append({**ctx, "task": ti, "skip": f"target_missing:{task['match']}"})
                            continue
                        box = elem_box_px(c["rect"], vp_left, vp_top, dpr)
                        bn = px_box_to_norm(box, W, H)
                        expected = {"action": "left_click", "coordinate": center_norm(bn)}
                        inst = make_instruction_v2(rng, task.get("inst_kind", "web_click"), label=task["label"])
                        before_url = str(sess.cdp("location.href") or "")
                        before_hash = sess.cdp(JS_BODY_HASH)
                        before_tabs = sess.target_ids()
                        px = min(max(round(expected["coordinate"][0] / 1000 * (W - 1)), 0), W - 1)
                        py = min(max(round(expected["coordinate"][1] / 1000 * (H - 1)), 0), H - 1)
                        css_x, css_y = (px - vp_left) / dpr, (py - vp_top) / dpr
                        traj = sess.norm_traj(
                            sess.mouse_path(px, py, vp_left, vp_top, dpr, rng), W, H)
                        for t in ("mousePressed", "mouseReleased"):
                            sess.chrome.cdp.send("Input.dispatchMouseEvent",
                                                 {"type": t, "x": css_x, "y": css_y, "button": "left",
                                                  "buttons": 1 if t == "mousePressed" else 0, "clickCount": 1})
                        time.sleep(1.6)
                        after_url = str(sess.cdp("location.href") or "")
                        after_hash = sess.cdp(JS_BODY_HASH)
                        after_tabs = sess.target_ids()
                        landed = (urldefrag(after_url)[0] != urldefrag(before_url)[0]
                                  or bool(after_tabs - before_tabs)
                                  or (before_hash and after_hash and before_hash[0] != after_hash[0]))
                        verify.update({"before_url": before_url, "after_url": after_url,
                                       "new_tabs": len(after_tabs - before_tabs), "landed": landed})
                        rec = {"bucket": "web_click", "instruction": inst, "expected": expected,
                               "bbox": bn, "bbox_px": box, "target_desc": f"web:{task['label']}",
                               "verified": bool(landed), "trajectory": traj}
                        if after_tabs - before_tabs:
                            sess.close_extra_tabs(keep=before_tabs)
                            sess.sync_active_tab()
                    elif task["kind"] == "type":
                        c = find_candidate(cands, task["match"])
                        if not c:
                            log.append({**ctx, "task": ti, "skip": f"input_missing:{task['match']}"})
                            continue
                        box = elem_box_px(c["rect"], vp_left, vp_top, dpr)
                        bn = px_box_to_norm(box, W, H)
                        coord = center_norm(bn)
                        expected = {"action": "type", "coordinate": coord, "text": task["text"]}
                        inst = make_instruction_v2(rng, "web_type", label=task["label"], text=task["text"])
                        px = min(max(round(coord[0] / 1000 * (W - 1)), 0), W - 1)
                        py = min(max(round(coord[1] / 1000 * (H - 1)), 0), H - 1)
                        css_x, css_y = (px - vp_left) / dpr, (py - vp_top) / dpr
                        traj = sess.norm_traj(
                            sess.mouse_path(px, py, vp_left, vp_top, dpr, rng), W, H)
                        for t in ("mousePressed", "mouseReleased"):
                            sess.chrome.cdp.send("Input.dispatchMouseEvent",
                                                 {"type": t, "x": css_x, "y": css_y, "button": "left",
                                                  "buttons": 1 if t == "mousePressed" else 0, "clickCount": 1})
                        time.sleep(0.25)
                        if task.get("clear", True):
                            sess.cdp("(()=>{const e=document.elementFromPoint(%f,%f);"
                                     "const t=e&&(e.closest('input,textarea')||e);"
                                     "if(t&&'value'in t){t.value='';t.dispatchEvent(new Event('input',{bubbles:true}));}"
                                     "return true})()" % (css_x, css_y), timeout=5)
                            time.sleep(0.1)
                        sess.chrome.cdp.send("Input.insertText", {"text": task["text"]})
                        time.sleep(0.3)
                        val = sess.cdp("document.activeElement && (document.activeElement.value||'')") or ""
                        ok = task["text"][:4] in str(val)
                        verify.update({"value": str(val)[:40], "typed": ok})
                        rec = {"bucket": "web_type", "instruction": inst, "expected": expected,
                               "bbox": bn, "bbox_px": box, "target_desc": f"input:{task['label']}",
                               "verified": bool(ok), "trajectory": traj}
                    elif task["kind"] == "scroll":
                        direction = task.get("direction", "down")
                        amount = int(task.get("amount", 4))
                        expected = {"action": "scroll", "coordinate": [500, 600],
                                    "direction": direction, "amount": amount}
                        inst = make_instruction_v2(rng, "web_scroll", topic=task.get("topic", "页面"),
                                                   direction=direction, amount=amount)
                        before_y = float(sess.cdp("window.scrollY") or 0)
                        sess.mouse_path(W * 0.5, H * 0.6, vp_left, vp_top, dpr, rng)
                        sess.chrome.cdp.send("Input.dispatchMouseEvent",
                                             {"type": "mouseWheel", "x": W / 2 / dpr, "y": 0.6 * (H / dpr),
                                              "deltaX": 0, "deltaY": 120 * amount * (-1 if direction == "up" else 1)})
                        time.sleep(0.6)
                        after_y = float(sess.cdp("window.scrollY") or 0)
                        moved = (after_y - before_y) * (1 if direction == "down" else -1) > 30
                        verify.update({"before_y": before_y, "after_y": after_y, "moved": moved})
                        rec = {"bucket": "web_scroll", "instruction": inst, "expected": expected,
                               "bbox": None, "bbox_px": None, "target_desc": "scroll:page",
                               "verified": bool(moved)}
                    elif task["kind"] == "drag":
                        task.setdefault("label", task["match"])
                        c = find_candidate(cands, task["match"])
                        if not c:
                            log.append({**ctx, "task": ti, "skip": f"target_missing:{task['match']}"})
                            continue
                        box = elem_box_px(c["rect"], vp_left, vp_top, dpr)
                        bn = px_box_to_norm(box, W, H)
                        cx_px = (box[0] + box[2]) / 2
                        cy_px = (box[1] + box[3]) / 2
                        dx = float(task.get("dx", -200))
                        dy = float(task.get("dy", 0))
                        ex_px = min(max(cx_px + dx, 5), W - 5)
                        ey_px = min(max(cy_px + dy, 5), H - 5)
                        start_n = [min(max(round(cx_px / max(1, W - 1) * 1000), 0), 1000),
                                   min(max(round(cy_px / max(1, H - 1) * 1000), 0), 1000)]
                        end_n = [min(max(round(ex_px / max(1, W - 1) * 1000), 0), 1000),
                                 min(max(round(ey_px / max(1, H - 1) * 1000), 0), 1000)]
                        expected = {"action": "drag", "coordinate": start_n, "to": end_n}
                        dzh = "左" if abs(dx) >= abs(dy) and dx < 0 else ("右" if abs(dx) >= abs(dy) else ("下" if dy > 0 else "上"))
                        inst = rng.choice([f"按住「{task['label']}」向{dzh}拖动。",
                                           f"把「{task['label']}」往{dzh}拖拽。"])
                        before_hash = sess.cdp(JS_BODY_HASH)
                        before_url = str(sess.cdp("location.href") or "")
                        sess.mouse_path(cx_px, cy_px, vp_left, vp_top, dpr, rng)
                        sess.chrome.cdp.send("Input.dispatchMouseEvent",
                                             {"type": "mousePressed", "x": (cx_px - vp_left) / dpr,
                                              "y": (cy_px - vp_top) / dpr, "button": "left", "buttons": 1,
                                              "clickCount": 1})
                        time.sleep(0.12)
                        traj = sess.norm_traj(
                            sess.mouse_path(ex_px, ey_px, vp_left, vp_top, dpr, rng,
                                            step_s=0.02, pressed=True), W, H)
                        sess.chrome.cdp.send("Input.dispatchMouseEvent",
                                             {"type": "mouseReleased", "x": (ex_px - vp_left) / dpr,
                                              "y": (ey_px - vp_top) / dpr, "button": "left", "buttons": 0,
                                              "clickCount": 1})
                        time.sleep(1.0)
                        after_hash = sess.cdp(JS_BODY_HASH)
                        after_url = str(sess.cdp("location.href") or "")
                        changed = (before_hash and after_hash and before_hash[0] != after_hash[0]) \
                            or urldefrag(after_url)[0] != urldefrag(before_url)[0]
                        verify.update({"dx": dx, "dy": dy, "dom_changed": bool(changed)})
                        rec = {"bucket": "web_drag", "instruction": inst, "expected": expected,
                               "bbox": bn, "bbox_px": box, "target_desc": f"drag:{task['label']}",
                               "neg_subtype": None, "verified": bool(changed), "trajectory": traj}
                    elif task["kind"] == "right_click":
                        c = find_candidate(cands, task["match"])
                        if not c or not c.get("inview", True):
                            log.append({**ctx, "task": ti, "skip": f"target_missing:{task['match']}"})
                            continue
                        box = elem_box_px(c["rect"], vp_left, vp_top, dpr)
                        bn = px_box_to_norm(box, W, H)
                        expected = {"action": "right_click", "coordinate": center_norm(bn)}
                        inst = rng.choice([f"在「{task['label']}」上点击鼠标右键。",
                                           f"右键点一下页面里的「{task['label']}」。"])
                        sess.cdp("window.__ctxSeen=0;document.addEventListener('contextmenu',function(){window.__ctxSeen=1},{once:true,capture:true});1")
                        before_hash = sess.cdp(JS_BODY_HASH)
                        px = min(max(round(expected["coordinate"][0] / 1000 * (W - 1)), 0), W - 1)
                        py = min(max(round(expected["coordinate"][1] / 1000 * (H - 1)), 0), H - 1)
                        css_x, css_y = (px - vp_left) / dpr, (py - vp_top) / dpr
                        traj = sess.norm_traj(sess.mouse_path(px, py, vp_left, vp_top, dpr, rng), W, H)
                        for t in ("mousePressed", "mouseReleased"):
                            sess.chrome.cdp.send("Input.dispatchMouseEvent",
                                                 {"type": t, "x": css_x, "y": css_y, "button": "right",
                                                  "buttons": 2 if t == "mousePressed" else 0, "clickCount": 1})
                        time.sleep(0.8)
                        ctx_seen = bool(sess.cdp("window.__ctxSeen") or 0)
                        after_hash = sess.cdp(JS_BODY_HASH)
                        changed = ctx_seen or (before_hash and after_hash and before_hash[0] != after_hash[0])
                        verify.update({"ctx_event": ctx_seen, "dom_changed": bool(changed)})
                        rec = {"bucket": "web_right_click", "instruction": inst, "expected": expected,
                               "bbox": bn, "bbox_px": box, "target_desc": f"web:{task['label']}",
                               "verified": bool(changed), "trajectory": traj}
                    elif task["kind"] == "double_click":
                        c = find_candidate(cands, task["match"])
                        if not c or not c.get("inview", True):
                            log.append({**ctx, "task": ti, "skip": f"target_missing:{task['match']}"})
                            continue
                        box = elem_box_px(c["rect"], vp_left, vp_top, dpr)
                        bn = px_box_to_norm(box, W, H)
                        expected = {"action": "double_click", "coordinate": center_norm(bn)}
                        inst = rng.choice([f"双击「{task['label']}」。", f"在「{task['label']}」上双击鼠标左键。"])
                        before_hash = sess.cdp(JS_BODY_HASH)
                        before_url = str(sess.cdp("location.href") or "")
                        before_tabs = sess.target_ids()
                        px = min(max(round(expected["coordinate"][0] / 1000 * (W - 1)), 0), W - 1)
                        py = min(max(round(expected["coordinate"][1] / 1000 * (H - 1)), 0), H - 1)
                        css_x, css_y = (px - vp_left) / dpr, (py - vp_top) / dpr
                        traj = sess.norm_traj(sess.mouse_path(px, py, vp_left, vp_top, dpr, rng), W, H)
                        for cc in (1, 2):
                            for t in ("mousePressed", "mouseReleased"):
                                sess.chrome.cdp.send("Input.dispatchMouseEvent",
                                                     {"type": t, "x": css_x, "y": css_y, "button": "left",
                                                      "buttons": 1 if t == "mousePressed" else 0,
                                                      "clickCount": cc})
                        time.sleep(1.0)
                        sel = str(sess.cdp("(getSelection?getSelection().toString():'')") or "")
                        after_hash = sess.cdp(JS_BODY_HASH)
                        after_url = str(sess.cdp("location.href") or "")
                        after_tabs = sess.target_ids()
                        changed = (len(sel.strip()) >= 1
                                   or (before_hash and after_hash and before_hash[0] != after_hash[0])
                                   or urldefrag(after_url)[0] != urldefrag(before_url)[0]
                                   or bool(after_tabs - before_tabs))
                        verify.update({"selection": sel[:40], "dom_changed": bool(changed)})
                        rec = {"bucket": "web_double_click", "instruction": inst, "expected": expected,
                               "bbox": bn, "bbox_px": box, "target_desc": f"web:{task['label']}",
                               "verified": bool(changed), "trajectory": traj}
                    elif task["kind"] == "checkbox":
                        c = find_candidate(cands, task["match"])
                        if not c or not c.get("inview", True):
                            log.append({**ctx, "task": ti, "skip": f"target_missing:{task['match']}"})
                            continue
                        box = elem_box_px(c["rect"], vp_left, vp_top, dpr)
                        bn = px_box_to_norm(box, W, H)
                        coord = center_norm(bn)
                        expected = {"action": "left_click", "coordinate": coord}
                        inst = make_instruction_v2(rng, "web_click", label=task["label"], is_checkbox=True)
                        px = min(max(round(coord[0] / 1000 * (W - 1)), 0), W - 1)
                        py = min(max(round(coord[1] / 1000 * (H - 1)), 0), H - 1)
                        css_x, css_y = (px - vp_left) / dpr, (py - vp_top) / dpr
                        chk_js = ("(()=>{const e=document.elementFromPoint(%f,%f);"
                                  "const t=e&&(e.closest('input[type=checkbox],input[type=radio]')||e);"
                                  "return t&&'checked'in t?t.checked:null;})()")
                        before_chk = sess.cdp(chk_js % (css_x, css_y))
                        traj = sess.norm_traj(sess.mouse_path(px, py, vp_left, vp_top, dpr, rng), W, H)
                        for t in ("mousePressed", "mouseReleased"):
                            sess.chrome.cdp.send("Input.dispatchMouseEvent",
                                                 {"type": t, "x": css_x, "y": css_y, "button": "left",
                                                  "buttons": 1 if t == "mousePressed" else 0, "clickCount": 1})
                        time.sleep(0.5)
                        after_chk = sess.cdp(chk_js % (css_x, css_y))
                        flipped = before_chk is not None and after_chk is not None and before_chk != after_chk
                        verify.update({"before": before_chk, "after": after_chk, "flipped": bool(flipped)})
                        rec = {"bucket": "web_checkbox", "instruction": inst, "expected": expected,
                               "bbox": bn, "bbox_px": box, "target_desc": f"check:{task['label']}",
                               "verified": bool(flipped), "trajectory": traj}
                    elif task["kind"] == "scroll_click":
                        # two-sample chain: scroll-to-find (target offscreen) -> click it
                        c = find_candidate(cands, task["match"])
                        if not c:
                            log.append({**ctx, "task": ti, "skip": f"target_missing:{task['match']}"})
                            continue
                        if c.get("inview", True):
                            log.append({**ctx, "task": ti, "skip": f"already_inview:{task['match']}"})
                            continue
                        amount = int(task.get("amount", 4))
                        inst_a = rng.choice([f"向下滚动页面，找到「{task['label']}」。",
                                             f"把页面往下滚，找一下「{task['label']}」。"])
                        rec_a = {"bucket": "web_scroll_find", "instruction": inst_a,
                                 "expected": {"action": "scroll", "coordinate": [500, 600],
                                              "direction": "down", "amount": amount},
                                 "bbox": None, "bbox_px": None, "target_desc": f"scrollfind:{task['label']}",
                                 "verified": False}
                        before_y = float(sess.cdp("window.scrollY") or 0)
                        sess.mouse_path(W * 0.5, H * 0.6, vp_left, vp_top, dpr, rng)
                        tries = 0
                        found = None
                        while tries < int(task.get("max_wheels", 4)):
                            sess.chrome.cdp.send("Input.dispatchMouseEvent",
                                                 {"type": "mouseWheel", "x": W / 2 / dpr, "y": 0.6 * (H / dpr),
                                                  "deltaX": 0, "deltaY": 120 * amount})
                            time.sleep(0.5)
                            tries += 1
                            cands = sess.cdp(JS_CANDIDATES) or []
                            found = find_candidate(cands, task["match"])
                            if found and found.get("inview", False):
                                break
                        after_y = float(sess.cdp("window.scrollY") or 0)
                        ok_a = after_y > before_y + 30 and bool(found and found.get("inview"))
                        rec_a["verified"] = bool(ok_a)
                        emit(rec_a, {"kind": "scroll_find", "before_y": before_y,
                                     "after_y": after_y, "wheels": tries,
                                     "inview": bool(found and found.get("inview"))}, img, ti, 1)
                        if not (found and found.get("inview")):
                            log.append({**ctx, "task": ti, "skip": "never_inview"})
                            continue
                        try:
                            info, img = sess.shot()
                        except Exception:
                            break
                        cands = sess.cdp(JS_CANDIDATES) or []
                        vp_left, vp_top, dpr = sess.vp_map(info)
                        W, H = img.size
                        c = find_candidate(cands, task["match"])
                        if not c or not c.get("inview", True):
                            log.append({**ctx, "task": ti, "skip": "lost_after_scroll"})
                            continue
                        box = elem_box_px(c["rect"], vp_left, vp_top, dpr)
                        bn = px_box_to_norm(box, W, H)
                        expected = {"action": "left_click", "coordinate": center_norm(bn)}
                        inst = make_instruction_v2(rng, "web_click", label=task["label"])
                        before_url = str(sess.cdp("location.href") or "")
                        before_hash = sess.cdp(JS_BODY_HASH)
                        before_tabs = sess.target_ids()
                        px = min(max(round(expected["coordinate"][0] / 1000 * (W - 1)), 0), W - 1)
                        py = min(max(round(expected["coordinate"][1] / 1000 * (H - 1)), 0), H - 1)
                        css_x, css_y = (px - vp_left) / dpr, (py - vp_top) / dpr
                        traj = sess.norm_traj(sess.mouse_path(px, py, vp_left, vp_top, dpr, rng), W, H)
                        for t in ("mousePressed", "mouseReleased"):
                            sess.chrome.cdp.send("Input.dispatchMouseEvent",
                                                 {"type": t, "x": css_x, "y": css_y, "button": "left",
                                                  "buttons": 1 if t == "mousePressed" else 0, "clickCount": 1})
                        time.sleep(1.6)
                        after_url = str(sess.cdp("location.href") or "")
                        after_hash = sess.cdp(JS_BODY_HASH)
                        after_tabs = sess.target_ids()
                        landed = (urldefrag(after_url)[0] != urldefrag(before_url)[0]
                                  or bool(after_tabs - before_tabs)
                                  or (before_hash and after_hash and before_hash[0] != after_hash[0]))
                        rec = {"bucket": "web_click", "instruction": inst, "expected": expected,
                               "bbox": bn, "bbox_px": box, "target_desc": f"web:{task['label']}",
                               "verified": bool(landed), "trajectory": traj}
                        emit(rec, {"kind": "click", "before_url": before_url, "after_url": after_url,
                                   "new_tabs": len(after_tabs - before_tabs), "landed": landed}, img, ti, 2)
                        if after_tabs - before_tabs:
                            sess.close_extra_tabs(keep=before_tabs)
                            sess.sync_active_tab()
                        continue
                    elif task["kind"] == "hover_menu":
                        task.setdefault("label", task["match"])
                        c = find_candidate(cands, task["match"])
                        if not c or not c.get("inview", True):
                            log.append({**ctx, "task": ti, "skip": f"target_missing:{task['match']}"})
                            continue
                        box = elem_box_px(c["rect"], vp_left, vp_top, dpr)
                        bn = px_box_to_norm(box, W, H)
                        coord = center_norm(bn)
                        inst_a = rng.choice([f"把鼠标指针移到「{task['label']}」菜单上，先不要点击。",
                                             f"将鼠标悬停在「{task['label']}」上，等菜单展开。"])
                        rec_a = {"bucket": "web_move", "instruction": inst_a,
                                 "expected": {"action": "move", "coordinate": coord},
                                 "bbox": bn, "bbox_px": box, "target_desc": f"hover:{task['label']}",
                                 "verified": False}
                        px = min(max(round(coord[0] / 1000 * (W - 1)), 0), W - 1)
                        py = min(max(round(coord[1] / 1000 * (H - 1)), 0), H - 1)
                        traj_a = sess.norm_traj(sess.mouse_path(px, py, vp_left, vp_top, dpr, rng), W, H)
                        rec_a["trajectory"] = traj_a
                        time.sleep(0.7)
                        cands2 = sess.cdp(JS_CANDIDATES) or []
                        item_c = find_candidate(cands2, task["item"]) if task.get("item") else None
                        appeared = bool(item_c and item_c.get("inview", False))
                        rec_a["verified"] = appeared
                        emit(rec_a, {"kind": "hover", "item_appeared": appeared}, img, ti, 1)
                        if not item_c:
                            log.append({**ctx, "task": ti, "skip": f"menu_item_missing:{task.get('item')}"})
                            continue
                        try:
                            info, img = sess.shot()
                        except Exception:
                            break
                        cands2 = sess.cdp(JS_CANDIDATES) or []
                        vp_left, vp_top, dpr = sess.vp_map(info)
                        W, H = img.size
                        item_c = find_candidate(cands2, task["item"])
                        if not item_c or not item_c.get("inview", True):
                            log.append({**ctx, "task": ti, "skip": "menu_item_lost"})
                            continue
                        box = elem_box_px(item_c["rect"], vp_left, vp_top, dpr)
                        bn = px_box_to_norm(box, W, H)
                        expected = {"action": "left_click", "coordinate": center_norm(bn)}
                        inst = make_instruction_v2(rng, "web_click", label=task.get("item_label") or task["item"])
                        before_url = str(sess.cdp("location.href") or "")
                        before_hash = sess.cdp(JS_BODY_HASH)
                        before_tabs = sess.target_ids()
                        px = min(max(round(expected["coordinate"][0] / 1000 * (W - 1)), 0), W - 1)
                        py = min(max(round(expected["coordinate"][1] / 1000 * (H - 1)), 0), H - 1)
                        css_x, css_y = (px - vp_left) / dpr, (py - vp_top) / dpr
                        traj = sess.norm_traj(sess.mouse_path(px, py, vp_left, vp_top, dpr, rng), W, H)
                        for t in ("mousePressed", "mouseReleased"):
                            sess.chrome.cdp.send("Input.dispatchMouseEvent",
                                                 {"type": t, "x": css_x, "y": css_y, "button": "left",
                                                  "buttons": 1 if t == "mousePressed" else 0, "clickCount": 1})
                        time.sleep(1.6)
                        after_url = str(sess.cdp("location.href") or "")
                        after_hash = sess.cdp(JS_BODY_HASH)
                        after_tabs = sess.target_ids()
                        landed = (urldefrag(after_url)[0] != urldefrag(before_url)[0]
                                  or bool(after_tabs - before_tabs)
                                  or (before_hash and after_hash and before_hash[0] != after_hash[0]))
                        rec = {"bucket": "web_click", "instruction": inst, "expected": expected,
                               "bbox": bn, "bbox_px": box, "target_desc": f"menu:{task['item']}",
                               "verified": bool(landed), "trajectory": traj}
                        emit(rec, {"kind": "click", "before_url": before_url, "after_url": after_url,
                                   "new_tabs": len(after_tabs - before_tabs), "landed": landed,
                                   "via": "hover_menu"}, img, ti, 2)
                        if after_tabs - before_tabs:
                            sess.close_extra_tabs(keep=before_tabs)
                            sess.sync_active_tab()
                        continue
                    elif task["kind"] == "not_found_offscreen":
                        c = find_candidate(cands, task["match"])
                        ok = bool(c and not c.get("inview", True))
                        verify.update({"candidate_inview": c.get("inview") if c else None})
                        expected = {"action": "not_found",
                                    "reason": "目标控件在当前可视区域外，需先滚动页面"}
                        inst = make_instruction_v2(rng, "not_found_web", label=task["label"])
                        rec = {"bucket": "web_not_found", "instruction": inst, "expected": expected,
                               "bbox": None, "bbox_px": None, "target_desc": f"offscreen:{task['label']}",
                               "neg_subtype": "offscreen_target", "verified": ok}
                    elif task["kind"] == "not_found":
                        lbl = task["label"]
                        hit = find_candidate(cands, lbl)
                        absent = hit is None
                        verify.update({"candidate_hit": bool(hit)})
                        expected = {"action": "not_found",
                                    "reason": f"当前页面未出现「{lbl}」控件"}
                        inst = make_instruction_v2(rng, "not_found_web", label=lbl)
                        rec = {"bucket": "web_not_found", "instruction": inst, "expected": expected,
                               "bbox": None, "bbox_px": None, "target_desc": f"absent:{lbl}",
                               "neg_subtype": "absent_label", "verified": absent}
                    else:
                        log.append({**ctx, "task": ti, "skip": f"unknown_kind:{task['kind']}"})
                        continue

                    if rec is None:
                        continue
                    emit(rec, verify, img, ti)
                sess.close_extra_tabs()
            except Exception as exc:
                log.append({**ctx, "skip": f"site_error:{type(exc).__name__}:{exc}"})
                try:
                    sess.close()
                except Exception:
                    pass
                sess = Session(tempfile.TemporaryDirectory(
                    prefix="teacher_run_", ignore_cleanup_errors=True))
    finally:
        sess.close()
    (out / "run_log.json").write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[done] {samples_total} samples -> {rec_path}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("survey")
    p.add_argument("--out", required=True)
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--ids", default="")
    p.add_argument("--results", default=str(PROJECT_ROOT / "artifacts" / "cn500_20261125" / "results.jsonl"))
    r = sub.add_parser("run")
    r.add_argument("--plan", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--seed", type=int, default=20261125)
    r.add_argument("--skip-done", action="store_true")
    r.add_argument("--tag", default="")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass
    if args.cmd == "survey":
        return cmd_survey(args)
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
