"""Record the README demo: real panel + real HTTP MCP + real local model, on a local page in an isolated Chrome.

Nothing is mocked. An MCP client thread plays the upper model (one batched plan, one screen question), then an
instruction is typed into the floating bar and the high-risk guard stops it until it is confirmed. Only the demo
Chrome window is captured; the page is served from this script and has no real checkout behind it.

    .\\.venv\\Scripts\\python.exe .\\scripts\\record_demo.py            # record + encode assets/demo.gif
    .\\.venv\\Scripts\\python.exe .\\scripts\\record_demo.py --dry-run  # run the scenario only, print what happened
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from argparse import Namespace
from pathlib import Path
from typing import Any, Callable

import requests
import win32con
import win32gui
from PIL import Image, ImageDraw, ImageFont, ImageGrab

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
os.environ["CUA_PREWARM"] = "0"

from roi_utils import enable_per_monitor_dpi_awareness, get_dwm_extended_frame_bounds  # noqa: E402

enable_per_monitor_dpi_awareness()

from chrome_pilot_panel import ChromePilotPanel  # noqa: E402
from m35_e2e_eval import start_e2e_server  # noqa: E402

FPS = 10
WIN_POS, WIN_SIZE = (300, 120), (1100, 760)
CAPTION_H = 64
HUMAN_INSTRUCTION = "点击右侧橙色的按钮"

DEMO_PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>山野器物 · 手冲生活馆</title>
<style>
  * { box-sizing: border-box; }
  body { margin:0; background:#f3f1ec; color:#1f2933; font:14px/1.5 'Microsoft YaHei UI','Microsoft YaHei',sans-serif; }
  button, input { font:inherit; color:inherit; }
  button { cursor:pointer; }
  header { background:#fff; border-bottom:1px solid #e3ded5; padding:12px 24px; display:flex; align-items:center; gap:14px; }
  .brand { font-size:18px; font-weight:700; color:#22543d; margin-right:8px; }
  .search { flex:1; display:flex; gap:8px; max-width:430px; }
  .search input { flex:1; height:38px; border:1px solid #cfc8ba; border-radius:8px; padding:0 12px; background:#fff; }
  .search input:focus { outline:2px solid #2f855a; outline-offset:-1px; }
  .search button { height:38px; padding:0 16px; border:none; border-radius:8px; background:#22543d; color:#fff; }
  .ghost { height:38px; padding:0 14px; border:1px solid #cfc8ba; border-radius:8px; background:#fff; }
  .wrap { max-width:760px; margin:18px 0 0 24px; }
  .hint { color:#7b8794; font-size:13px; margin-bottom:12px; }
  .grid { display:grid; grid-template-columns:repeat(2, 1fr); gap:12px; }
  .card { background:#fff; border:1px solid #e3ded5; border-radius:10px; padding:14px 16px; display:flex; justify-content:space-between; align-items:center; }
  .cat { font-size:12px; color:#7b8794; }
  .price { color:#9b2c2c; font-weight:700; margin-top:4px; }
  #drawer { position:fixed; right:22px; top:76px; width:250px; background:#fff; border:1px solid #e3ded5; border-radius:12px; padding:16px;
            display:none; box-shadow:0 10px 28px rgba(31,41,51,.14); }
  #drawer ul { padding-left:18px; margin:8px 0 10px; }
  #total { display:flex; justify-content:space-between; border-top:1px solid #e3ded5; padding-top:10px; margin-bottom:12px; }
  #pay { width:100%; height:40px; border:none; border-radius:8px; background:#dd6b20; color:#fff; font-weight:700; }
  #paid { display:none; margin-top:10px; padding:8px 10px; border-radius:8px; background:#e6f4ea; color:#22543d; font-weight:600; text-align:center; }
  footer { position:fixed; left:24px; bottom:12px; color:#9aa5b1; font-size:12px; }
  .ripple { position:fixed; width:14px; height:14px; margin:-7px 0 0 -7px; border-radius:50%; border:2px solid #3182ce; background:rgba(49,130,206,.25);
            pointer-events:none; z-index:9999; animation:rip .7s ease-out forwards; }
  @keyframes rip { to { transform:scale(4.2); opacity:0; } }
</style>
<body>
  <header>
    <div class="brand">山野器物</div>
    <div class="search"><input id="q" type="search" placeholder="搜索滤杯、手冲壶或咖啡豆"><button id="go" type="button">搜好物</button></div>
    <button id="cart-btn" class="ghost" type="button" style="margin-left:auto">查看购物袋 (<span id="cnt">0</span>)</button>
  </header>
  <div class="wrap">
    <div class="hint" id="hint">全部商品 · 4 件</div>
    <div id="grid" class="grid"></div>
  </div>
  <div id="drawer"><b>购物袋清单</b><ul id="list"></ul>
    <div id="total"><span>合计</span><b id="sum">¥0</b></div>
    <button id="pay" type="button">确认支付</button>
    <div id="paid">已支付（演示页，无真实交易）</div>
  </div>
  <footer>本地演示页面 · 无真实商品与交易</footer>
  <script>
    const ITEMS = [
      { name:'陶瓷滤杯V60', cat:'手冲器具', price:88 },
      { name:'温控手冲壶', cat:'手冲器具', price:299 },
      { name:'埃塞俄比亚耶加雪菲', cat:'咖啡豆', price:68 },
      { name:'轻量钛金属随行杯', cat:'保温杯', price:198 },
    ];
    window.__STATE__ = { query:'', cart:[], drawerOpen:false, paid:false };
    const $ = (id) => document.getElementById(id);
    function drawCart() {
      const cart = window.__STATE__.cart;
      $('cnt').textContent = String(cart.length);
      $('list').innerHTML = cart.map(i => '<li>' + i.name + ' ¥' + i.price + '</li>').join('');
      $('sum').textContent = '¥' + cart.reduce((s, i) => s + i.price, 0);
    }
    function render() {
      const q = window.__STATE__.query;
      const shown = ITEMS.filter(x => !q || x.name.includes(q));
      $('hint').textContent = q ? '“' + q + '”的搜索结果 · ' + shown.length + ' 件' : '全部商品 · ' + shown.length + ' 件';
      $('grid').innerHTML = '';
      for (const item of shown) {
        const div = document.createElement('div');
        div.className = 'card';
        div.innerHTML = '<div><b>' + item.name + '</b><div class="cat">' + item.cat + '</div><div class="price">¥' + item.price + '</div></div>';
        const btn = document.createElement('button');
        btn.className = 'ghost'; btn.type = 'button'; btn.textContent = '加入购物袋';
        btn.addEventListener('click', () => { window.__STATE__.cart.push(item); drawCart(); });
        div.appendChild(btn);
        $('grid').appendChild(div);
      }
    }
    render();
    $('go').addEventListener('click', () => { window.__STATE__.query = $('q').value.trim(); render(); });
    $('cart-btn').addEventListener('click', () => {
      window.__STATE__.drawerOpen = !window.__STATE__.drawerOpen;
      $('drawer').style.display = window.__STATE__.drawerOpen ? 'block' : 'none';
      drawCart();
    });
    $('pay').addEventListener('click', () => { window.__STATE__.paid = true; $('paid').style.display = 'block'; });
    document.addEventListener('mousedown', (e) => {
      const r = document.createElement('div');
      r.className = 'ripple'; r.style.left = e.clientX + 'px'; r.style.top = e.clientY + 'px';
      document.body.appendChild(r);
      setTimeout(() => r.remove(), 750);
    }, true);
  </script>
</body></html>"""


