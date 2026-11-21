"""End-to-end verification for Chrome Pilot CUA engine, dual-transport MCP server, and modern UI."""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw, ImageFont, ImageGrab, ImageTk
import requests
import win32gui
import win32ui

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# Keep the suite deterministic even if the user opted in globally: no background llama-server loads unless a test asks.
os.environ["CUA_PREWARM"] = "0"

from chrome_cua_mcp import MCPServerCore, start_http_mcp_background
from chrome_pilot_panel import AMBER, CYAN, ChromePilotPanel, set_win11_border_color
from cua_effects import (
    _user32,
    build_glow_strip_frames,
    create_hcursor_from_rgba,
    render_ai_cursor_rgba,
    save_effects_previews,
    ScreenEdgeGlowOverlay,
)
import cua_engine
from cua_engine import (
    ChromeCUAEngine,
    VLMCallError,
    _accumulate_usage,
    _classify_desktop_submission,
    _classify_managed_submission,
    _merge_local_usage,
    _new_local_usage,
)
from desktop_perceiver import PersistentWinRTOCR, analyze_click_effect, match_ocr_target
from m3_auto_collect_chrome import DEFAULT_CHROME, launch_chrome_session

TEST_HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>CUA 自动化验收页</title>
<style>
  body { margin:0; font-family:'Microsoft YaHei UI',sans-serif; background:#f5f7fb; color:#18212f; }
  .wrap { max-width:780px; margin:28px auto; padding:0 20px; }
  .card { background:#fff; border:1px solid #dce3ee; border-radius:12px; padding:20px; display:grid; gap:14px; }
  label { display:block; font-size:13px; color:#5b6b80; margin-bottom:5px; }
  input { width:100%; height:38px; border:1px solid #cbd5e1; border-radius:8px; padding:0 12px; font-size:14px; }
  .btn-row { display:flex; gap:12px; align-items:center; margin-top:6px; }
  .primary { padding:9px 20px; border:none; border-radius:8px; background:#2563eb; color:#fff; font-weight:600; cursor:pointer; }
  .danger { padding:9px 20px; border:none; border-radius:8px; background:#dc2626; color:#fff; font-weight:600; cursor:pointer; }
  a.tiny { color:#2563eb; font-size:13px; margin-left:auto; }
  #promo-modal { position:fixed; inset:0; background:rgba(15,23,42,.55); display:flex; align-items:center; justify-content:center; z-index:999; }
  .modal-box { width:360px; background:#fff; border-radius:12px; padding:22px; text-align:center; box-shadow:0 12px 32px rgba(0,0,0,.22); }
</style>
<body>
  <div class="wrap">
    <div class="card">
      <h2 style="margin:0">订单确认与草稿测试</h2>
      <div><label for="customer-name">收件人姓名</label><input id="customer-name" type="text" placeholder="请输入收件人"></div>
      <div><label for="order-note">配送备注</label><input id="order-note" type="text" placeholder="例如放快递柜"></div>
      <div class="btn-row">
        <button id="save-draft" class="primary" type="button">保存草稿</button>
        <button id="pay-btn" class="danger" type="button">确认支付</button>
        <a id="tiny-link" class="tiny" href="#details">查看明细</a>
      </div>
      <div id="status-banner">状态：待保存 · 未支付</div>
      <div id="shadow-host" style="margin-top:4px"></div>
      <form id="search-form" action="#search" style="display:flex;gap:8px;align-items:center">
        <input id="search-box" type="search" placeholder="站内搜索" style="flex:1">
        <button id="search-go" type="submit" class="primary">Go</button>
      </form>
      <div id="search-result">搜索状态：空闲</div>
    </div>
    <div style="height:1100px"></div>
    <div id="bottom-marker" class="card">底部锚点：滚动可达</div>
  </div>
  <div id="promo-modal" role="dialog" aria-modal="true">
    <div class="modal-box">
      <h3 style="margin:0 0 8px">限时活动提醒</h3>
      <p style="margin:0 0 16px;color:#475569;font-size:13px">测试弹窗自动检测与关闭功能。</p>
      <button id="close-promo" class="primary" type="button">暂不需要</button>
    </div>
  </div>
  <script>
    const sh = document.getElementById('shadow-host').attachShadow({ mode: 'open' });
    sh.innerHTML = '<button id="shadow-btn" type="button" style="padding:6px 12px;border:1px solid #cbd5e1;border-radius:6px;background:#eef2ff;color:#1e40af;cursor:pointer">影子按钮</button>';
    window.__TEST__ = { modalClosed:false, saved:false, name:'', note:'', paid:false };
    function refreshBanner() {
      const s = window.__TEST__.saved ? ('已保存草稿：' + window.__TEST__.name + '/' + window.__TEST__.note) : '待保存';
      const p = window.__TEST__.paid ? '已支付' : '未支付';
      document.getElementById('status-banner').textContent = '状态：' + s + ' · ' + p;
    }
    document.getElementById('close-promo').addEventListener('click', () => {
      window.__TEST__.modalClosed = true;
      document.getElementById('promo-modal').style.display = 'none';
    });
    document.getElementById('save-draft').addEventListener('click', () => {
      window.__TEST__.saved = true;
      window.__TEST__.name = document.getElementById('customer-name').value.trim();
      window.__TEST__.note = document.getElementById('order-note').value.trim();
      refreshBanner();
    });
    document.getElementById('pay-btn').addEventListener('click', () => {
      window.__TEST__.paid = true;
      refreshBanner();
    });
    document.getElementById('search-form').addEventListener('submit', (e) => {
      e.preventDefault();
      const q = document.getElementById('search-box').value.trim();
      location.hash = 'q=' + encodeURIComponent(q);
      document.getElementById('search-result').textContent = '搜索结果：' + q;
    });
  </script>
</body></html>"""


P1_EDGES_HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>P1 边界与负例测试页</title>
<style>
  body { margin:0; font-family:'Microsoft YaHei UI',sans-serif; background:#f8fafc; color:#0f172a; }
  .wrap { max-width:760px; margin:20px auto; padding:0 18px; display:grid; gap:12px; }
  .card { background:#fff; border:1px solid #cbd5e1; border-radius:10px; padding:14px; display:grid; gap:10px; }
  input, textarea { width:100%; border:1px solid #cbd5e1; border-radius:8px; padding:8px 10px; font-size:14px; box-sizing:border-box; }
  .btn { padding:8px 16px; border:none; border-radius:8px; background:#2563eb; color:#fff; cursor:pointer; width:fit-content; }
  .highlight { background:#fef08a; border-color:#ca8a04; }
</style>
<body>
  <div class="wrap">
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <b>P1 边界测试</b>
        <span id="tick-clock">12:00</span>
      </div>
      <div id="tick-box" class="card" style="padding:8px">时钟与样式状态区</div>
      <div id="banner-tip">默认提示文案</div>
      <input id="plain-input" type="text" placeholder="非提交输入框">
      <button id="draft-btn" class="btn" type="button">保存订单</button>
      <div id="draft-status">状态：待保存</div>
      <textarea id="multiline-box" rows="3" placeholder="多行备注"></textarea>
      <input id="async-box" type="text" placeholder="异步搜索">
      <div id="async-result">异步结果：空</div>
      <input id="clock-input" type="text" placeholder="时钟干扰输入框">
      <input id="banner-input" type="text" placeholder="横幅文案干扰输入框">
      <form id="search-form" action="#search" style="display:flex;gap:8px">
        <input id="search-input" type="search" placeholder="表单搜索框">
        <button type="submit" class="btn">提交</button>
      </form>
      <div id="search-out">表单结果：空闲</div>
    </div>
  </div>
  <script>
    window.__P1__ = { draftClicks: 0, asyncCalls: 0, formSubmits: 0 };
    document.getElementById('draft-btn').addEventListener('click', () => {
      window.__P1__.draftClicks += 1;
      document.getElementById('draft-status').textContent = '状态：已错误点击保存';
    });
    document.getElementById('async-box').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        const v = e.target.value.trim();
        window.__P1__.asyncCalls += 1;
        setTimeout(() => {
          document.getElementById('async-result').textContent = '异步命中：' + v;
        }, 380);
      }
    });
    document.getElementById('clock-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        document.getElementById('tick-clock').textContent = '12:01';
        document.getElementById('tick-box').classList.toggle('highlight');
      }
    });
    document.getElementById('banner-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        document.getElementById('banner-tip').textContent = '输入区已获得焦点';
      }
    });
    document.getElementById('search-form').addEventListener('submit', (e) => {
      e.preventDefault();
      window.__P1__.formSubmits += 1;
      const v = document.getElementById('search-input').value.trim();
      location.hash = 'q=' + encodeURIComponent(v);
      document.getElementById('search-out').textContent = '表单结果：' + v;
    });
  </script>
</body></html>"""

P1_POPUP_VISION_HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>视觉弹窗自愈页</title>
<style>
  body { margin:0; font-family:'Microsoft YaHei UI',sans-serif; background:#f5f7fb; }
  #opaque-modal { position:fixed; inset:0; background:rgba(15,23,42,.55); display:flex; align-items:center; justify-content:center; z-index:999; }
  .box { width:340px; background:#fff; border-radius:12px; padding:24px; text-align:center; }
  #close-icon { padding:10px 28px; border:none; border-radius:8px; background:#2563eb; color:#fff; font-size:15px; cursor:pointer; }
</style>
<body>
  <div style="padding:30px"><h2>视觉弹窗底层内容</h2><div id="heal-marker">目标内容已可见</div></div>
  <div id="opaque-modal" role="dialog" aria-modal="true">
    <div class="box">
      <p style="margin:0 0 14px">无常见关闭文案的弹窗</p>
      <button id="close-icon" type="button">继续</button>
    </div>
  </div>
  <script>
    window.__MODAL_CLOSED__ = false;
    document.getElementById('close-icon').addEventListener('click', () => {
      window.__MODAL_CLOSED__ = true;
      document.getElementById('opaque-modal').style.display = 'none';
    });
  </script>
</body></html>"""


P1_ADVERSARIAL_HTML = """<!doctype html><html lang="en"><meta charset="utf-8"><title>Adversarial Submit Test</title>
<body>
  <h1>Submission test</h1><p>READY-FOUND</p>
  <input id="adv-links" type="text" placeholder="Test links input">
  <input id="adv-pre" type="text" placeholder="Test pre input">
  <input id="adv-echo" type="text" placeholder="Test echo input">
  <button type="button" id="adv-next">Continue</button>
  <div id="adv-noise"></div>
  <script>
    window.__ADV__ = { submitted: false, continued: false };
    document.getElementById('adv-links').addEventListener('keydown', e => {
      if (e.key === 'Enter') {
        setTimeout(() => {
          const d = document.getElementById('adv-noise');
          for (let i = 0; i < 4; i++) {
            const a = document.createElement('a');
            a.textContent = 'Unrelated link ' + i;
            a.href = '#noise-' + i;
            d.appendChild(a);
          }
        }, 250);
      }
    });
    document.getElementById('adv-echo').addEventListener('keydown', e => {
      if (e.key === 'Enter') {
        setTimeout(() => {
          document.getElementById('adv-noise').textContent = 'Draft preview: gamma (not submitted)';
        }, 250);
      }
    });
    document.getElementById('adv-next').addEventListener('click', () => {
      window.__ADV__.continued = true;
    });
  </script>
</body></html>"""


def start_fixture_http() -> tuple[ThreadingHTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/p1_edges":
                html = P1_EDGES_HTML
            elif path == "/p1_popup_vision":
                html = P1_POPUP_VISION_HTML
            elif path == "/p1_adversarial":
                html = P1_ADVERSARIAL_HTML
            else:
                html = TEST_HTML
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/cua_test"


def test_stdio_mcp(test_url: str) -> None:
    print("[1/4] Running Stdio MCP end-to-end test...")
    with tempfile.TemporaryDirectory(prefix="cua_stdio_prof_", ignore_cleanup_errors=True) as tmp:
        proc = subprocess.Popen(
            [
                str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"),
                str(PROJECT_ROOT / "scripts" / "chrome_cua_mcp.py"),
                "--stdio",
                "--vlm-port",
                "18088",
                "--profile-dir",
                tmp,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        assert proc.stdin is not None and proc.stdout is not None
        seq = 0

        def rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            nonlocal seq
            seq += 1
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": seq, "method": method, "params": params or {}}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            assert line, f"Stdio EOF on {method}: {proc.stderr.read() if proc.stderr else ''}"
            return json.loads(line)

        def call_tool(name: str, arguments: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            resp = rpc("tools/call", {"name": name, "arguments": arguments or {}})
            assert "result" in resp, resp
            res = resp["result"]
            assert not res.get("isError"), res
            content = res["content"]
            payload = json.loads(content[0]["text"])
            return payload, content

        try:
            init = rpc("initialize", {"clientInfo": {"name": "Antigravity", "version": "2.0"}})["result"]
            assert init["protocolVersion"] == "2025-03-26"
            assert init["serverInfo"]["name"] == "chrome-pilot-cua"
            assert isinstance(init.get("instructions"), str) and "cua_ask_screen" in init["instructions"], init

            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            proc.stdin.flush()
            assert rpc("ping")["result"] == {}

            tools = rpc("tools/list")["result"]["tools"]
            names = [t["name"] for t in tools]
            assert names == [
                "cua_observe",
                "cua_navigate",
                "cua_act",
                "cua_run_steps",
                "cua_ask_screen",
                "cua_locate",
                "cua_dismiss_popup",
                "cua_extract",
                "cua_status",
                "cua_task",
            ], names

            st, _ = call_tool("cua_status")
            assert "M3.5" in st["model"], st
            assert st["active_harness"] == "Antigravity" and st["harness_mode"] == "single", st

            t_start, _ = call_tool("cua_task", {"event": "start", "title": "测试订单任务", "total_steps": 3, "play_sound": False})
            assert t_start["task_state"] == "running" and t_start["task_total_steps"] == 3, t_start

            # Default zero-screenshot navigation returns text & elements directly
            nav, nav_content = call_tool("cua_navigate", {"action": "goto", "url": test_url})
            assert nav["status"] == "ok", nav
            assert len(nav_content) == 1, f"Expected text-only default, got {len(nav_content)} content blocks"
            assert "订单确认" in nav.get("text_excerpt", ""), nav

            # Explicit JPEG vs PNG comparison
            obs, content = call_tool("cua_observe", {"include_screenshot": True, "annotate_som": True})
            assert len(content) == 2 and content[1]["type"] == "image" and content[1]["mimeType"] == "image/jpeg"
            jpg_bytes = base64.b64decode(content[1]["data"])
            assert jpg_bytes.startswith(b"\xff\xd8\xff") and len(jpg_bytes) > 1000
            assert obs["popup_detected"] is True and obs["popup_close_ids"], obs

            obs_png, png_content = call_tool("cua_observe", {"include_screenshot": True, "screenshot_format": "png"})
            assert len(png_content) == 2 and png_content[1]["mimeType"] == "image/png"
            png_bytes = base64.b64decode(png_content[1]["data"])
            assert png_bytes.startswith(b"\x89PNG") and len(jpg_bytes) < len(png_bytes), (len(jpg_bytes), len(png_bytes))

            dis, _ = call_tool("cua_dismiss_popup")
            assert dis["status"] == "ok" and dis["popup_detected"] is False, dis

            obs_after, obs_after_content = call_tool("cua_observe")
            assert len(obs_after_content) == 1
            assert obs_after["popup_detected"] is False and len(obs_after["elements"]) >= 6, obs_after
            assert all("rect_css" not in el for el in obs_after["elements"]), obs_after["elements"][0]
            assert any(e["text"] == "影子按钮" for e in obs_after["elements"]), obs_after["elements"]

            # Atomic composite run_steps (click_and_type + text_hint + verify_question, 0 images)
            run_res, run_content = call_tool(
                "cua_run_steps",
                {
                    "steps": [
                        {
                            "kind": "click_and_type",
                            "instruction": "在「收件人姓名」输入框",
                            "text": "陈知行",
                            "clear_first": True,
                            "press_enter": False,
                        },
                        {
                            "kind": "click_and_type",
                            "instruction": "在「配送备注」输入框",
                            "text": "放门卫处",
                            "clear_first": True,
                            "press_enter": False,
                        },
                        {"kind": "act", "text_hint": "保存草稿"},
                    ],
                    "auto_dismiss_popup": False,
                    "include_screenshot": False,
                    "verify_question": "页面状态栏现在显示什么内容？简短回答。",
                },
            )
            assert run_res["status"] == "completed" and run_res["executed_steps"] == 3, run_res
            assert len(run_content) == 1, run_content
            assert "已保存草稿：陈知行/放门卫处" in run_res.get("page_text", ""), run_res
            assert run_res.get("verification_answer"), run_res
            assert run_res.get("local_usage", {}).get("calls", 0) >= 2 and run_res["local_usage"].get("prompt_tokens", 0) > 0, run_res

            ask_res, ask_content = call_tool(
                "cua_ask_screen",
                {"question": "目前是否已经支付？直接回答未支付或已支付。"},
            )
            assert len(ask_content) == 1 and ask_res["status"] == "ok" and ask_res.get("answer"), ask_res
            assert ask_res.get("local_usage", {}).get("calls") == 1 and ask_res["local_usage"].get("prompt_tokens", 0) > 0, ask_res

            # Test click_and_type(press_enter=True) form submission via CDP send_key
            search_el = next((e for e in obs_after["elements"] if e.get("placeholder") == "站内搜索"), None)
            assert search_el is not None, obs_after["elements"]
            form_res, _ = call_tool(
                "cua_run_steps",
                {
                    "steps": [
                        {
                            "kind": "click_and_type",
                            "element_id": search_el["id"],
                            "text": "asyncio",
                            "clear_first": True,
                            "press_enter": True,
                            "wait_settle": True,
                        }
                    ],
                    "auto_dismiss_popup": False,
                    "include_screenshot": False,
                },
            )
            assert form_res["status"] == "completed", form_res
            assert form_res["trace"][0].get("submitted") is True, form_res
            assert "搜索结果：asyncio" in form_res.get("page_text", ""), form_res
            assert "#q=asyncio" in form_res.get("final_url", ""), form_res

            ext1, _ = call_tool("cua_extract")
            assert "已保存草稿：陈知行/放门卫处" in ext1["text"], ext1
            assert "未支付" in ext1["text"], ext1
            assert "影子按钮" in ext1["text"], ext1

            loc, _ = call_tool("cua_locate", {"instruction": "点击「查看明细」链接", "zoom": True})
            assert loc["zoomed"] is True and loc["action"] == "left_click" and loc["coordinate"] is not None, loc
            assert loc.get("local_usage", {}).get("calls", 0) >= 1 and loc["local_usage"].get("prompt_tokens", 0) > 0, loc

            blocked, _ = call_tool("cua_act", {"instruction": "点击「确认支付」按钮"})
            assert blocked["status"] == "needs_human_confirmation" and blocked.get("confirm_token"), blocked
            ext2, _ = call_tool("cua_extract")
            assert "未支付" in ext2["text"], f"Red-tier leaked real click: {ext2}"

            confirmed, conf_content = call_tool(
                "cua_act",
                {"instruction": "点击「确认支付」按钮", "confirm_token": blocked["confirm_token"], "include_screenshot": True},
            )
            assert confirmed["status"] == "ok", confirmed
            assert confirmed.get("local_usage", {}).get("calls", 0) >= 1 and confirmed["local_usage"].get("prompt_tokens", 0) > 0, confirmed
            assert len(conf_content) == 2 and conf_content[1]["type"] == "image" and conf_content[1]["mimeType"] == "image/jpeg", conf_content
            ext3, _ = call_tool("cua_extract")
            assert "已支付" in ext3["text"], ext3

            # Verify coordinate-free scroll & scroll_until in cua_run_steps
            scroll_res, _ = call_tool(
                "cua_run_steps",
                {
                    "steps": [
                        {"kind": "scroll", "direction": "down", "amount": 5},
                        {"kind": "scroll_until", "until_text": "底部锚点：滚动可达", "max_scrolls": 4},
                    ],
                    "auto_dismiss_popup": False,
                    "include_screenshot": False,
                },
            )
            assert scroll_res["status"] == "completed" and "底部锚点：滚动可达" in scroll_res.get("page_text", ""), scroll_res

            t_done, _ = call_tool("cua_task", {"event": "done", "message": "验收完成", "play_sound": False})
            assert t_done["task_state"] == "done" and t_done["task_step_label"] == "验收完成", t_done
            print(f"  [PASS] Stdio MCP verified (JPEG={len(jpg_bytes)}B vs PNG={len(png_bytes)}B, ask_infer={ask_res['infer_s']}s).")
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.wait(timeout=15)


def test_http_mcp(test_url: str) -> None:
    print("[2/4] Running Streamable HTTP MCP + Desktop mode test...")
    with (
        tempfile.TemporaryDirectory(prefix="cua_http_prof_", ignore_cleanup_errors=True) as tmp,
        tempfile.TemporaryDirectory(prefix="cua_ext_desktop_", ignore_cleanup_errors=True) as ext_tmp,
    ):
        engine = ChromeCUAEngine(profile_dir=Path(tmp), vlm_port=18089, mcp_port=18395)
        server, _th, url = start_http_mcp_background(engine, port=18395)
        ext_proc = None
        try:
            health = requests.get("http://127.0.0.1:18395/health", timeout=5)
            assert health.status_code == 200 and health.json()["name"] == "chrome-pilot-cua", health.text

            get_mcp = requests.get(url, timeout=5)
            assert get_mcp.status_code == 405 and "POST" in get_mcp.headers.get("Allow", ""), (get_mcp.status_code, get_mcp.headers)

            r1 = requests.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "Codex", "version": "1.0"}}},
                timeout=5,
            )
            assert r1.status_code == 200
            sid = r1.headers.get("Mcp-Session-Id")
            assert sid, r1.headers

            r2 = requests.post(url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers={"Mcp-Session-Id": sid}, timeout=5)
            assert r2.status_code == 200 and len(r2.json()["result"]["tools"]) == 10

            def http_call(req_id: int, tool_name: str, arguments: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
                resp = requests.post(
                    url,
                    json={"jsonrpc": "2.0", "id": req_id, "method": "tools/call", "params": {"name": tool_name, "arguments": arguments or {}}},
                    headers={"Mcp-Session-Id": sid},
                    timeout=90,
                )
                assert resp.status_code == 200, resp.text
                res = resp.json()["result"]
                assert not res.get("isError"), res
                content = res["content"]
                return json.loads(content[0]["text"]), content

            st, _ = http_call(3, "cua_status")
            assert st["mcp_clients"] >= 1 and st["active_harness"] == "Codex" and st["harness_mode"] == "single", st

            ext_proc, ext_hwnd, _ext_port = launch_chrome_session(
                DEFAULT_CHROME,
                Path(ext_tmp),
                (1280, 820),
                scale=1.0,
                dark_mode=False,
                start_url=test_url,
            )
            time.sleep(1.5)

            lw, _ = http_call(4, "cua_navigate", {"action": "list_windows"})
            assert any(w["hwnd"] == ext_hwnd for w in lw["desktop_windows"]), lw

            sm, _ = http_call(5, "cua_navigate", {"action": "set_mode", "mode": "desktop", "hwnd": ext_hwnd})
            assert sm["target_mode"] == "desktop" and sm["desktop_hwnd"] == ext_hwnd, sm

            st_desk, _ = http_call(6, "cua_status")
            assert st_desk["target_mode"] == "desktop" and st_desk["desktop_hwnd"] == ext_hwnd, st_desk

            obs_desk, obs_content = http_call(7, "cua_observe")
            assert obs_desk["status"] == "ok" and obs_desk["mode"] == "desktop_vision" and obs_desk["hwnd"] == ext_hwnd, obs_desk
            assert len(obs_content) == 1, f"Expected text-first desktop observe, got {len(obs_content)} blocks"
            assert "elements" not in obs_desk, "Duplicate elements array leaked into desktop observe"
            assert obs_desk.get("ocr_elements"), obs_desk
            assert all("center_norm" not in el for el in obs_desk["ocr_elements"]), obs_desk["ocr_elements"][0]
            assert any(k in obs_desk.get("page_text", "") for k in ("限时活动提醒", "暂不需要", "订单确认")), obs_desk
            assert "127.0.0.1" in obs_desk.get("url", "") or "cua_test" in obs_desk.get("url", ""), obs_desk
            obs_desk_bytes = len(obs_content[0]["text"].encode("utf-8"))

            obs_jpg, jpg_content = http_call(8, "cua_observe", {"include_screenshot": True})
            assert len(jpg_content) == 2 and jpg_content[1]["mimeType"] == "image/jpeg"
            jpg_bytes = base64.b64decode(jpg_content[1]["data"])
            assert jpg_bytes.startswith(b"\xff\xd8\xff")

            obs_png, png_content = http_call(9, "cua_observe", {"include_screenshot": True, "screenshot_format": "png"})
            assert len(png_content) == 2 and png_content[1]["mimeType"] == "image/png"
            png_bytes = base64.b64decode(png_content[1]["data"])
            assert png_bytes.startswith(b"\x89PNG") and len(jpg_bytes) < len(png_bytes), (len(jpg_bytes), len(png_bytes))

            ext_desk, ext_content = http_call(10, "cua_extract")
            assert len(ext_content) == 1
            assert ext_desk["mode"] == "desktop_vision" and "note" in ext_desk, ext_desk
            assert ext_desk.get("text") and ext_desk.get("ocr_elements"), ext_desk

            ask_desk, ask_content = http_call(
                11,
                "cua_ask_screen",
                {"question": "弹窗里蓝色按钮上写着什么文字？请简短回答。"},
            )
            assert len(ask_content) == 1 and ask_desk["status"] == "ok" and ask_desk.get("answer") and ask_desk["infer_s"] > 0, ask_desk

            loc_desk, _ = http_call(12, "cua_locate", {"instruction": "点击「暂不需要」按钮", "zoom": False})
            assert loc_desk["status"] == "ok" and loc_desk["mode"] == "desktop", loc_desk
            assert loc_desk["action"] == "left_click" and loc_desk["coordinate"] is not None, loc_desk

            back, _ = http_call(13, "cua_navigate", {"action": "set_mode", "mode": "managed"})
            assert back["target_mode"] == "managed", back

            r4 = requests.delete(url, headers={"Mcp-Session-Id": sid}, timeout=5)
            assert r4.status_code == 200 and r4.json()["closed"] is True
            print(
                f"  [PASS] Streamable HTTP MCP + Desktop OCR/UIA verified at {url} "
                f"(ocr={obs_desk['ocr_ms']}ms, obs_json={obs_desk_bytes}B, JPEG={len(jpg_bytes)}B vs PNG={len(png_bytes)}B, ask={ask_desk['infer_s']}s)."
            )
        finally:
            if ext_proc is not None and ext_proc.poll() is None:
                ext_proc.terminate()
                try:
                    ext_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    ext_proc.kill()
            server.shutdown()
            server.server_close()
            engine.close()


def _capture_hwnd(hwnd: int, out_path: Path) -> None:
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = max(1, r - l), max(1, b - t)
    hdc = win32gui.GetWindowDC(hwnd)
    src = win32ui.CreateDCFromHandle(hdc)
    dst = src.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    try:
        bmp.CreateCompatibleBitmap(src, w, h)
        dst.SelectObject(bmp)
        ctypes.windll.user32.PrintWindow(hwnd, dst.GetSafeHdc(), 2)
        img = Image.frombytes("RGB", (w, h), bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
    finally:
        dst.DeleteDC()
        if bmp.GetHandle():
            win32gui.DeleteObject(bmp.GetHandle())
        win32gui.ReleaseDC(hwnd, hdc)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)


def _grab_panel(root: tk.Tk, out_path: Path, margin: int = 28) -> None:
    """Save the panel as it really composites on screen (acrylic is invisible to PrintWindow) over a neutral page backdrop."""
    back = tk.Toplevel(root)
    back.overrideredirect(True)
    back.attributes("-topmost", True)
    root.update()
    w, h = root.winfo_width() + 2 * margin, root.winfo_height() + 2 * margin
    page = Image.new("RGB", (w, h), (246, 247, 249))
    d = ImageDraw.Draw(page)
    try:
        font = ImageFont.truetype("msyh.ttc", 17)
    except OSError:
        font = ImageFont.load_default()
    for i in range(h // 26 + 1):
        d.text((14, 6 + i * 26), "订单确认 · Order summary — the quick brown fox jumps over the lazy dog 示例文字", fill=(60, 64, 67), font=font)
    d.rectangle([w - 190, h // 2 - 8, w - 30, h // 2 + 30], fill=(26, 115, 232))
    photo = ImageTk.PhotoImage(page)
    tk.Label(back, image=photo, bd=0).pack()
    back.geometry(f"{w}x{h}+{root.winfo_rootx() - margin}+{root.winfo_rooty() - margin}")
    end = time.time() + 0.6
    while time.time() < end:
        root.update()
        root.lift()
        time.sleep(0.03)
    box = (root.winfo_rootx() - margin, root.winfo_rooty() - margin, root.winfo_rootx() - margin + w, root.winfo_rooty() - margin + h)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ImageGrab.grab(bbox=box, include_layered_windows=True, all_screens=True).save(out_path)
    back.destroy()
    root.update()


def test_ui_preview() -> None:
    print("[3/4] Rendering command bar UI screenshots...")
    preview_dir = PROJECT_ROOT / "artifacts" / "cua_ui_preview"
    root = tk.Tk()
    panel = ChromePilotPanel(root, enable_mcp=False, sound_enabled=False, effects_enabled=False)
    try:
        panel.apply_telemetry_snapshot(
            {
                "active_harness": "",
                "harness_mode": "none",
                "mcp_clients": 0,
                "task_state": "idle",
                "task_step": 0,
                "task_total_steps": 0,
                "task_elapsed_s": 0.0,
            },
        )
        _grab_panel(root, preview_dir / "ui_compact.png")

        panel.apply_telemetry_snapshot(
            {
                "active_harness": "Antigravity",
                "harness_mode": "single",
                "mcp_clients": 1,
                "task_state": "running",
                "task_step": 3,
                "task_total_steps": 5,
                "task_elapsed_s": 12.4,
                "task_step_label": "点击搜索下拉列表中的「Elon Musk (@elonmusk)」用户项",
            }
        )
        set_win11_border_color(root, CYAN)

        _grab_panel(root, preview_dir / "ui_running.png")
        panel.apply_telemetry_snapshot({"task_state": "idle"})
        panel.toggle_settings()
        _grab_panel(root, preview_dir / "ui_settings.png")
        panel.toggle_settings()

        # Risk confirmation appears only when an action is actually stopped; show it on a realistic page crop
        page = Image.new("RGB", (640, 360), (255, 255, 255))
        d = ImageDraw.Draw(page)
        try:
            f_body, f_bold = ImageFont.truetype("msyh.ttc", 15), ImageFont.truetype("msyhbd.ttc", 15)
        except OSError:
            f_body = f_bold = ImageFont.load_default()
        d.text((210, 120), "订单合计", fill=(95, 99, 104), font=f_body)
        d.text((380, 120), "¥ 1,299.00", fill=(32, 33, 36), font=f_bold)
        d.line([(210, 150), (450, 150)], fill=(218, 220, 224))
        d.rounded_rectangle([250, 168, 410, 206], radius=8, fill=(26, 115, 232))
        d.text((298, 177), "确认支付", fill=(255, 255, 255), font=f_bold)
        panel.show_confirm_card("触发高风险保护词「支付」，执行前需人工确认。", panel._crop_crosshair(page, [515, 520]), warning=True, detail="指令：帮我把这个订单付掉")
        panel._flash_border(AMBER, 0)
        _grab_panel(root, preview_dir / "ui_expanded.png")
        print(f"  [PASS] UI screenshots saved to {preview_dir}.")
    finally:
        panel.close()


def test_ui_interactions() -> None:
    print("[3b] Driving the command bar: layout states, timers, clicks, confirm/submit flows...")
    import chrome_pilot_panel as cpp

    # Behaviour first, with motion off so every assertion sees the settled state; motion itself is checked in test_ui_motion
    saved_motion, cpp.MOTION_SCALE = cpp.MOTION_SCALE, 0.0
    root = tk.Tk()
    panel = ChromePilotPanel(root, enable_mcp=False, sound_enabled=False, effects_enabled=False)

    def pump(ms: int = 120) -> None:
        end = time.time() + ms / 1000.0
        while time.time() < end:
            root.update()
            time.sleep(0.01)

    def click(widget: tk.Widget, x: int = 6, y: int = 6, press_release: bool = False) -> None:
        if press_release:
            widget.event_generate("<ButtonPress-1>", x=x, y=y)
            widget.event_generate("<ButtonRelease-1>", x=x, y=y)
        else:
            widget.event_generate("<Button-1>", x=x, y=y)
        pump(80)

    def plain(text: str) -> str:
        return text.replace("\u200a", "")

    run_snap = {"active_harness": "Antigravity", "harness_mode": "single", "mcp_clients": 1, "task_state": "running", "task_step": 3,
                "task_total_steps": 5, "task_elapsed_s": 12.4, "task_step_label": "点击搜索下拉列表中的用户项"}
    try:
        # 1) At rest the bar is one line: no status, no settings, no confirmation; window controls recede.
        panel.apply_telemetry_snapshot({"task_state": "idle"})
        pump(700)  # let start-up engine events drain: idle bookkeeping must not open a status line
        h_idle = root.winfo_reqheight()
        assert not panel.status_frame.winfo_ismapped() and not panel.settings_frame.winfo_ismapped() and not panel.confirm_frame.winfo_ismapped()
        assert panel.gear_btn._dim and panel.min_btn._dim and panel.close_btn._dim
        with patch.object(root, "winfo_pointerxy", return_value=(root.winfo_rootx() + 5, root.winfo_rooty() + 5)):
            panel._sync_hover()
        assert not panel.gear_btn._dim, "controls must surface when the pointer is over the bar"
        with patch.object(root, "winfo_pointerxy", return_value=(root.winfo_rootx() - 50, root.winfo_rooty() - 50)):
            panel._sync_hover()
        assert panel.close_btn._dim

        # 1b) Glass: over a white window the bar is translucent dark (not black, not white), type stays bright,
        #     and the background still belongs to the bar for hit-testing (no colour-key click-through).
        if panel.glass:
            back = tk.Toplevel(root)
            back.overrideredirect(True)
            back.configure(bg="white")
            back.attributes("-topmost", True)
            back.geometry(f"{root.winfo_width() + 80}x{root.winfo_height() + 80}+{root.winfo_rootx() - 40}+{root.winfo_rooty() - 40}")
            pump(200)
            root.lift()
            pump(500)
            rx, ry = root.winfo_rootx(), root.winfo_rooty()
            shot = ImageGrab.grab(bbox=(rx, ry, rx + root.winfo_width(), ry + root.winfo_height()), include_layered_windows=True, all_screens=True)
            bx, by = panel.SHELL_PADX + 200, root.winfo_height() - 5
            glass_px = shot.getpixel((bx, by))
            ink_max = max(shot.getpixel((x, y))[0] for x in range(panel.SHELL_PADX + 34, panel.SHELL_PADX + 200) for y in range(12, root.winfo_height() - 12))
            hit = ctypes.windll.user32.GetAncestor(ctypes.windll.user32.WindowFromPoint(ctypes.wintypes.POINT(rx + bx, ry + by)), 2)
            own = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
            back.destroy()
            assert all(18 <= c <= 120 for c in glass_px), f"glass over white should be translucent dark, got {glass_px}"
            assert ink_max >= 150 and hit == own, (ink_max, hit, own)
            glass_note = f"glass over white={glass_px}, ink max={ink_max}"
        else:
            glass_note = "glass unavailable (opaque fallback)"

        # 2) Status line: notices leave by themselves, failures stay until clicked, long text is cut to one line.
        panel.show_status("已取消", cpp.TEXT_SECONDARY, ttl_ms=150)
        pump(60)
        assert panel.status_frame.winfo_ismapped() and root.winfo_reqheight() > h_idle
        pump(400)
        assert not panel.status_frame.winfo_ismapped() and root.winfo_reqheight() == h_idle
        long_err = "目标区域被其他窗口遮挡：" + "请把 Chrome 窗口移到前台后重试；" * 6
        panel.show_status(long_err, cpp.DANGER)
        pump(500)
        assert panel.status_frame.winfo_ismapped() and panel._status_truncated and panel.status_label.cget("text").endswith("…")
        assert panel.status_label.winfo_reqwidth() <= panel._col_w and panel._status_full == " ".join(long_err.split())
        assert panel.status_label.cget("fg") == cpp.DANGER
        click(panel.status_label)
        assert not panel.status_frame.winfo_ismapped(), "a failure line is dismissed by clicking it"

        # 3) A harness task: message left, compact meta right, mini label mirrors it; results fold away.
        panel.apply_telemetry_snapshot(run_snap)
        pump(60)
        assert panel.status_frame.winfo_ismapped() and plain(panel.meta_label.cget("text")) == "ANTIGRAVITY · 3/5 · 12.4s", panel.meta_label.cget("text")
        assert panel.status_label.cget("text") == "点击搜索下拉列表中的用户项" and plain(panel.mini_label.cget("text")) == "ANTIGRAVITY · 3/5"
        assert panel.pline.state == "running"
        panel.apply_telemetry_snapshot({**run_snap, "task_state": "done", "task_step": 5, "task_elapsed_s": 28.6, "task_step_label": "已完成"})
        assert plain(panel.meta_label.cget("text")) == "ANTIGRAVITY · 5 步 · 28.6s" and panel._status_after is not None
        panel._clear_status()
        assert plain(panel.mini_label.cget("text")) == "CHROME PILOT"

        # 4) Engine events: idle bookkeeping is ignored, a failure or a finished plan is shown.
        idle_evt = {"task_state": "idle", "state": "idle", "last_message": "已获取画面 · 12 个交互元素"}
        panel._handle_engine_event_ui(idle_evt, "status", False, "已获取画面 · 12 个交互元素")
        assert not panel.status_frame.winfo_ismapped()
        panel._handle_engine_event_ui({"task_state": "idle", "state": "error"}, "status", False, "读屏模型调用失败")
        pump(60)
        assert panel.status_frame.winfo_ismapped() and panel.status_label.cget("text") == "读屏模型调用失败"
        panel._handle_engine_event_ui({"task_state": "done", "state": "idle", "task_step": 4}, "status", False, "动作已执行（未验证提交 · 4 步）")
        assert panel.status_label.cget("text") == "动作已执行（未验证提交 · 4 步）"
        panel._clear_status()

        # 5) A harness that went quiet stops being presented as live; the next event brings it back.
        eng = panel.engine
        eng.task_state, eng.task_started_at, eng.task_last_activity = "running", time.monotonic() - 300.0, time.monotonic() - 300.0
        panel.apply_telemetry_snapshot(run_snap)
        pump(500)
        assert not panel.status_frame.winfo_ismapped() and panel.pline.state == "idle", "stale task must quiet down"
        eng.task_started_at = eng.task_last_activity = time.monotonic()
        panel.apply_telemetry_snapshot({**run_snap, "task_elapsed_s": 0.0})
        pump(700)
        ticking = plain(panel.meta_label.cget("text"))
        assert panel.status_frame.winfo_ismapped() and ticking.startswith("ANTIGRAVITY · 3/5 · ") and not ticking.endswith(" 0.0s"), ticking
        eng.task_state = "idle"
        panel._stop_elapsed_tick()
        panel._clear_status()

        # 6) Settings: one line of dropdowns and icon switches; choosing says nothing; no port on any standing label.
        def open_menu(dd: Any) -> Any:
            click(dd)
            assert cpp.PopupMenu.current is not None and cpp.PopupMenu.current.anchor is dd, "click must open the dropdown"
            return cpp.PopupMenu.current

        def shown(dd: Any) -> str:
            return dd.itemcget(dd._txt_id, "text")

        click(panel.gear_btn)
        assert panel.settings_open and panel.settings_frame.winfo_ismapped() and panel.gear_btn._active
        menu = open_menu(panel.mode_dd)
        assert list(menu.rows) == ["auto", "confirm", "preview"]
        click(menu.rows["preview"])
        assert panel.exec_mode == "preview" and cpp.PopupMenu.current is None and shown(panel.mode_dd) == "仅预览" and not panel.status_frame.winfo_ismapped()
        open_menu(panel.mode_dd).choose("auto")
        assert panel.exec_mode == "auto" and shown(panel.mode_dd) == "高危时确认"

        saved_model = panel.model_choice
        with patch.object(cpp, "available_model_options", return_value=["M3.5 · Q4_K_M", "M3.5 · Q8_0", "M3 · Q4_K_M"]), \
                patch.object(panel.engine.grounder, "set_model") as fake_set_model:
            panel._set_model("M3.5 · Q8_0")
            menu = open_menu(panel.model_dd)
            assert list(menu.rows) == ["M3.5", "M3"]
            click(menu.rows["M3"])
            assert panel.model_choice == "M3 · Q4_K_M", "a quant the new model lacks falls back to one it has"
            assert shown(panel.model_dd) == "M3" and shown(panel.quant_dd) == "Q4" and fake_set_model.call_args.args == ("M3 · Q4_K_M",)
            assert list(open_menu(panel.quant_dd).rows) == ["Q4_K_M"]
            cpp.PopupMenu.dismiss()
            panel._choose_model_name("M3.5")
            menu = open_menu(panel.quant_dd)
            assert list(menu.rows) == ["Q4_K_M", "Q8_0"]
            click(menu.rows["Q8_0"])
            assert panel.model_choice == "M3.5 · Q8_0" and shown(panel.quant_dd) == "Q8"
            panel._set_model(saved_model)

        fake_wins = [SimpleNamespace(hwnd=4242, title="Elon Musk (@elonmusk) / X - Google Chrome"), SimpleNamespace(hwnd=4343, title="订单确认 - Google Chrome")]
        with patch.object(cpp, "find_chrome_windows", return_value=fake_wins), patch.object(panel.engine, "set_target_mode") as fake_target:
            menu = open_menu(panel.target_dd)
            assert list(menu.rows) == ["managed", ("desktop", 4242), ("desktop", 4343)]
            click(menu.rows[("desktop", 4343)])
            assert panel.target_mode == "desktop" and panel.selected_hwnd == 4343 and fake_target.call_args.args == ("desktop", 4343)
            assert shown(panel.target_dd) == "本机 Chrome" and panel._target_tip() == "订单确认 - Google Chrome"
            open_menu(panel.target_dd).choose("managed")
            assert panel.target_mode == "managed" and shown(panel.target_dd) == "后台托管"

        open_menu(panel.mode_dd)
        time.sleep(0.2)
        click(panel.cmd_row)
        assert cpp.PopupMenu.current is None, "a click elsewhere in the bar closes the dropdown"
        open_menu(panel.mode_dd)
        panel._on_escape()
        assert cpp.PopupMenu.current is None and panel.settings_open, "Esc closes the dropdown first, not the settings"

        # The second click on a menu's own anchor closes it, also when Windows has already closed it for that click
        # (a real click on the anchor takes focus from the menu before the anchor sees it); the third opens it again.
        for anchor in (panel.target_dd, panel.mode_dd, panel.model_dd, panel.quant_dd, panel.mcp_btn):
            click(anchor)
            assert cpp.PopupMenu.current is not None and cpp.PopupMenu.current.anchor is anchor
            click(anchor)
            assert cpp.PopupMenu.current is None, "second click closes"
            click(anchor)
            time.sleep(0.25)
            cpp.PopupMenu.current._on_focus_out()
            assert cpp.PopupMenu.current is None and getattr(anchor, "_open", False) is False
            click(anchor)
            assert cpp.PopupMenu.current is None, "the click that took the menu's focus must not reopen it"
            click(anchor)
            assert cpp.PopupMenu.current is not None and cpp.PopupMenu.current.anchor is anchor, "third click opens again"
            time.sleep(0.25)
            cpp.PopupMenu.current._on_focus_out()
            other = panel.mode_dd if anchor is not panel.mode_dd else panel.quant_dd
            click(other)
            assert cpp.PopupMenu.current is not None and cpp.PopupMenu.current.anchor is other, "focus lost to another dropdown opens that one"
            cpp.PopupMenu.dismiss()

        click(panel.sound_btn)
        assert panel.sound_enabled is True and panel.sound_btn._kind == "bell" and not panel.status_frame.winfo_ismapped()
        click(panel.sound_btn)
        assert panel.sound_enabled is False and panel.sound_btn._kind == "bell_off"
        assert panel.effects_btn._kind == "spark_off"

        def standing_text(widget: tk.Misc) -> str:
            parts = [str(widget.cget("text"))] if isinstance(widget, tk.Label) and widget.winfo_ismapped() else []
            if isinstance(widget, tk.Canvas) and widget.winfo_ismapped():
                parts += [str(widget.itemcget(i, "text")) for i in widget.find_all() if widget.type(i) == "text"]
            return " ".join(parts + [standing_text(c) for c in widget.winfo_children() if not isinstance(c, tk.Toplevel)])

        assert "18390" not in standing_text(root) and "127.0.0.1" not in standing_text(root), "endpoint must not be on a standing label"
        click(panel.mcp_btn)
        menu = cpp.PopupMenu.current
        assert menu is not None and list(menu.rows) == ["http", "stdio"]
        assert any("18390" in str(c.cget("text")) for c in menu.win.winfo_children()[0].winfo_children() if isinstance(c, tk.Label)), "the endpoint is available on demand"
        with patch.object(cpp.pyperclip, "copy") as fake_copy:
            click(menu.rows["http"])
        assert fake_copy.call_count == 1 and cpp.PopupMenu.current is None and panel.status_label.cget("text") == "已复制"
        panel._clear_status()
        assert cpp._brief_reason("触发高风险保护词「支付」，执行前需人工确认。") == "触发高风险保护词「支付」"
        assert cpp._brief_reason("目标为密码输入框，需人工确认后才能操作。") == "目标为密码输入框"

        # 7) High-risk confirmation: replaces the status line, sits above settings, ochre action, cancel clears it.
        assert not panel.confirm_frame.winfo_ismapped() and "保护词" not in standing_text(root), "risk content exists only when triggered"
        sample = panel._crop_crosshair(Image.new("RGB", (640, 360), (255, 255, 255)), [500, 500])
        assert sample.size == (260, 140) and sample.getpixel((130, 70 - 14))[0] > 200 and not any(
            r > 200 and g < 110 and b < 110 for r, g, b in sample.getdata()), "target marker is a quiet ring, not a red cross-hair"
        panel.show_status("执行中…", cpp.CYAN)
        panel.pending_managed = {"instruction": "支付订单", "confirm_token": "tok-1"}
        panel.show_confirm_card("触发高风险保护词「支付」，执行前需人工确认。", sample, warning=True, detail="指令：支付订单")
        pump(80)
        assert panel.confirm_frame.winfo_ismapped() and not panel.status_frame.winfo_ismapped()
        assert panel.confirm_frame.winfo_y() < panel.settings_frame.winfo_y(), "confirmation must come before settings"
        assert panel.confirm_yes._fill == cpp.AMBER and panel.confirm_title.cget("fg") == cpp.AMBER and panel.preview_label.winfo_ismapped()
        assert panel.confirm_title.cget("text") == "触发高风险保护词「支付」" and root.winfo_reqwidth() == panel.PANEL_W
        assert panel.confirm_detail.winfo_ismapped() and panel.confirm_detail.cget("text") == "指令：支付订单"
        assert panel.confirm_bar.cget("bg") == cpp.AMBER and panel.confirm_frame.winfo_height() <= 150, panel.confirm_frame.winfo_height()
        assert panel.confirm_yes.winfo_ismapped() and panel.confirm_no.winfo_ismapped()
        panel._flash_border(cpp.AMBER, 120)
        pump(400)
        assert panel.pline.state == "confirm", "a pending question keeps its ochre frieze past the flash"
        click(panel.confirm_no, x=10, y=10, press_release=True)
        assert panel.pline.state == "idle"
        assert panel.pending_managed is None and not panel.confirm_frame.winfo_ismapped() and panel.status_label.cget("text") == "已取消"
        assert panel.status_frame.winfo_y() < panel.settings_frame.winfo_y(), "status must come before settings"
        panel._on_escape()
        pump(60)
        assert not panel.settings_open and not panel.settings_frame.winfo_ismapped(), "Esc closes settings"

        # Confirming passes the one-time token on, shows the working state, then the result.
        panel.pending_managed = {"instruction": "支付订单", "confirm_token": "tok-2"}
        panel.show_confirm_card("触发高风险保护词「支付」，执行前需人工确认。", None, warning=True)
        with patch.object(panel.engine, "act", return_value={"status": "ok", "elapsed_s": 0.21}) as fake_act:
            click(panel.confirm_yes, x=10, y=10, press_release=True)
            pump(500)
        assert fake_act.call_count == 1 and fake_act.call_args.kwargs["confirm_token"] == "tok-2" and fake_act.call_args.kwargs["instruction"] == "支付订单"
        assert not panel.confirm_frame.winfo_ismapped() and panel.status_label.cget("text").startswith("完成"), panel.status_label.cget("text")
        panel._clear_status()

        # 8) Submit: empty input does nothing but ask for input; a URL runs, flips the action to 停止, and reports.
        panel.on_submit()
        assert not panel.busy and not panel.status_frame.winfo_ismapped()
        panel._clear_placeholder(None)
        panel.entry.insert(0, "https://example.com/")
        gate = threading.Event()

        def slow_nav(action: str, url: str | None = None, **_kw: Any) -> dict[str, Any]:
            gate.wait(5.0)
            return {"status": "ok", "elapsed_s": 0.33, "url": url}

        with patch.object(panel.engine, "navigate", side_effect=slow_nav) as fake_nav:
            click(panel.run_btn, x=10, y=10, press_release=True)
            assert panel.busy and panel.run_btn.itemcget(panel.run_btn._txt_id, "text") == "停止" and panel.status_label.cget("text") == "打开中…"
            assert str(panel.entry.cget("state")) == "disabled"
            gate.set()
            pump(500)
        assert fake_nav.call_args.kwargs["url"] == "https://example.com/" and not panel.busy
        assert panel.run_btn.itemcget(panel.run_btn._txt_id, "text") == "执行" and panel.status_label.cget("text").startswith("完成")
        panel._clear_status()

        # 9) Collapse and expand.
        click(panel.min_btn)
        assert panel.collapsed and panel.mini_bar.winfo_ismapped() and not panel.main_wrap.winfo_ismapped()
        click(panel.mini_label)
        assert not panel.collapsed and panel.main_wrap.winfo_ismapped() and root.winfo_reqheight() == h_idle
        print(f"  [PASS] Command bar interactions verified (resting height {h_idle}px; {glass_note}; dropdowns, status/confirm/settings order, timers, clicks, confirm + submit flows).")
    finally:
        cpp.MOTION_SCALE = saved_motion
        panel.close()


def test_ui_motion() -> None:
    print("[3c] Checking motion: eased tray, hover/role colour easing, menu and window fades, interruption, pacing...")
    import chrome_pilot_panel as cpp

    assert cpp.MOTION_SCALE > 0.0, "motion must be on by default (CHROME_PILOT_MOTION=0 turns it off)"
    root = tk.Tk()
    panel = ChromePilotPanel(root, enable_mcp=False, sound_enabled=False, effects_enabled=False)

    def pump(ms: int) -> None:
        end = time.time() + ms / 1000.0
        while time.time() < end:
            root.update()
            time.sleep(0.004)

    def spy(obj: Any, attr: str, pick: Any) -> list[Any]:
        """Record what an animation actually writes (one Tk update() can run several frames, so polling misses them)."""
        orig, log = getattr(obj, attr), []

        def wrapped(*a: Any, **k: Any) -> Any:
            out = orig(*a, **k)
            val = pick(a, k)
            if val is not None:
                log.append(val)
            return out

        setattr(obj, attr, wrapped)
        return log

    try:
        # Entrance: fades in and settles; ends fully opaque (glass) so type is never left translucent.
        assert float(root.attributes("-alpha")) < 1.0, "entrance starts transparent"
        pump(700)
        assert abs(float(root.attributes("-alpha")) - panel._alpha_rest) < 1e-6
        h_idle = root.winfo_reqheight()

        # Settings tray: height eases through intermediate values, never overshoots, lands exactly on its content.
        heights = spy(panel.tray, "configure", lambda a, k: k.get("height"))
        t0 = time.perf_counter()
        panel.toggle_settings()
        pump(400)
        goal = panel.tray_inner.winfo_reqheight()
        distinct = sorted(set(heights))
        t_open = len(heights)
        assert len(distinct) >= 5 and heights == sorted(heights) and max(heights) == goal == panel._tray_h, (distinct, goal)
        frames_open = panel._tray_tween.frames
        assert frames_open >= 6, f"tray tween ran only {frames_open} frames (jank)"
        assert panel.settings_frame.winfo_ismapped() and root.winfo_reqheight() == h_idle + goal

        # Interrupting motion: close mid-open, reopen mid-close — ends in the last requested state, nothing queued.
        panel.toggle_settings()
        pump(50)
        panel.toggle_settings()
        pump(40)
        panel.toggle_settings()
        pump(450)
        assert not panel.settings_open and panel._tray_h == 0 and not panel.tray.winfo_ismapped() and root.winfo_reqheight() == h_idle
        assert not panel._tray_tween.running

        # A risk question is usable from the first frame and can be answered mid-animation.
        panel.pending_managed = {"instruction": "支付订单", "confirm_token": "tok"}
        panel.show_confirm_card("触发高风险保护词「支付」，执行前需人工确认。", None, warning=True, detail="指令：支付订单")
        assert panel._confirm_visible and panel.confirm_frame.winfo_manager() and panel.confirm_title.cget("text") == "触发高风险保护词「支付」"
        pump(30)
        assert 0 < panel._tray_h, "confirmation is already opening"
        panel.on_confirm_cancel()
        pump(500)
        assert panel.pending_managed is None and not panel.confirm_frame.winfo_ismapped() and panel.status_label.cget("text") == "已取消"
        panel._clear_status()
        pump(400)
        assert panel._tray_h == 0 and root.winfo_reqheight() == h_idle

        # Buttons: colour eases to hover and back; a role change (执行 -> 停止) eases too; press darkens at once.
        mids = spy(panel.run_btn, "_redraw", lambda a, k: panel.run_btn._shown)
        panel.run_btn._set_hover(True)
        pump(260)
        assert panel.run_btn._shown == cpp.ACCENT and len(set(mids)) >= 3, set(mids)
        panel.run_btn._set_hover(False)
        pump(260)
        assert panel.run_btn._shown == cpp.FG
        panel._set_busy(True)
        pump(300)
        assert panel.run_btn._shown == cpp.DANGER and panel.run_btn.itemcget(panel.run_btn._txt_id, "text") == "停止"
        panel._set_busy(False)
        pump(300)
        assert panel.run_btn._shown == cpp.FG
        panel.gear_btn.set_dim(False)
        pump(300)
        assert panel.gear_btn._shown == cpp.FG_FAINT
        panel.gear_btn.set_dim(True)
        pump(300)
        assert panel.gear_btn._shown == cpp.FG_DIM

        # Dropdown: fades in while settling 6 px, chevron flips while open and flips back on close.
        panel.toggle_settings()
        pump(400)
        menu = panel.mode_dd.open()
        assert float(menu.win.attributes("-alpha")) < 1.0, "menu starts transparent"
        pump(300)
        assert float(menu.win.attributes("-alpha")) == 1.0 and menu.tween.frames >= 3 and not menu.tween.running
        assert menu.win.winfo_rooty() == panel.mode_dd.winfo_rooty() + panel.mode_dd.winfo_height() + 6 and panel.mode_dd._open
        menu.choose("confirm")
        pump(60)
        assert panel.exec_mode == "confirm" and not panel.mode_dd._open
        panel._set_exec_mode("auto")
        panel._sync_drawer_styles()
        panel.toggle_settings()
        pump(400)

        # Status: new text fades up from the glass; outcome colour on the progress line fades out, not off.
        inks = spy(panel.status_label, "configure", lambda a, k: str(k["fg"]).upper() if "fg" in k else None)
        panel.show_status("已取消", cpp.TEXT_SECONDARY, ttl_ms=None)
        pump(300)
        assert inks[-1] == cpp.FG.upper() and len(set(inks)) >= 3, set(inks)
        panel._clear_status()
        panel.pline.set_state("done")
        panel.pline.set_state("idle")
        assert panel.pline._fade.running
        pump(500)
        assert not panel.pline._fade.running and panel.pline.state == "idle"
        panel.orb.set_color(cpp.AMBER)
        assert panel.orb._pulse_tween.running, "seal swells once when the state colour changes"
        pump(400)

        # Collapse: a short dip in opacity hides the change of width; ends at rest opacity in the right state.
        dips = spy(root, "attributes", lambda a, k: float(a[1]) if len(a) == 2 and a[0] == "-alpha" else None)
        panel.toggle_collapse()
        pump(400)
        assert panel.collapsed and panel.mini_bar.winfo_ismapped() and min(dips) < 0.6 and abs(dips[-1] - panel._alpha_rest) < 1e-6
        panel.toggle_collapse()
        pump(400)
        assert not panel.collapsed and panel.main_wrap.winfo_ismapped() and abs(float(root.attributes("-alpha")) - panel._alpha_rest) < 1e-6
        print(f"  [PASS] Motion verified (settings tray {goal}px over {t_open} resize steps in {frames_open} frames; interruption, confirm-first-frame, easing, fades).")
    finally:
        panel.close()


def test_cua_effects() -> None:
    print("[4/4] Verifying Screen-Edge Blue Glow + AI Cursor sprites...")
    for pal in ("blue", "emerald"):
        frames = build_glow_strip_frames(1280, 720, thickness=36, num_frames=4, palette=pal)
        assert len(frames) == 4
        top = frames[0]["top"]
        assert top.shape == (36, 1280, 4) and int(top[0, 640, 3]) > 40 and int(top[-1, 640, 3]) <= 5, (pal, top.shape)
        left = frames[0]["left"]
        assert left.shape == (648, 36, 4) and int(left[300, 0, 3]) > 40, left.shape

    for kind in ("arrow", "hand"):
        sprite, hot = render_ai_cursor_rgba(kind, 48)
        assert sprite.size == (48, 48) and sprite.mode == "RGBA"
        hcur = create_hcursor_from_rgba(sprite, hot)
        assert hcur != 0
        assert _user32.DestroyIcon(ctypes.wintypes.HICON(hcur))

    glow_p, cur_p = save_effects_previews(PROJECT_ROOT / "artifacts" / "cua_ui_preview")
    assert glow_p.is_file() and cur_p.is_file()

    # Verify layered click-through overlay creates, updates, excludes from Point hits, and closes cleanly
    overlay = ScreenEdgeGlowOverlay(thickness=24)
    try:
        overlay.start_blue()
        time.sleep(0.25)
        assert overlay._hwnd != 0
        pt_hwnd = win32gui.WindowFromPoint((640, 360))
        assert pt_hwnd != overlay._hwnd, f"Overlay intercepted WindowFromPoint: {pt_hwnd}"
        overlay.pulse_emerald_and_fade(hold_s=0.15)
        time.sleep(0.35)
    finally:
        overlay.close()
    print("  [PASS] CUA screen-edge glow, AI cursor sprites, and click-through layered window verified.")


def test_perceiver_and_guards() -> None:
    print("[5/5] Running desktop perceiver & fail-closed foreground guard tests...")
    # 1) Tiny 18x18 heart highlight delta should register as effective (prevents double-click unlike bug)
    before = Image.new("RGB", (400, 300), (16, 18, 22))
    d_b = ImageDraw.Draw(before)
    d_b.rectangle([191, 141, 209, 159], fill=(110, 118, 129))
    after_heart = before.copy()
    d_a = ImageDraw.Draw(after_heart)
    d_a.rectangle([191, 141, 209, 159], fill=(249, 24, 128))
    eff_heart = analyze_click_effect(
        before,
        after_heart,
        (500, 500),
        before_ocr=[{"text": "63", "center_norm": [540, 500]}],
        after_ocr=[{"text": "64", "center_norm": [540, 500]}],
    )
    assert eff_heart["effective"] is True and eff_heart["highlight_delta"] >= 18, eff_heart
    assert "63 -> 64" in eff_heart["local_ocr_changes"], eff_heart

    eff_same = analyze_click_effect(before, before.copy(), (500, 500))
    assert eff_same["effective"] is False, eff_same

    # 2) Deterministic OCR matching rejects ambiguous ties cleanly
    sample_els = [
        {"id": 1, "text": "Latest", "center_norm": [460, 165], "rect_norm": [440, 155, 485, 178]},
        {"id": 2, "text": "Like", "center_norm": [460, 405], "rect_norm": [445, 395, 475, 415]},
        {"id": 3, "text": "Like", "center_norm": [460, 650], "rect_norm": [445, 640, 475, 660]},
    ]
    hit_latest = match_ocr_target("Latest", sample_els)
    assert hit_latest is not None and hit_latest["element"]["id"] == 1 and hit_latest["unique"] is True, hit_latest
    assert match_ocr_target("Like", sample_els) is None

    # 3) PersistentWinRTOCR handles dark mode cleanly and unlinks temp file on close()
    dark_card = Image.new("RGB", (640, 200), (15, 18, 25))
    d_card = ImageDraw.Draw(dark_card)
    d_card.text((40, 60), "Logan Kilpatrick  64  Gemini", fill=(242, 245, 250))
    ocr = PersistentWinRTOCR()
    tmp_bmp = ocr._tmp_path
    try:
        res_ocr = ocr.recognize(dark_card)
        tmp_bmp = ocr._tmp_path
        assert res_ocr.get("ocr_elements") and "Logan" in res_ocr.get("page_text", ""), res_ocr
    finally:
        ocr.close()
    assert not tmp_bmp.exists() and not ocr._created_tmps, f"Temp OCR BMP leaked: {tmp_bmp}"

    # 4) Fail-closed foreground focus guard still aborts with zero keystroke leakage
    chrome_info = SimpleNamespace(hwnd=123456, title="X - Google Chrome", is_minimized=False)
    eng = cua_engine.ChromeCUAEngine.__new__(cua_engine.ChromeCUAEngine)
    eng.desktop_hwnd = chrome_info.hwnd
    eng._resolve_desktop_window = MagicMock(return_value=chrome_info)
    eng._desktop_io_guard = lambda: __import__("contextlib").nullcontext()
    with (
        patch.object(cua_engine, "check_corner_failsafe", return_value=False),
        patch.object(cua_engine, "focus_chrome_window", return_value=chrome_info),
        patch.object(cua_engine.win32gui, "GetForegroundWindow", return_value=999999),
        patch.object(cua_engine, "send_hotkey") as mock_hotkey,
        patch.object(cua_engine, "paste_text_via_clipboard") as mock_paste,
    ):
        try:
            eng._navigate_desktop(action="goto", url="https://x.com/home", tab_id=None)
            raise AssertionError("Expected RuntimeError(chrome_not_foreground)")
        except RuntimeError as exc:
            assert "chrome_not_foreground" in str(exc), str(exc)
        assert mock_hotkey.call_count == 0 and mock_paste.call_count == 0

    print("  [PASS] Desktop perceiver (heart delta, OCR tie-break, dark-mode WinRT OCR) and foreground guard verified.")


def test_p1_contracts(test_url: str) -> None:
    print("[6/6] Running P1 submission semantics & local_usage accounting contracts...")
    base_url = test_url.rsplit("/", 1)[0]

    # ---- A. Unit contracts for _accumulate_usage & _merge_local_usage ----
    u_zero = _new_local_usage()
    assert u_zero == {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "usage_complete": True,
        "missing_usage_calls": 0,
    }, u_zero

    u_missing = _new_local_usage()
    _accumulate_usage(u_missing, {})
    assert u_missing["calls"] == 1 and u_missing["usage_complete"] is False and u_missing["missing_usage_calls"] == 1
    assert u_missing["prompt_tokens"] is None and u_missing["completion_tokens"] is None and u_missing["total_tokens"] is None, u_missing

    u_mixed = _new_local_usage()
    _accumulate_usage(u_mixed, {"prompt_tokens": 100, "completion_tokens": 12, "total_tokens": 112})
    _accumulate_usage(u_mixed, None)
    assert u_mixed == {
        "calls": 2,
        "prompt_tokens": 100,
        "completion_tokens": 12,
        "total_tokens": 112,
        "usage_complete": False,
        "missing_usage_calls": 1,
    }, u_mixed

    agg = _new_local_usage()
    _merge_local_usage(agg, u_missing)
    assert agg["calls"] == 1 and agg["usage_complete"] is False and agg["prompt_tokens"] is None, agg
    _merge_local_usage(agg, {"calls": 1, "prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55, "usage_complete": True, "missing_usage_calls": 0})
    assert agg == {
        "calls": 2,
        "prompt_tokens": 50,
        "completion_tokens": 5,
        "total_tokens": 55,
        "usage_complete": False,
        "missing_usage_calls": 1,
    }, agg

    # ---- B. Unit contracts for _classify_managed_submission & _classify_desktop_submission ----
    assert _classify_desktop_submission(
        before_title="Site", after_title="Site", before_url="https://a", after_url="https://a",
        pixel_change_frac=0.008, expect_submit=None, wait_settle=True, until_text=None, until_matched=True, timed_out=False,
    ) == ("not_observed", False, "no_change_observed")
    assert _classify_desktop_submission(
        before_title="Site", after_title="Site", before_url="https://a", after_url="https://a",
        pixel_change_frac=0.08, expect_submit=None, wait_settle=True, until_text=None, until_matched=True, timed_out=False,
    ) == ("unknown", None, "desktop_visual_change_only")
    assert _classify_desktop_submission(
        before_title="Site", after_title="Search - Site", before_url="https://a", after_url="https://a",
        pixel_change_frac=0.08, expect_submit=None, wait_settle=True, until_text=None, until_matched=True, timed_out=False,
    ) == ("observed", True, "title_changed")
    assert _classify_managed_submission(
        {"url": "https://a", "probe_id": "1", "body_text": "hello", "el_count": 4, "is_multiline": False},
        {"url": "https://a", "probe_id": "2", "body_text": "hello", "el_count": 4, "form_submitted": False},
        text_val="q", expect_submit=None, wait_settle=True, until_text=None, until_matched=True, timed_out=False, probe_failed=False,
    ) == ("observed", True, "page_navigated")
    assert _classify_managed_submission(
        {"url": "https://a", "probe_id": "1", "body_text": "hello", "el_count": 4, "is_multiline": False},
        {"url": "https://a", "probe_id": "1", "body_text": "hello Unrelated 0 Unrelated 1 Unrelated 2", "el_count": 9, "form_submitted": False},
        text_val="alpha", expect_submit=None, wait_settle=True, until_text=None, until_matched=True, timed_out=False, probe_failed=False,
    ) == ("unknown", None, "ui_change_only_no_submit")
    assert _classify_managed_submission(
        {"url": "https://a", "probe_id": "1", "body_text": "hello", "el_count": 4, "is_multiline": False},
        {"url": "https://a", "probe_id": "1", "body_text": "hello Draft preview: alpha (not submitted)", "el_count": 4, "form_submitted": False},
        text_val="alpha", expect_submit=None, wait_settle=True, until_text=None, until_matched=True, timed_out=False, probe_failed=False,
    ) == ("unknown", None, "query_reflected_unverified")
    assert _classify_managed_submission(
        {"url": "https://a", "probe_id": "1", "body_text": "READY-FOUND", "el_count": 4, "is_multiline": False},
        {"url": "https://a", "probe_id": "1", "body_text": "READY-FOUND", "el_count": 4, "form_submitted": False},
        text_val="alpha", expect_submit=None, wait_settle=True, until_text="READY-FOUND", until_matched=True, timed_out=False, probe_failed=False, until_preexisted=True,
    ) == ("not_observed", False, "until_text_preexisting_no_submit")
    assert _classify_managed_submission(
        {"url": "https://a", "probe_id": "1", "body_text": "READY-FOUND", "el_count": 4, "is_multiline": False},
        {"url": "https://a", "probe_id": "1", "body_text": "READY-FOUND unrelated", "el_count": 7, "form_submitted": False},
        text_val="alpha", expect_submit=None, wait_settle=True, until_text="READY-FOUND", until_matched=True, timed_out=False, probe_failed=False, until_preexisted=True,
    ) == ("unknown", None, "until_text_preexisting_unverified")
    assert _classify_managed_submission(
        {"url": "https://a", "probe_id": "1", "body_text": "READY-FOUND", "el_count": 4, "is_multiline": False},
        {"url": "https://a/next", "probe_id": "2", "body_text": "READY-FOUND", "el_count": 4, "form_submitted": False},
        text_val="alpha", expect_submit=None, wait_settle=True, until_text="READY-FOUND", until_matched=True, timed_out=False, probe_failed=False, until_preexisted=True,
    ) == ("observed", True, "url_changed")
    assert _classify_desktop_submission(
        before_title="Site", after_title="Site", before_url="https://a", after_url="https://a",
        pixel_change_frac=0.005, expect_submit=None, wait_settle=True, until_text="READY", until_matched=True, timed_out=False, until_preexisted=True,
    ) == ("not_observed", False, "until_text_preexisting_no_submit")

    # ---- C. Desktop mocked contracts (dismiss_popup, _act_desktop, click_and_type + run_steps) ----
    blank_img = Image.new("RGB", (640, 400), (245, 245, 245))
    noisy_img = Image.new("RGB", (640, 400), (40, 40, 40))
    desk_info = SimpleNamespace(hwnd=222222, title="MockPage - Google Chrome", is_minimized=False, dwm_rect=SimpleNamespace(left=0, top=0, right=640, bottom=400), dpi_scale=1.0)
    desk_eng = ChromeCUAEngine.__new__(ChromeCUAEngine)
    desk_eng.target_mode = "desktop"
    desk_eng.desktop_hwnd = desk_info.hwnd
    desk_eng.active_url = "https://mock.local/"
    desk_eng.active_title = desk_info.title
    desk_eng.step_count = 0
    desk_eng.task_id = ""
    desk_eng.task_title = ""
    desk_eng.task_state = "idle"
    desk_eng.task_step = 0
    desk_eng.task_total_steps = 0
    desk_eng.task_started_at = 0.0
    desk_eng.task_last_activity = 0.0
    desk_eng.task_step_label = ""
    desk_eng.recent_steps = []
    desk_eng.state = "idle"
    desk_eng.last_message = ""
    desk_eng.status_listener = None
    desk_eng.desktop_hide_hook = None
    desk_eng._pending_confirms = {}
    desk_eng._mcp_sessions = {}
    desk_eng.active_harness = ""
    desk_eng.mcp_clients = 0
    desk_eng.mcp_port = 0
    desk_eng._last_ocr_elements = []
    desk_eng._last_click_coord = None
    desk_eng._last_click_effect = None
    desk_eng._lock = threading.RLock()
    desk_eng.abort_event = threading.Event()
    desk_eng._desktop_io_guard = lambda: __import__("contextlib").nullcontext()
    desk_eng._resolve_desktop_window = MagicMock(return_value=desk_info)
    desk_eng._capture_desktop_window = MagicMock(return_value=(desk_info, blank_img))
    desk_eng._desktop_url_reader = SimpleNamespace(read_url_and_tabs=lambda *a, **kw: {"url": "https://mock.local/", "tabs": []})
    desk_eng._desktop_ocr = SimpleNamespace(recognize=lambda *a, **kw: {"page_text": "MockPage", "ocr_elements": [], "ocr_ms": 1.0, "inferred_url": "https://mock.local/"})
    desk_eng.chrome = SimpleNamespace(is_running=lambda: False, hwnd=0)
    desk_eng.grounder = SimpleNamespace(model_label="M3.5 · Q4_K_M", is_running=lambda: True, port=18091)

    # Upfront validation must reject invalid require_submit before any steps run
    for bad_step in (
        {"kind": "click_and_type", "text_hint": "x", "text": "y", "press_enter": False, "require_submit": True},
        {"kind": "click_and_type", "text_hint": "x", "text": "y", "press_enter": True, "expect_submit": False, "require_submit": True},
    ):
        try:
            desk_eng.run_steps([bad_step])
            raise AssertionError(f"Expected ValueError for invalid step {bad_step}")
        except ValueError:
            pass

    # Desktop dismiss_popup: normal vision not_found (retains tokens)
    desk_eng.grounder.predict = MagicMock(return_value=('{"action": "not_found", "reason": "no popup"}', cua_engine.ParsedAction(action="not_found", reason="no popup", raw="{}"), 0.1, {"prompt_tokens": 888, "completion_tokens": 12, "total_tokens": 900}))
    with (
        patch.object(cua_engine, "check_corner_failsafe", return_value=False),
        patch.object(cua_engine, "focus_chrome_window", return_value=desk_info),
        patch.object(cua_engine.win32gui, "GetForegroundWindow", return_value=desk_info.hwnd),
        patch.object(cua_engine, "send_hotkey"),
    ):
        d_ok = desk_eng.dismiss_popup()
        assert d_ok["status"] == "ok" and d_ok["local_usage"] == {"calls": 1, "prompt_tokens": 888, "completion_tokens": 12, "total_tokens": 900, "usage_complete": True, "missing_usage_calls": 0}, d_ok

    # Desktop dismiss_popup: post-predict focus failure preserves local_usage
    desk_eng.grounder.predict = MagicMock(return_value=('{"action": "left_click", "coordinate": [500, 500]}', cua_engine.ParsedAction(action="left_click", coordinate=(500, 500), raw="{}"), 0.1, {"prompt_tokens": 777, "completion_tokens": 14, "total_tokens": 791}))
    with (
        patch.object(cua_engine, "check_corner_failsafe", return_value=False),
        patch.object(cua_engine, "focus_chrome_window", return_value=desk_info),
        patch.object(cua_engine.win32gui, "GetForegroundWindow", return_value=999999),
    ):
        d_fg = desk_eng.dismiss_popup()
        assert d_fg["status"] == "exec_failed" and d_fg["local_usage"]["calls"] == 1 and d_fg["local_usage"]["prompt_tokens"] == 777, d_fg

    # Desktop dismiss_popup: VLMCallError marks usage_complete=False, calls=1
    desk_eng.grounder.predict = MagicMock(side_effect=VLMCallError("server down"))
    d_err = desk_eng.dismiss_popup()
    assert d_err["status"] == "vlm_error" and d_err["local_usage"]["calls"] == 1 and d_err["local_usage"]["usage_complete"] is False and d_err["local_usage"]["prompt_tokens"] is None, d_err

    # Desktop click_and_type: noisy pixel change alone must NOT report submitted=True
    desk_eng._last_ocr_elements = [{"id": 1, "text": "MockBox", "center_norm": [200, 200], "rect_norm": [180, 190, 220, 210]}]
    with (
        patch.object(cua_engine, "check_corner_failsafe", return_value=False),
        patch.object(cua_engine, "focus_chrome_window", return_value=desk_info),
        patch.object(cua_engine.win32gui, "GetForegroundWindow", return_value=desk_info.hwnd),
        patch.object(cua_engine.win32gui, "IsWindow", return_value=True),
        patch.object(cua_engine, "inspect_chrome_window", return_value=desk_info),
        patch.object(cua_engine, "print_window_dwm", return_value=blank_img),
        patch.object(cua_engine, "send_hotkey"),
        patch.object(cua_engine, "paste_text_via_clipboard"),
        patch.object(cua_engine, "execute_action", return_value=(True, "sent")),
        patch.object(desk_eng, "_wait_desktop_frame_settle", side_effect=lambda *a, **kw: (kw["meta_out"].update({"settled": True, "timed_out": False, "until_matched": True}) or (desk_info, noisy_img, {"url": "https://mock.local/", "page_text": "MockPage", "ocr_elements": [], "ocr_ms": 1.0}))),
    ):
        d_noise = desk_eng.run_steps([{"kind": "click_and_type", "ocr_id": 1, "text": "test", "press_enter": True, "wait_settle": True}])
        assert d_noise["trace"][0]["submission_state"] == "unknown" and d_noise["trace"][0]["submitted"] is None and d_noise["status"] == "completed_unverified_submit", d_noise

    desk_eng._last_ocr_elements = [{"id": 1, "text": "MockBox", "center_norm": [200, 200], "rect_norm": [180, 190, 220, 210]}]
    with (
        patch.object(cua_engine, "check_corner_failsafe", return_value=False),
        patch.object(cua_engine, "focus_chrome_window", return_value=desk_info),
        patch.object(cua_engine.win32gui, "GetForegroundWindow", return_value=desk_info.hwnd),
        patch.object(cua_engine.win32gui, "IsWindow", return_value=True),
        patch.object(cua_engine, "inspect_chrome_window", return_value=desk_info),
        patch.object(cua_engine, "print_window_dwm", return_value=blank_img),
        patch.object(cua_engine, "send_hotkey"),
        patch.object(cua_engine, "paste_text_via_clipboard"),
        patch.object(cua_engine, "execute_action", return_value=(True, "sent")),
        patch.object(desk_eng, "_wait_desktop_frame_settle", side_effect=lambda *a, **kw: (kw["meta_out"].update({"settled": True, "timed_out": False, "until_matched": True}) or (desk_info, noisy_img, {"url": "https://mock.local/?q=test", "page_text": "Search test", "ocr_elements": [], "ocr_ms": 1.0}))),
    ):
        d_ok_sub = desk_eng.run_steps([{"kind": "click_and_type", "ocr_id": 1, "text": "test", "press_enter": True, "wait_settle": True, "require_submit": True}])
        assert d_ok_sub["status"] == "completed" and d_ok_sub["trace"][0]["submitted"] is True and d_ok_sub["trace"][0]["submission_evidence"] == "url_changed", d_ok_sub

    # ---- D. Live Managed Chrome + MCPServerCore + call_vlm intercept suite ----
    orig_call_vlm = cua_engine.call_vlm
    intercepted_calls: list[dict[str, Any] | Exception] = []
    vlm_script: list[tuple[str, dict[str, Any]] | Exception] = []

    def scripted_call_vlm(base_url: str, img_b64: str, instruction: str, max_tokens: int = 64, system_prompt: str = "") -> tuple[str, dict[str, Any]]:
        if not vlm_script:
            raise RuntimeError("Unexpected unscripted call_vlm invocation: " + instruction)
        nxt = vlm_script.pop(0)
        intercepted_calls.append(nxt if isinstance(nxt, Exception) else nxt[1])
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    cua_engine.call_vlm = scripted_call_vlm
    with tempfile.TemporaryDirectory(prefix="cua_p1_prof_", ignore_cleanup_errors=True) as prof:
        eng = ChromeCUAEngine(profile_dir=Path(prof), vlm_port=18091)
        eng.grounder.ensure_started = lambda: 0.0
        core = MCPServerCore(eng)
        seq = 0

        def mcp_call(tool_name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
            nonlocal seq
            seq += 1
            resp = core.handle_message({"jsonrpc": "2.0", "id": seq, "method": "tools/call", "params": {"name": tool_name, "arguments": args or {}}}, "p1_test")
            res = resp["result"]
            assert not res.get("isError"), resp
            return json.loads(res["content"][0]["text"])

        try:
            mcp_call("cua_navigate", {"action": "goto", "url": f"{base_url}/p1_edges"})
            obs = mcp_call("cua_observe", {"max_elements": 60})
            els = {e.get("placeholder") or e.get("text"): e for e in obs["elements"]}

            # 1) Valid form Enter submission -> observed / submitted=True / completed
            res_form = mcp_call("cua_run_steps", {
                "steps": [{"kind": "click_and_type", "element_id": els["表单搜索框"]["id"], "text": "py312", "press_enter": True, "wait_settle": True}],
                "auto_dismiss_popup": False,
            })
            assert res_form["status"] == "completed" and res_form["actions_completed"] is True and res_form["submission_summary"] == "observed", res_form
            assert res_form["trace"][0]["submitted"] is True and res_form["trace"][0]["submission_state"] == "observed", res_form

            # 2) Non-form Enter (require_submit=False) -> completed_unverified_submit, submitted=False
            res_non = mcp_call("cua_run_steps", {
                "steps": [{"kind": "click_and_type", "element_id": els["非提交输入框"]["id"], "text": "hello", "press_enter": True, "wait_settle": True}],
                "auto_dismiss_popup": False,
            })
            assert res_non["status"] == "completed_unverified_submit" and res_non["actions_completed"] is True, res_non
            assert res_non["unverified_submit_steps"] == [1] and res_non["submission_summary"] == "not_observed", res_non
            assert res_non["trace"][0]["submitted"] is False and res_non["trace"][0]["submission_state"] == "not_observed", res_non

            # 3) Non-form Enter (require_submit=True) -> halted:submit_not_observed & step 2 NEVER executes!
            res_req = mcp_call("cua_run_steps", {
                "steps": [
                    {"kind": "click_and_type", "element_id": els["非提交输入框"]["id"], "text": "hello2", "press_enter": True, "wait_settle": True, "require_submit": True},
                    {"kind": "act", "element_id": els["保存订单"]["id"]},
                ],
                "auto_dismiss_popup": False,
            })
            assert res_req["status"] == "halted:submit_not_observed" and res_req["actions_completed"] is False and res_req["executed_steps"] == 1, res_req
            ext_draft = mcp_call("cua_extract")
            assert "状态：待保存" in ext_draft["text"] and "已错误点击保存" not in ext_draft["text"], ext_draft

            # 4) press_enter=False -> not_requested, completed
            res_plain = mcp_call("cua_run_steps", {
                "steps": [{"kind": "click_and_type", "element_id": els["非提交输入框"]["id"], "text": "only_type", "press_enter": False}],
                "auto_dismiss_popup": False,
            })
            assert res_plain["status"] == "completed" and res_plain["trace"][0]["submission_state"] == "not_requested" and "submitted" not in res_plain["trace"][0], res_plain

            # 5) Multiline <textarea> Enter + step 2 -> NOT interrupted; expect_submit=False -> completed
            res_ml = mcp_call("cua_run_steps", {
                "steps": [
                    {"kind": "click_and_type", "element_id": els["多行备注"]["id"], "text": "第一行", "press_enter": True, "wait_settle": True},
                    {"kind": "type", "text": "第二行"},
                ],
                "auto_dismiss_popup": False,
            })
            assert res_ml["executed_steps"] == 2 and res_ml["actions_completed"] is True and res_ml["trace"][0]["submission_evidence"] == "multiline_newline", res_ml
            res_ml2 = mcp_call("cua_run_steps", {
                "steps": [
                    {"kind": "click_and_type", "element_id": els["多行备注"]["id"], "text": "行1", "press_enter": True, "expect_submit": False},
                    {"kind": "type", "text": "行2"},
                ],
                "auto_dismiss_popup": False,
            })
            assert res_ml2["status"] == "completed" and res_ml2["trace"][0]["submission_state"] == "not_expected" and "submitted" not in res_ml2["trace"][0], res_ml2

            # 6) Async non-form search needs a specific new result marker to establish a submission signal.
            res_async = mcp_call("cua_run_steps", {
                "steps": [{"kind": "click_and_type", "element_id": els["异步搜索"]["id"], "text": "上海浦东", "press_enter": True, "wait_settle": True, "require_submit": True, "until_text": "异步命中：上海浦东"}],
                "auto_dismiss_popup": False,
            })
            assert res_async["status"] == "completed" and res_async["trace"][0]["submitted"] is True and res_async["trace"][0]["submission_evidence"] == "until_text_matched", res_async
            assert eng.chrome.cdp.evaluate("window.__P1__.asyncCalls") == 1, res_async

            # Without a specific result marker, even a real async update is intentionally unverified.
            res_async_unverified = mcp_call("cua_run_steps", {
                "steps": [{"kind": "click_and_type", "element_id": els["异步搜索"]["id"], "text": "杭州西湖", "press_enter": True, "wait_settle": True}],
                "auto_dismiss_popup": False,
            })
            async_truth = eng.chrome.cdp.evaluate("({calls:window.__P1__.asyncCalls,text:document.getElementById('async-result').textContent})")
            assert res_async_unverified["status"] == "completed_unverified_submit" and res_async_unverified["trace"][0]["submission_evidence"] == "query_reflected_unverified", res_async_unverified
            assert async_truth == {"calls": 2, "text": "异步命中：杭州西湖"}, async_truth

            # 7) False-positive guards: until_text timeout, clock+highlight noise, unrelated text banner
            res_uto = mcp_call("cua_run_steps", {
                "steps": [{"kind": "click_and_type", "element_id": els["非提交输入框"]["id"], "text": "x", "press_enter": True, "wait_settle": True, "until_text": "永远不会出现的结果", "max_wait_s": 0.6}],
                "auto_dismiss_popup": False,
            })
            assert res_uto["status"] == "completed_unverified_submit" and res_uto["trace"][0]["submission_evidence"] == "until_text_timeout" and res_uto["trace"][0]["submitted"] is False, res_uto

            res_clk = mcp_call("cua_run_steps", {
                "steps": [{"kind": "click_and_type", "element_id": els["时钟干扰输入框"]["id"], "text": "clk", "press_enter": True, "wait_settle": True}],
                "auto_dismiss_popup": False,
            })
            assert res_clk["status"] == "completed_unverified_submit" and res_clk["trace"][0]["submitted"] is False and res_clk["trace"][0]["submission_evidence"] == "no_change_observed", res_clk

            res_ban = mcp_call("cua_run_steps", {
                "steps": [{"kind": "click_and_type", "element_id": els["横幅文案干扰输入框"]["id"], "text": "ban", "press_enter": True, "wait_settle": True}],
                "auto_dismiss_popup": False,
            })
            assert res_ban["status"] == "completed_unverified_submit" and res_ban["trace"][0]["submitted"] is None and res_ban["trace"][0]["submission_state"] == "unknown", res_ban

            # Positive control: pre-existing until_text on real form submit STILL succeeds
            res_pre_real = mcp_call("cua_run_steps", {
                "steps": [{"kind": "click_and_type", "element_id": els["表单搜索框"]["id"], "text": "py313", "press_enter": True, "wait_settle": True, "until_text": "表单结果", "require_submit": True}],
                "auto_dismiss_popup": False,
            })
            assert res_pre_real["status"] == "completed" and res_pre_real["trace"][0]["submitted"] is True, res_pre_real

            # 8) Adversarial submission false-positive blocks (unrelated async DOM links & pre-existing until_text)
            mcp_call("cua_navigate", {"action": "goto", "url": f"{base_url}/p1_adversarial"})
            adv_obs = mcp_call("cua_observe", {"max_elements": 60})
            adv_els = {e.get("placeholder") or e.get("text"): e for e in adv_obs["elements"]}

            adv_links = mcp_call("cua_run_steps", {
                "steps": [
                    {"kind": "click_and_type", "element_id": adv_els["Test links input"]["id"], "text": "alpha", "press_enter": True, "wait_settle": True, "require_submit": True, "max_wait_s": 2.5},
                    {"kind": "act", "text_hint": "Continue"},
                ],
                "auto_dismiss_popup": False,
            })
            truth_links = eng.chrome.cdp.evaluate("({state: window.__ADV__, noise: document.querySelectorAll('#adv-noise a').length})")
            assert adv_links["status"] == "halted:submit_not_observed" and adv_links["executed_steps"] == 1, adv_links
            assert adv_links["trace"][0]["submission_state"] == "unknown" and adv_links["trace"][0]["submitted"] is None, adv_links["trace"]
            assert truth_links["noise"] >= 4 and truth_links["state"] == {"submitted": False, "continued": False}, truth_links

            adv_pre = mcp_call("cua_run_steps", {
                "steps": [
                    {"kind": "click_and_type", "element_id": adv_els["Test pre input"]["id"], "text": "beta", "press_enter": True, "wait_settle": True, "until_text": "READY-FOUND", "require_submit": True, "max_wait_s": 2.5},
                    {"kind": "act", "text_hint": "Continue"},
                ],
                "auto_dismiss_popup": False,
            })
            truth_pre = eng.chrome.cdp.evaluate("window.__ADV__")
            assert adv_pre["status"] == "halted:submit_not_observed" and adv_pre["executed_steps"] == 1, adv_pre
            assert adv_pre["trace"][0]["submission_state"] == "not_observed" and adv_pre["trace"][0]["submission_evidence"] == "until_text_preexisting_no_submit" and adv_pre["trace"][0]["submitted"] is False, adv_pre["trace"]
            assert truth_pre == {"submitted": False, "continued": False}, truth_pre

            # 9) An input echo in a draft preview is not submission, even when a later action is requested.
            mcp_call("cua_navigate", {"action": "goto", "url": f"{base_url}/p1_adversarial"})
            echo_obs = mcp_call("cua_observe", {"max_elements": 60})
            echo_el = next(e for e in echo_obs["elements"] if e.get("placeholder") == "Test echo input")
            adv_echo = mcp_call("cua_run_steps", {
                "steps": [
                    {"kind": "click_and_type", "element_id": echo_el["id"], "text": "gamma", "press_enter": True, "wait_settle": True, "require_submit": True, "max_wait_s": 2.5},
                    {"kind": "act", "text_hint": "Continue"},
                ],
                "auto_dismiss_popup": False,
            })
            truth_echo = eng.chrome.cdp.evaluate("({state:window.__ADV__,preview:document.getElementById('adv-noise').textContent})")
            assert adv_echo["status"] == "halted:submit_not_observed" and adv_echo["executed_steps"] == 1, adv_echo
            assert adv_echo["trace"][0]["submission_state"] == "unknown" and adv_echo["trace"][0]["submission_evidence"] == "query_reflected_unverified", adv_echo
            assert truth_echo["state"] == {"submitted": False, "continued": False} and "gamma" in truth_echo["preview"], truth_echo

            # ---- E. P1-2 live managed local_usage checks ----
            # 1) Direct cua_dismiss_popup vision fallback via run_steps & standalone (not_found, invalid_json, vlm_error, missing usage)
            intercepted_calls.clear()
            vlm_script.append(('{"action": "not_found", "reason": "no modal"}', {"prompt_tokens": 1079, "completion_tokens": 20, "total_tokens": 1099}))
            rp = mcp_call("cua_run_steps", {"steps": [{"kind": "dismiss_popup"}], "auto_dismiss_popup": False})
            assert rp["local_usage"] == {"calls": 1, "prompt_tokens": 1079, "completion_tokens": 20, "total_tokens": 1099, "usage_complete": True, "missing_usage_calls": 0}, rp
            assert len(intercepted_calls) == 1

            intercepted_calls.clear()
            vlm_script.append(("not json at all", {"prompt_tokens": 650, "completion_tokens": 9, "total_tokens": 659}))
            dp_bad = mcp_call("cua_dismiss_popup")
            assert dp_bad["local_usage"] == {"calls": 1, "prompt_tokens": 650, "completion_tokens": 9, "total_tokens": 659, "usage_complete": True, "missing_usage_calls": 0} and dp_bad.get("vision_action") == "invalid_json", dp_bad

            intercepted_calls.clear()
            vlm_script.append(RuntimeError("vlm crash"))
            dp_vlm_err = mcp_call("cua_dismiss_popup")
            assert dp_vlm_err["status"] == "vlm_error" and dp_vlm_err["local_usage"]["calls"] == 1 and dp_vlm_err["local_usage"]["usage_complete"] is False and dp_vlm_err["local_usage"]["prompt_tokens"] is None, dp_vlm_err

            intercepted_calls.clear()
            vlm_script.append(('{"action": "not_found", "reason": "missing metrics"}', {}))
            dp_no_metrics = mcp_call("cua_dismiss_popup")
            assert dp_no_metrics["local_usage"] == {"calls": 1, "prompt_tokens": None, "completion_tokens": None, "total_tokens": None, "usage_complete": False, "missing_usage_calls": 1}, dp_no_metrics

            # 2) Mixed locate(zoom=True) where pass 2 fails -> retains pass 1 tokens + marks incomplete
            intercepted_calls.clear()
            vlm_script.extend([
                ('{"action": "left_click", "coordinate": [200, 200]}', {"prompt_tokens": 1072, "completion_tokens": 17, "total_tokens": 1089}),
                RuntimeError("zoom crash"),
            ])
            loc_mix = mcp_call("cua_locate", {"instruction": "提交", "zoom": True})
            assert loc_mix["status"] == "vlm_error" and loc_mix["local_usage"] == {"calls": 2, "prompt_tokens": 1072, "completion_tokens": 17, "total_tokens": 1089, "usage_complete": False, "missing_usage_calls": 1}, loc_mix

            # 3) DOM close path (genuine 0 calls) & auto-popup-heal visual fallback + subsequent step sum
            mcp_call("cua_navigate", {"action": "goto", "url": test_url})
            intercepted_calls.clear()
            dp_dom = mcp_call("cua_dismiss_popup")
            assert dp_dom["local_usage"] == u_zero and len(intercepted_calls) == 0, dp_dom

            # Post-grounding Red-tier act retains local_usage
            pay_btn = next(e for e in mcp_call("cua_observe")["elements"] if "确认支付" in e.get("text", ""))
            px = (pay_btn["rect_norm"][0] + pay_btn["rect_norm"][2]) // 2
            py = (pay_btn["rect_norm"][1] + pay_btn["rect_norm"][3]) // 2
            vlm_script.append((f'{{"action": "left_click", "coordinate": [{px}, {py}]}}', {"prompt_tokens": 910, "completion_tokens": 18, "total_tokens": 928}))
            red_act = mcp_call("cua_act", {"instruction": "点击右侧红色按钮", "strategy": "vision"})
            assert red_act["status"] == "needs_human_confirmation" and red_act["local_usage"] == {"calls": 1, "prompt_tokens": 910, "completion_tokens": 18, "total_tokens": 928, "usage_complete": True, "missing_usage_calls": 0}, red_act

            # Normal locate(zoom=True) usage matches sum of both passes identically
            intercepted_calls.clear()
            vlm_script.extend([
                ('{"action": "left_click", "coordinate": [250, 250]}', {"prompt_tokens": 1072, "completion_tokens": 17, "total_tokens": 1089}),
                ('{"action": "left_click", "coordinate": [500, 500]}', {"prompt_tokens": 540, "completion_tokens": 17, "total_tokens": 557}),
            ])
            loc_ok = mcp_call("cua_locate", {"instruction": "提交", "zoom": True})
            assert loc_ok["status"] == "ok" and loc_ok["local_usage"] == {"calls": 2, "prompt_tokens": 1612, "completion_tokens": 34, "total_tokens": 1646, "usage_complete": True, "missing_usage_calls": 0}, loc_ok
            assert len(intercepted_calls) == 2

            # Auto-popup-heal on modal without DOM close label -> 1 heal VLM call + 1 ask call, zero omission & zero double-count
            mcp_call("cua_navigate", {"action": "goto", "url": f"{base_url}/p1_popup_vision"})
            obs_pop = mcp_call("cua_observe")
            icon_btn = next(e for e in obs_pop["elements"] if e.get("text") == "继续")
            ix = (icon_btn["rect_norm"][0] + icon_btn["rect_norm"][2]) // 2
            iy = (icon_btn["rect_norm"][1] + icon_btn["rect_norm"][3]) // 2
            intercepted_calls.clear()
            vlm_script.extend([
                (f'{{"action": "left_click", "coordinate": [{ix}, {iy}]}}', {"prompt_tokens": 1000, "completion_tokens": 20, "total_tokens": 1020}),
                ("目标内容已可见", {"prompt_tokens": 400, "completion_tokens": 10, "total_tokens": 410}),
            ])
            heal_plan = mcp_call("cua_run_steps", {
                "steps": [
                    {"kind": "act", "action_json": {"action": "wait", "seconds": 0.1}},
                    {"kind": "ask", "question": "弹窗是否已关闭？"},
                ],
                "auto_dismiss_popup": True,
            })
            assert heal_plan["status"] == "completed", heal_plan
            heal_step = next((t for t in heal_plan["trace"] if t.get("step") == "1_popup_heal"), None)
            assert heal_step is not None and heal_step["local_usage"]["calls"] == 1, heal_plan["trace"]
            assert heal_plan["local_usage"] == {
                "calls": 2,
                "prompt_tokens": 1400,
                "completion_tokens": 30,
                "total_tokens": 1430,
                "usage_complete": True,
                "missing_usage_calls": 0,
            }, heal_plan["local_usage"]
            assert len(intercepted_calls) == 2
        finally:
            eng.close()
            cua_engine.call_vlm = orig_call_vlm
    print("  [PASS] P1 submission semantics (form/non-form/async/multiline/desktop) & local_usage accounting verified.")


def test_v14_efficiency_contracts(test_url: str) -> None:
    print("[P1+] Verifying v14 efficiency contracts (brief payloads, capture-free observe, light status, prewarm)...")
    base_url = test_url.rsplit("/", 1)[0]

    # Brief trace keeps status + submission evidence verbatim, drops echoes and defaults only.
    zero = cua_engine._new_local_usage()
    used = {**zero, "calls": 1, "prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10}
    missing = {**zero, "usage_complete": False}
    e1 = {"step": 1, "kind": "click_and_type", "input": {"text": "secret"}, "status": "ok", "url": "u", "title": "t", "submitted": None,
          "submission_state": "unknown", "submission_evidence": "query_reflected_unverified", "require_submit": False, "local_usage": zero}
    e2 = {"step": 2, "kind": "scroll", "input": {}, "status": "ok", "url": "u", "title": "t2", "popup_detected": False, "auto_healed": False,
          "infer_s": 0.0, "raw_prediction": "{}", "local_usage": used}
    e3 = {"step": 3, "kind": "act", "input": {}, "status": "invalid_json", "raw_prediction": "garbage", "popup_detected": True, "infer_s": 0.4, "local_usage": missing}
    b1, page = cua_engine._brief_trace_entry(e1, (None, None))
    b2, page = cua_engine._brief_trace_entry(e2, page)
    b3, page = cua_engine._brief_trace_entry(e3, page)
    assert "input" not in b1 and "local_usage" not in b1 and b1["url"] == "u" and b1["title"] == "t", b1
    assert b1["submitted"] is None and b1["submission_state"] == "unknown" and b1["require_submit"] is False, b1
    assert "url" not in b2 and b2["title"] == "t2" and b2["local_usage"] == used and "raw_prediction" not in b2, b2
    assert not {"popup_detected", "auto_healed", "infer_s", "input"} & set(b2), b2
    assert b3["raw_prediction"] == "garbage" and b3["popup_detected"] is True and b3["infer_s"] == 0.4 and b3["local_usage"] == missing, b3
    assert "input" in e1 and "raw_prediction" in e2, "brief must not mutate the full trace"
    bo = cua_engine._brief_observation({"url": "u", "dom_signature": "x", "vp_origin": [0, 0], "viewport": [1, 1, 1], "frame_size": [1, 1],
                                        "popup_close_ids": [], "auth_or_captcha_gate": None, "popup_detected": False})
    assert bo == {"url": "u", "popup_detected": False}, bo
    assert cua_engine._brief_observation({"popup_close_ids": [3], "auth_or_captcha_gate": "captcha_iframe"}) == {"popup_close_ids": [3], "auth_or_captcha_gate": "captcha_iframe"}

    # Prewarm racing close() must not leave a llama-server behind, and must not retry on another port.
    class FakeProc:
        def __init__(self) -> None:
            self.dead = False
        def poll(self) -> int | None:
            return 1 if self.dead else None
        def terminate(self) -> None:
            self.dead = True
        def wait(self, timeout: float | None = None) -> int:
            return 1

    started: list[FakeProc] = []

    def fake_start(_ns: Any) -> tuple[FakeProc, str]:
        started.append(FakeProc())
        return started[-1], "http://127.0.0.1:1"

    def fake_wait(_base: str, proc: FakeProc, timeout_s: float = 90.0) -> float:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("llama-server exited prematurely")
            time.sleep(0.02)
        return 8.0

    with tempfile.TemporaryDirectory(prefix="cua_v14_pw_", ignore_cleanup_errors=True) as tmp_pw, \
            patch.object(cua_engine, "start_llama_server", fake_start), patch.object(cua_engine, "wait_for_server", fake_wait), \
            patch.dict("os.environ", {"CUA_PREWARM": "1"}):
        eng_pw = cua_engine.ChromeCUAEngine(profile_dir=Path(tmp_pw), vlm_port=18299)
        core_pw = MCPServerCore(eng_pw)

        def pw_call(tool_name: str, tool_args: dict[str, Any]) -> None:
            resp = core_pw.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool_name, "arguments": tool_args}}, "v14_pw")
            assert not resp["result"].get("isError"), resp

        # Default is OFF: unset or unrecognised values never start the model, through the engine or the MCP router.
        for off_value in (None, "", "0", "false", "off", "2"):
            with patch.dict("os.environ"):
                os.environ.pop("CUA_PREWARM", None)
                if off_value is not None:
                    os.environ["CUA_PREWARM"] = off_value
                assert cua_engine.ChromeCUAEngine.prewarm_enabled() is False, off_value
                assert eng_pw.prewarm_async() is False, off_value
                pw_call("cua_navigate", {"action": "list_windows"})
        assert not started and eng_pw._prewarm_started is False, started
        for on_value in ("1", "true", "ON", " yes "):
            with patch.dict("os.environ", {"CUA_PREWARM": on_value}):
                assert cua_engine.ChromeCUAEngine.prewarm_enabled() is True, on_value

        # Explicit opt-in: status/task calls still do not load; the first real tool call does, exactly once.
        pw_call("cua_status", {})
        pw_call("cua_task", {"event": "progress", "message": "noop", "play_sound": False})
        assert not started, started
        pw_call("cua_navigate", {"action": "list_windows"})
        assert eng_pw._prewarm_started is True and eng_pw.prewarm_async() is False
        t_wait = time.monotonic() + 3.0
        while not started and time.monotonic() < t_wait:
            time.sleep(0.01)
        assert len(started) == 1, started
        eng_pw.close()
        time.sleep(0.3)
        assert len(started) == 1 and started[0].dead and eng_pw.grounder.proc is None, (len(started), started[0].dead)
        try:
            eng_pw.grounder.ensure_started()
            raise AssertionError("ensure_started must refuse after shutdown")
        except RuntimeError as exc:
            assert "grounder_shut_down" in str(exc), exc
    with tempfile.TemporaryDirectory(prefix="cua_v14_pw0_", ignore_cleanup_errors=True) as tmp_pw0, patch.dict("os.environ", {"CUA_PREWARM": "0"}):
        eng_off = cua_engine.ChromeCUAEngine(profile_dir=Path(tmp_pw0), vlm_port=18299)
        assert eng_off.prewarm_async() is False
        eng_off.close()

    with tempfile.TemporaryDirectory(prefix="cua_v14_", ignore_cleanup_errors=True) as tmp:
        eng = cua_engine.ChromeCUAEngine(profile_dir=Path(tmp), vlm_port=18298)
        core = MCPServerCore(eng)

        def mcp_call(tool_name: str, args: dict[str, Any] | None = None) -> tuple[dict[str, Any], int]:
            resp = core.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool_name, "arguments": args or {}}}, "v14")
            res = resp["result"]
            assert not res.get("isError"), resp
            return json.loads(res["content"][0]["text"]), len(res["content"][0]["text"].encode("utf-8"))

        try:
            nav_brief, nav_brief_b = mcp_call("cua_navigate", {"action": "goto", "url": f"{base_url}/p1_edges"})
            nav_small, nav_small_b = mcp_call("cua_navigate", {"action": "goto", "url": f"{base_url}/p1_edges", "max_elements": 3, "max_text_chars": 40})
            assert len(nav_small["elements"]) <= 3 and len(nav_small["text_excerpt"]) <= 40 and nav_small_b < nav_brief_b, (nav_small_b, nav_brief_b)
            assert "auth_or_captcha_gate" not in nav_brief and nav_brief["elements"], nav_brief

            # Screenshot-free observe must not capture, yet report the same geometry as a real capture.
            _info, frame = eng.chrome.capture_full_window()
            with patch.object(eng.chrome, "capture_full_window", side_effect=AssertionError("observe captured without include_screenshot")):
                obs_full = eng.observe(max_elements=60)
                obs_brief, obs_brief_b = mcp_call("cua_observe", {"max_elements": 60})
                eng.navigate("goto", url=f"{base_url}/p1_edges")
            obs_shot = eng.observe(include_screenshot=True, max_elements=60)
            assert obs_full["frame_size"] == list(frame.size) == obs_shot["frame_size"], (obs_full["frame_size"], frame.size)
            assert [e["rect_norm"] for e in obs_full["elements"]] == [e["rect_norm"] for e in obs_shot["elements"]]
            assert obs_shot.get("screenshot_b64") and "screenshot_b64" not in obs_full
            assert obs_full.get("dom_signature") and not {"dom_signature", "vp_origin", "viewport", "frame_size"} & set(obs_brief), obs_brief.keys()
            assert [e["id"] for e in obs_brief["elements"]] == [e["id"] for e in obs_full["elements"]]
            obs_fullmcp, obs_full_b = mcp_call("cua_observe", {"max_elements": 60, "detail": "full"})
            assert obs_fullmcp.get("dom_signature") and obs_brief_b < obs_full_b

            # Brief vs full run_steps: same verdict, smaller payload, no echoed inputs.
            els = {e.get("placeholder") or e.get("text"): e for e in obs_full["elements"]}
            plan = {"steps": [
                {"kind": "goto", "url": f"{base_url}/p1_edges"},
                {"kind": "click_and_type", "element_id": els["表单搜索框"]["id"], "text": "py312", "press_enter": True, "wait_settle": True, "require_submit": True},
                {"kind": "scroll", "direction": "down", "amount": 2},
            ], "auto_dismiss_popup": False}
            r_brief, r_brief_b = mcp_call("cua_run_steps", plan)
            r_full, r_full_b = mcp_call("cua_run_steps", {**plan, "detail": "full"})
            assert r_brief["status"] == r_full["status"] == "completed" and r_brief["executed_steps"] == r_full["executed_steps"] == 3, (r_brief, r_full)
            assert r_brief["local_usage"] == r_full["local_usage"] and r_brief["submission_summary"] == r_full["submission_summary"] == "observed"
            for tb, tf in zip(r_brief["trace"], r_full["trace"]):
                assert "input" not in tb and "input" in tf, (tb, tf)
                assert all(tb.get(k) == tf.get(k) for k in ("step", "kind", "status", "submitted", "submission_state", "submission_evidence")), (tb, tf)
            assert r_brief_b < r_full_b * 0.8, (r_brief_b, r_full_b)

            # Per-event telemetry must not spawn nvidia-smi or enumerate windows; cua_status is cached.
            gpu_calls: list[int] = []
            events: list[dict[str, Any]] = []
            fake_gpu = {"total_mib": 1, "used_mib": 1, "free_mib": 0}
            with patch.object(cua_engine, "query_gpu_memory_mib", side_effect=lambda: gpu_calls.append(1) or dict(fake_gpu)), \
                    patch.object(cua_engine, "find_chrome_windows", side_effect=AssertionError("window enumeration during status emission")):
                eng.status_listener = events.append
                eng._vram_cache = (0.0, {})
                eng.observe()
                t_res = eng.update_task("progress", message="tick", play_sound=False)
                eng.status_listener = None
            assert events and not gpu_calls and all("task_state" in ev and "event_kind" in ev for ev in events), (len(events), gpu_calls)
            assert set(t_res) == {"status", "task_state", "task_title", "task_step", "task_total_steps", "task_step_label", "task_elapsed_s"}, t_res
            with patch.object(cua_engine, "query_gpu_memory_mib", side_effect=lambda: gpu_calls.append(1) or dict(fake_gpu)):
                eng._vram_cache = (0.0, {})
                st1, st2 = eng.get_status(), eng.get_status()
            assert len(gpu_calls) == 1 and st1["vram_mib"] == st2["vram_mib"] == fake_gpu, gpu_calls
            print(f"  [PASS] v14 efficiency contracts (run_steps brief={r_brief_b}B vs full={r_full_b}B; observe brief={obs_brief_b}B vs full={obs_full_b}B; "
                  f"navigate default={nav_brief_b}B vs capped={nav_small_b}B).")
        finally:
            eng.close()


def main() -> int:
    test_perceiver_and_guards()
    srv, url = start_fixture_http()
    try:
        test_p1_contracts(url)
        test_v14_efficiency_contracts(url)
        test_stdio_mcp(url)
        test_http_mcp(url)
        test_ui_preview()
        test_ui_interactions()
        test_ui_motion()
        test_cua_effects()
    finally:
        srv.shutdown()
        srv.server_close()
    print("[ALL PASS] CUA engine, desktop perceiver, MCP Stdio/HTTP, Spotlight UI, Win32 effects, and P1 contracts verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