class Recorder:
    """Grabs one screen rectangle at a steady pace; PNG writing happens off the capture thread."""

    def __init__(self, box: tuple[int, int, int, int], out_dir: Path) -> None:
        self.box, self.out_dir = box, out_dir
        self.frames: list[tuple[float, Path]] = []
        self._stop = threading.Event()
        self._q: queue.Queue[Any] = queue.Queue()
        self._threads = [threading.Thread(target=self._grab, daemon=True), threading.Thread(target=self._write, daemon=True)]
        self.t0 = 0.0

    def start(self) -> None:
        self.t0 = time.perf_counter()
        for th in self._threads:
            th.start()

    def now(self) -> float:
        return time.perf_counter() - self.t0

    def _grab(self) -> None:
        index = 0
        while not self._stop.is_set():
            due = self.t0 + index / FPS
            delay = due - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            stamp = self.now()
            img = ImageGrab.grab(bbox=self.box, include_layered_windows=True, all_screens=True)
            path = self.out_dir / f"raw_{index:05d}.png"
            self._q.put((img, path))
            self.frames.append((stamp, path))
            index = max(index + 1, int(self.now() * FPS))
        self._q.put(None)

    def _write(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            item[0].save(item[1], compress_level=1)

    def stop(self) -> None:
        self._stop.set()
        for th in self._threads:
            th.join(timeout=30)


def encode(rec: Recorder, captions: list[tuple[float, str, str]], out_dir: Path, gif_path: Path, width: int) -> dict[str, Any]:
    """Resample to a fixed frame rate, add the caption bar, then let ffmpeg build the palette and the GIF."""
    big = ImageFont.truetype("msyhbd.ttc", 21)
    small = ImageFont.truetype("msyh.ttc", 16)
    duration = rec.frames[-1][0]
    total = int(duration * FPS) + 1
    src = 0
    cache: tuple[Path, Image.Image] | None = None
    for i in range(total):
        t = i / FPS
        while src + 1 < len(rec.frames) and rec.frames[src + 1][0] <= t:
            src += 1
        path = rec.frames[src][1]
        if cache is None or cache[0] != path:
            cache = (path, Image.open(path).convert("RGB"))
        frame = cache[1]
        canvas = Image.new("RGB", (frame.width, frame.height + CAPTION_H), (18, 18, 20))
        canvas.paste(frame, (0, 0))
        line1, line2 = next(((a, b) for ts, a, b in reversed(captions) if ts <= t), ("", ""))
        draw = ImageDraw.Draw(canvas)
        draw.text((22, frame.height + (8 if line2 else 19)), line1, font=big, fill=(240, 240, 242))
        if line2:
            draw.text((22, frame.height + 37), line2, font=small, fill=(160, 166, 176))
        canvas.save(out_dir / f"out_{i:05d}.png", compress_level=1)
    pattern = str(out_dir / "out_%05d.png")
    scale = f"scale={width}:-1:flags=lanczos," if width else ""
    vf = f"fps={FPS},{scale}split[a][b];[a]palettegen=max_colors=200:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", pattern, "-filter_complex", vf, str(gif_path)], check=True)
    mp4 = out_dir / "demo.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", pattern, "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", str(mp4)], check=True)
    return {"frames_captured": len(rec.frames), "frames_out": total, "duration_s": round(duration, 1), "gif_bytes": gif_path.stat().st_size, "mp4_bytes": mp4.stat().st_size}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="run the scenario without recording")
    ap.add_argument("--out", type=Path, default=PROJECT_ROOT / "assets" / "demo.gif")
    ap.add_argument("--work", type=Path, default=PROJECT_ROOT / "artifacts" / "demo_recording")
    ap.add_argument("--width", type=int, default=0, help="output width in px; 0 keeps the captured size (sharpest text)")
    ap.add_argument("--vlm-port", type=int, default=18297)
    ap.add_argument("--mcp-port", type=int, default=18397)
    args = ap.parse_args()

    if args.work.exists():
        shutil.rmtree(args.work)
    args.work.mkdir(parents=True)
    server, base = start_e2e_server({"/store": DEMO_PAGE})
    profile = Path(tempfile.mkdtemp(prefix="cua_demo_profile_"))

    root = tk.Tk()
    panel = ChromePilotPanel(root, Namespace(port=args.vlm_port, mcp_port=args.mcp_port, max_image_tokens=1024, prompt_style="m1", quant="Q4_K_M"),
                             enable_mcp=True, sound_enabled=False, effects_enabled=False)
    assert panel.mcp_server is not None, panel.mcp_url
    engine = panel.engine
    # The panel binds to an already-open desktop Chrome when it finds one. The demo must never touch that window:
    # force the isolated managed session and refuse to continue (see managed_only) if anything switches it back.
    engine.chrome.profile_dir, engine.chrome.win_size = profile, WIN_SIZE
    panel._set_target_mode("managed")

    def managed_only() -> None:
        if engine.target_mode != "managed" or panel.target_mode != "managed":
            raise RuntimeError(f"refusing to act outside the isolated Chrome (engine={engine.target_mode}, panel={panel.target_mode})")

    managed_only()

    ui_q: queue.Queue[tuple[Callable[[], Any], threading.Event, list[Any]]] = queue.Queue()
    log: dict[str, Any] = {"events": []}
    captions: list[tuple[float, str, str]] = []
    holder: dict[str, Any] = {}

    def ui(fn: Callable[[], Any]) -> Any:
        """Run fn on the Tk thread and wait for it."""
        done, box = threading.Event(), []
        ui_q.put((fn, done, box))
        done.wait(20)
        return box[0] if box else None

    def pump_ui() -> None:
        while True:
            try:
                fn, done, box = ui_q.get_nowait()
            except queue.Empty:
                break
            try:
                box.append(fn())
            finally:
                done.set()
        if not holder.get("finished"):
            root.after(15, pump_ui)

    def now() -> float:
        rec = holder.get("rec")
        return rec.now() if rec else time.perf_counter() - holder["t0"]

    def cap(line1: str, line2: str = "") -> None:
        captions.append((now(), line1, line2))
        log["events"].append({"t": round(now(), 2), "caption": line1, "detail": line2})

    def wait_for(pred: Callable[[], bool], timeout: float, what: str) -> None:
        end = time.time() + timeout
        while not pred():
            if time.time() > end:
                raise TimeoutError(what)
            time.sleep(0.05)

    def press(button: tk.Widget) -> None:
        """Hover, press and release a panel button on the Tk thread, with the real handlers and their motion."""
        ui(lambda: button.event_generate("<Enter>"))
        time.sleep(0.35)
        ui(lambda: button.event_generate("<ButtonPress-1>", x=8, y=8))
        time.sleep(0.12)
        ui(lambda: button.event_generate("<ButtonRelease-1>", x=8, y=8))
        time.sleep(0.2)
        ui(lambda: button.event_generate("<Leave>"))

    def scenario() -> None:
        url = f"http://127.0.0.1:{args.mcp_port}/mcp"
        ids = iter(range(1, 1000))

        def rpc(method: str, params: dict[str, Any], headers: dict[str, str] | None = None) -> requests.Response:
            return requests.post(url, headers=headers or {}, json={"jsonrpc": "2.0", "id": next(ids), "method": method, "params": params}, timeout=120)

        def tool(name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            managed_only()
            started = time.perf_counter()
            resp = rpc("tools/call", {"name": name, "arguments": arguments}, session)
            content = resp.json()["result"]["content"]
            payload = json.loads(content[0]["text"])
            meta = {"tool": name, "seconds": round(time.perf_counter() - started, 2), "response_bytes": len(resp.content),
                    "images": sum(c.get("type") == "image" for c in content)}
            log["events"].append({"t": round(now(), 2), **meta, "status": payload.get("status"), "arguments": arguments})
            return payload, meta

        # ---- Not recorded: start the isolated Chrome, load the local model, place the two windows ----
        engine.chrome.ensure_started()
        hwnd = engine.chrome.hwnd
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, WIN_POS[0], WIN_POS[1], WIN_SIZE[0], WIN_SIZE[1], win32con.SWP_NOACTIVATE)
        log["model_load_s"] = round(engine.grounder.ensure_started(), 2)
        time.sleep(0.8)
        frame = get_dwm_extended_frame_bounds(hwnd)
        box = (frame.left, frame.top, frame.right, frame.bottom)
        log["capture_box"] = box

        def place_panel() -> tuple[int, int, int, int]:
            x = box[0] + (box[2] - box[0] - root.winfo_width()) // 2
            root.geometry(f"+{x}+{box[3] - 236}")
            root.update_idletasks()  # Tk drops a pending move if the z-order changes in the same breath
            top = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
            win32gui.SetWindowPos(top, win32con.HWND_TOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
            return root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height()

        px, py, pw, _ph = ui(place_panel)
        log["panel_rect"] = (px, py, pw, _ph)
        if not (box[0] <= px and px + pw <= box[2] and box[1] <= py and py + 200 <= box[3]):
            raise RuntimeError(f"panel {px, py, pw} is not inside the demo window {box}; it would show the desktop behind it")
        time.sleep(0.6)
        if not args.dry_run:
            holder["rec"] = Recorder(box, args.work)
            holder["rec"].start()

        # ---- 1. An upper model drives Chrome through MCP ----
        cap("隔离的托管 Chrome + 悬浮窗", "本地演示页面，真实的 MCP 服务与本地 2B 视觉模型，未加速")
        time.sleep(1.3)
        init = rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "demo-agent", "version": "1"}})
        session = {"Mcp-Session-Id": init.headers["Mcp-Session-Id"]}
        cap("① 上层模型通过 MCP 接入", "没有自带 computer use 的 harness 也能操作浏览器")
        time.sleep(1.8)
        nav, meta = tool("cua_navigate", {"action": "goto", "url": f"{base}/store"})
        cap("cua_navigate：打开页面，返回文字和可点元素", f"{meta['seconds']}s · 返回 {meta['response_bytes'] / 1024:.1f} KB 文本 · {meta['images']} 张截图")
        time.sleep(1.8)
        steps = [
            {"kind": "click_and_type", "instruction": "在搜索输入框", "text": "滤杯"},
            {"kind": "act", "text_hint": "搜好物"},
            {"kind": "act", "text_hint": "加入购物袋"},
            {"kind": "act", "text_hint": "查看购物袋"},
        ]
        cap("② 一次 cua_run_steps 调用，连续执行 4 步", "输入“滤杯” → 搜索 → 加入购物袋 → 打开购物袋")
        plan, meta = tool("cua_run_steps", {"steps": steps})
        usage = plan.get("local_usage") or {}
        cap(f"cua_run_steps → {plan.get('status')}", f"{meta['seconds']}s · 返回 {meta['response_bytes'] / 1024:.1f} KB 文本 · {meta['images']} 张截图 · 本地模型 {usage.get('calls', 0)} 次")
        log["plan_trace"] = plan.get("trace")
        time.sleep(2.4)
        question = "购物袋清单里有什么商品？合计多少钱？"
        cap("③ cua_ask_screen：让本地模型读屏，只回传文字", f"问：{question}")
        ans, meta = tool("cua_ask_screen", {"question": question})
        cap("③ cua_ask_screen：让本地模型读屏，只回传文字", f"答：{str(ans.get('answer', '')).strip()[:60]}（{meta['seconds']}s）")
        time.sleep(3.0)

        # ---- 2. A person types into the bar; the guard stops the risky click ----
        managed_only()
        cap("④ 也可以直接在悬浮窗里下指令", "")
        ui(lambda: panel._on_entry_focus_in(None))
        time.sleep(0.4)
        for ch in HUMAN_INSTRUCTION:
            ui(lambda c=ch: panel.entry.insert("end", c))
            time.sleep(0.11)
        time.sleep(0.5)
        press(panel.run_btn)
        wait_for(lambda: panel._confirm_visible, 40, "confirmation did not appear")
        cap("⑤ 本地模型定位到的按钮是“确认支付” → 被拦下", "指令里没有敏感词；拦截依据是定位到的目标。未点击，等人确认")
        time.sleep(4.2)
        log["state_before_confirm"] = engine.chrome.cdp.evaluate("JSON.stringify({paid: window.__STATE__.paid, cart: window.__STATE__.cart.map(i => i.name), query: window.__STATE__.query})")
        managed_only()
        press(panel.confirm_yes)
        cap("⑥ 人点了“确认执行”，才真正点击", "")
        wait_for(lambda: not panel.busy and not panel._confirm_visible, 40, "confirmed action did not finish")
        time.sleep(0.6)
        log["state_after_confirm"] = engine.chrome.cdp.evaluate("JSON.stringify({paid: window.__STATE__.paid, cart: window.__STATE__.cart.map(i => i.name), query: window.__STATE__.query})")
        log["panel_status_after"] = panel.status_label.cget("text")
        time.sleep(2.6)

    def run() -> None:
        try:
            scenario()
        except Exception as exc:  # the recording is still worth inspecting
            log["error"] = repr(exc)
        finally:
            if holder.get("rec"):
                holder["rec"].stop()
            holder["finished"] = True
            root.after(0, panel.close)

    holder["t0"] = time.perf_counter()
    root.after(15, pump_ui)
    threading.Thread(target=run, daemon=True).start()
    root.mainloop()
    server.shutdown()
    server.server_close()
    shutil.rmtree(profile, ignore_errors=True)

    if holder.get("rec") and holder["rec"].frames:
        log["encode"] = encode(holder["rec"], captions, args.work, args.out, args.width)
        for p in args.work.glob("raw_*.png"):
            p.unlink()
    (args.work / "demo_log.json").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in log.items() if k != "plan_trace"}, ensure_ascii=False, indent=1))
    return 1 if "error" in log else 0


if __name__ == "__main__":
    raise SystemExit(main())
