"""Focused behavioral regression tests for stability, control, UIA, os mode, and security guardrails."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import tkinter as tk
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image
import requests


def _build_fixture_server():
    scroll_html = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>滚动与输入回归测试</title>
<style>
  body { margin: 0; font-family: sans-serif; }
  .spacer { height: 2400px; background: linear-gradient(#f8fafc, #e2e8f0); }
</style>
<script>
window.__scrollByCalls = 0;
window.__scrollByDy = 0;
const origScrollBy = window.scrollBy.bind(window);
window.scrollBy = function(a, b) {
  window.__scrollByCalls += 1;
  if (typeof a === 'object' && a !== null) window.__scrollByDy += (a.top || 0);
  else window.__scrollByDy += (b || 0);
  return origScrollBy(a, b);
};
</script></head><body>
  <h1>顶部测试区</h1>
  <input id="txt" type="text" placeholder="输入测试框" style="margin:16px;padding:8px;width:240px;">
  <div class="spacer"></div>
  <button id="footer-anchor">FOOTER_ANCHOR_ONLY_AT_BOTTOM</button>
</body></html>"""

    tab_zh_html = "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>中文标签页测试</title></head><body><h1>中文第一页</h1></body></html>"
    tab_beta_html = "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>第二页Beta</title></head><body><h1>第二页</h1></body></html>"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = scroll_html
            if self.path == "/zh":
                body = tab_zh_html
            elif self.path == "/beta":
                body = tab_beta_html
            raw = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args) -> None:
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}"


def _fake_win_info(roi_utils, hwnd: int, title: str, w: int = 1000, h: int = 700):
    rect = roi_utils.Rect(0, 0, w, h)
    crect = roi_utils.Rect(0, 80, w, h)
    return roi_utils.ChromeWindowInfo(
        hwnd=hwnd,
        title=title,
        dpi=96,
        dpi_scale=1.0,
        is_foreground=True,
        is_minimized=False,
        window_rect=rect,
        dwm_rect=rect,
        client_rect=crect,
    )


def _make_panel_stub(cpp_mod, cua_mod):
    panel = cpp_mod.ChromePilotPanel.__new__(cpp_mod.ChromePilotPanel)
    panel._closed = False
    panel._ui_thread_id = threading.get_ident()
    panel.busy = False
    panel.glass = False
    panel.settings_open = False
    panel.sound_enabled = False
    panel.effects_enabled = False
    panel.desktop_windows = []
    panel.selected_hwnd = 0
    panel.target_mode = "desktop"
    panel.exec_mode = "auto"
    panel.model_choice = "M4 · Q4_K_M"
    panel.pending_desktop = None
    panel.pending_managed = None
    panel.effects = SimpleNamespace(activate=lambda: None, deactivate=lambda: None, pulse_complete=lambda: None, pause_for_human=lambda: None)
    panel.sound = SimpleNamespace(play=lambda *a, **k: None)
    panel.show_status = MagicMock()
    panel._flash_border = MagicMock()
    panel._set_busy = MagicMock()
    panel._set_dot = MagicMock()
    panel._sync_drawer_styles = MagicMock()
    panel.apply_telemetry_snapshot = MagicMock()
    panel.show_confirm_card = MagicMock()
    panel.root = MagicMock()
    panel.root.withdraw = MagicMock()
    panel.root.deiconify = MagicMock()
    panel.root.after = MagicMock()

    eng = cua_mod.ChromeCUAEngine.__new__(cua_mod.ChromeCUAEngine)
    eng._lock = threading.RLock()
    eng.abort_event = threading.Event()
    eng.target_mode = "desktop"
    eng.desktop_hwnd = 0
    eng.panel_hwnd = 0
    eng.active_title = ""
    eng.active_url = ""
    eng.step_count = 0
    eng.task_state = "idle"
    eng.task_step = 0
    eng.task_total_steps = 0
    eng.task_started_at = 0.0
    eng.task_last_activity = 0.0
    eng.task_step_label = ""
    eng.task_explicit = False
    eng.recent_steps = []
    eng.state = "idle"
    eng.last_message = ""
    eng.status_listener = None
    eng.desktop_hide_hook = None
    eng._pending_confirms = {}
    eng._last_ocr_elements = []
    eng._last_click_coord = None
    eng._last_click_effect = None
    eng._begin_auto_step = lambda *a, **k: None
    eng._emit_status = lambda *a, **k: None
    eng.chrome = SimpleNamespace(hwnd=0, is_running=lambda: False)
    eng.grounder = SimpleNamespace(set_model=lambda *a, **k: None, model_label="M4 · Q4_K_M")
    eng._desktop_ocr = SimpleNamespace(recognize=lambda *a, **k: {"page_text": "", "ocr_elements": [], "ocr_ms": 0.0})
    eng._desktop_url_reader = SimpleNamespace(read_url_and_tabs=lambda *a, **k: {"url": "", "tabs": []})
    panel.engine = eng
    return panel, eng


def test_a1_abort_before_input_and_panel_guard(mods) -> None:
    cua_engine, cpp, roi_utils = mods["cua_engine"], mods["chrome_pilot_panel"], mods["roi_utils"]
    from action_schema import ParsedAction

    panel, eng = _make_panel_stub(cpp, cua_engine)
    fake_info = _fake_win_info(roi_utils, 1111, "TestChrome", 1000, 700)
    frame = Image.new("RGB", (1000, 700), (255, 255, 255))
    events: list[str] = []
    eng.desktop_hide_hook = lambda hidden: events.append(f"hide:{hidden}")

    class SyncWorker:
        def submit(self, fn, *a, **kw):
            fut = SimpleNamespace(done=lambda: True)
            res = fn(*a, **kw)
            fut.result = lambda: res
            return fut

    panel.worker = SyncWorker()
    panel._watch = MagicMock()
    panel.selected_hwnd = 1111

    def fake_predict(_img, _inst):
        events.append("predict")
        eng.request_abort()
        return ('{"action":"left_click","coordinate":[500,500]}', ParsedAction(action="left_click", coordinate=(500, 500), raw="{}"), 0.05, {})

    eng.grounder.predict = fake_predict
    exec_calls: list[str] = []
    with (
        patch.object(cua_engine, "inspect_chrome_window", return_value=fake_info),
        patch.object(cua_engine, "focus_chrome_window", return_value=fake_info),
        patch.object(cua_engine, "capture_chrome_window", side_effect=lambda _i: (events.append("capture") or frame)),
        patch.object(cua_engine, "execute_confirmed_action", side_effect=lambda *a, **k: (exec_calls.append("exec") or (True, "ok"))),
    ):
        panel._launch_desktop_worker(1111, "点击按钮")
    assert panel.root.deiconify.call_count == 0, f"Premature deiconify called: {panel.root.deiconify.call_count}"
    assert exec_calls == [], f"Aborted panel desktop flow still executed input: {exec_calls}"
    assert "hide:True" in events and "hide:False" in events and events.index("hide:False") < events.index("predict"), events

    # Managed & desktop single-step act() abort checks after VLM prediction
    eng.reset_abort()
    eng.target_mode = "managed"
    eng.chrome = SimpleNamespace(
        hwnd=2222,
        capture_full_window=lambda: (fake_info, frame),
        extract_dom_snapshot=lambda *a, **k: {"url": "http://127.0.0.1/", "title": "T", "elements": [], "dom_signature": "s"},
        dispatch_viewport_action=MagicMock(return_value=(True, "ok")),
        click_dom_element=MagicMock(return_value=(True, "ok")),
    )
    eng.grounder.predict = fake_predict
    res_m = eng.act(instruction="点击提交")
    assert res_m.get("status") == "aborted" and eng.chrome.dispatch_viewport_action.call_count == 0, res_m

    eng.reset_abort()
    eng.target_mode = "desktop"
    eng.desktop_hwnd = 1111
    eng._capture_desktop_window = lambda focus=False: (fake_info, frame)
    eng._focus_and_verify_desktop = lambda inf, settle_s=0.16: inf
    with (
        patch.object(cua_engine, "check_corner_failsafe", return_value=False),
        patch.object(cua_engine, "execute_action", MagicMock(return_value=(True, "ok"))) as mock_exec,
    ):
        res_d = eng.act(instruction="点击提交")
        assert res_d.get("status") == "aborted" and mock_exec.call_count == 0, res_d


def test_a2_desktop_close_tab_active_verify_and_managed_close(mods) -> None:
    cua_engine, roi_utils = mods["cua_engine"], mods["roi_utils"]
    eng = cua_engine.ChromeCUAEngine.__new__(cua_engine.ChromeCUAEngine)
    eng.target_mode = "desktop"
    eng.desktop_hwnd = 3333
    eng.active_title = "AlphaTab - Google Chrome"
    eng._lock = threading.RLock()
    eng.abort_event = threading.Event()
    eng._desktop_io_guard = lambda: contextlib.nullcontext()

    alpha_info = _fake_win_info(roi_utils, 3333, "AlphaTab - Google Chrome", 900, 600)
    beta_info = _fake_win_info(roi_utils, 3333, "BetaTab - Google Chrome", 900, 600)
    tabs_alpha_selected = [
        {"index": 0, "title": "AlphaTab", "box_px": [10, 10, 100, 35], "selected": True},
        {"index": 1, "title": "BetaTab", "box_px": [110, 10, 200, 35], "selected": False},
    ]
    tabs_beta_selected = [
        {"index": 0, "title": "AlphaTab", "box_px": [10, 10, 100, 35], "selected": False},
        {"index": 1, "title": "BetaTab", "box_px": [110, 10, 200, 35], "selected": True},
    ]

    # Case 1: switch click fails to activate Beta -> MUST NOT send Ctrl+W!
    hotkeys: list[list[str]] = []
    clicks: list[tuple[int, int]] = []
    eng._resolve_desktop_window = lambda focus=False: alpha_info
    eng._focus_and_verify_desktop = lambda info, settle_s=0.16: info
    orig_nav = eng._navigate_desktop

    def nav_alpha_stuck(action, url=None, tab_id=None, **kw):
        if action == "list_tabs":
            return {"mode": "desktop", "hwnd": 3333, "tabs": tabs_alpha_selected}
        return orig_nav(action, url, tab_id, **kw)

    eng._navigate_desktop = nav_alpha_stuck
    with (
        patch.object(cua_engine, "check_corner_failsafe", return_value=False),
        patch.object(cua_engine, "click_abs", side_effect=lambda x, y, **kw: clicks.append((x, y))),
        patch.object(cua_engine, "send_hotkey", side_effect=lambda k: hotkeys.append(k)),
        patch.object(cua_engine.win32gui, "IsWindow", return_value=True),
        patch.object(cua_engine, "inspect_chrome_window", return_value=alpha_info),
    ):
        failed_closed = False
        try:
            orig_nav("close_tab", None, "BetaTab")
        except RuntimeError as exc:
            failed_closed = "desktop_close_tab_not_active" in str(exc)
        assert failed_closed, f"close_tab must fail closed when Beta did not activate; hotkeys={hotkeys}"
        assert hotkeys == [], f"Ctrl+W leaked onto wrong tab: {hotkeys}"

    # Case 2: switch activates Beta -> clicks Beta, verifies active, sends Ctrl+W
    switched = {"beta": False}

    def nav_beta_switches(action, url=None, tab_id=None, **kw):
        if action == "list_tabs":
            return {"mode": "desktop", "hwnd": 3333, "tabs": tabs_beta_selected if switched["beta"] else tabs_alpha_selected}
        return orig_nav(action, url, tab_id, **kw)

    eng._navigate_desktop = nav_beta_switches
    with (
        patch.object(cua_engine, "check_corner_failsafe", return_value=False),
        patch.object(cua_engine, "click_abs", side_effect=lambda x, y, **kw: switched.__setitem__("beta", True)),
        patch.object(cua_engine, "send_hotkey", side_effect=lambda k: hotkeys.append(k)),
        patch.object(cua_engine.win32gui, "IsWindow", return_value=True),
        patch.object(cua_engine, "inspect_chrome_window", side_effect=lambda _h: beta_info if switched["beta"] else alpha_info),
    ):
        res_ok = orig_nav("close_tab", None, "BetaTab")
        assert hotkeys == [["ctrl", "w"]] and res_ok.get("closed_tab") == "BetaTab", (hotkeys, res_ok)

    # Case 3: managed close_tab with missing tab raises ValueError (not StopIteration)
    ms = cua_engine.ManagedChromeSession.__new__(cua_engine.ManagedChromeSession)
    ms.ensure_started = lambda: None
    ms.cdp = object()
    ms.active_target_id = "t1"
    ms._list_targets = lambda: [{"id": "t1", "title": "A", "url": "http://1"}, {"id": "t2", "title": "B", "url": "http://2"}]
    try:
        ms.close_tab("missing-xyz")
        raise AssertionError("Expected ValueError")
    except ValueError:
        pass


def test_a3_uia_helper_fast_and_utf8(mods, base_url: str, uia_timeout: float | None = None) -> None:
    cua_engine, m3 = mods["cua_engine"], mods["m3_auto_collect_chrome"]
    with tempfile.TemporaryDirectory(prefix="cua_reg_a3_", ignore_cleanup_errors=True) as tmp:
        eng = cua_engine.ChromeCUAEngine(profile_dir=Path(tmp), vlm_port=18901, mcp_port=18401)
        try:
            eng.navigate("goto", url=f"{base_url}/zh")
            eng.navigate("new_tab", url=f"{base_url}/beta")
            time.sleep(0.5)

            uia = m3.UIAHelper()
            if uia_timeout is not None:
                uia._QUERY_TIMEOUT_S = float(uia_timeout)
            t0 = time.perf_counter()
            try:
                raw_items = uia.query(eng.chrome.hwnd, 300)
                elapsed = time.perf_counter() - t0
            finally:
                uia.close()

            assert elapsed < 5.0, f"UIAHelper too slow: {elapsed:.2f}s"
            tab_items = [it for it in raw_items if it.get("type") == "TabItem"]
            names = [it.get("name", "") for it in tab_items]
            assert any("中文标签页测试" in n for n in names), f"Missing UTF-8 Chinese tab title: {names}"
            assert any(it.get("selected") is True for it in tab_items), f"Missing UIA TabItem selected flag: {tab_items}"
        finally:
            eng.close()


def test_a4_os_mode_targeting_and_confirm_capture(mods) -> None:
    cua_engine, cpp, roi_utils = mods["cua_engine"], mods["chrome_pilot_panel"], mods["roi_utils"]
    panel, eng = _make_panel_stub(cpp, cua_engine)
    fake_win = _fake_win_info(roi_utils, 4444, "Google Chrome", 800, 600)
    with patch.object(cpp, "find_chrome_windows", return_value=[fake_win]):
        panel._choose_target(("os", 0))
    assert panel.target_mode == "os" and panel.selected_hwnd == 0, (panel.target_mode, panel.selected_hwnd)
    assert eng.target_mode == "os" and eng.desktop_hwnd == 0, (eng.target_mode, eng.desktop_hwnd)

    # Pinned os window closing must fail closed, never fall back to primary screen
    eng.desktop_hwnd = 555555
    with patch.object(cua_engine.win32gui, "IsWindow", return_value=False):
        try:
            eng._resolve_desktop_window()
            raise AssertionError("_resolve_desktop_window silently fell back to primary screen")
        except RuntimeError as exc:
            assert "target_window_closed" in str(exc), exc

    # Panel _worker_managed in os mode must capture OS frame, NEVER managed chrome.capture_full_window
    panel.exec_mode = "confirm"
    panel.model_choice = "M4 · Q4_K_M"
    os_frame = Image.new("RGB", (640, 360), (20, 30, 40))
    eng.locate = MagicMock(return_value={"action": "left_click", "coordinate": [500, 500]})
    eng._capture_desktop_window = MagicMock(return_value=(fake_win, os_frame))
    eng.chrome = SimpleNamespace(hwnd=0, is_running=lambda: False, capture_full_window=MagicMock(side_effect=AssertionError("called managed capture in os mode")))
    res = panel._worker_managed("点击确定")
    assert res.get("_need_confirm") is True and eng._capture_desktop_window.call_count == 1, res


def test_a5_managed_scroll_and_scroll_until_live(mods, base_url: str) -> None:
    cua_engine = mods["cua_engine"]
    with tempfile.TemporaryDirectory(prefix="cua_reg_a5_", ignore_cleanup_errors=True) as tmp:
        eng = cua_engine.ChromeCUAEngine(profile_dir=Path(tmp), vlm_port=18902, mcp_port=18402)
        try:
            eng.navigate("goto", url=f"{base_url}/scroll")
            time.sleep(0.3)
            res_until = eng._run_scroll_until({"until_text": "FOOTER_ANCHOR_ONLY_AT_BOTTOM", "max_scrolls": 6})
            y_until = eng.chrome.cdp.evaluate("window.scrollY")
            assert res_until.get("matched") is True, res_until
            assert int(res_until.get("scrolls_done", 0)) >= 1 and float(y_until) > 0, (res_until, y_until)

            eng.chrome.cdp.evaluate("window.scrollTo(0, 0); window.__scrollByCalls = 0; window.__scrollByDy = 0;")
            time.sleep(0.2)
            res_scroll = eng.act(action_json={"action": "scroll", "coordinate": [500, 500], "direction": "down", "amount": 2})
            st = eng.chrome.cdp.evaluate("({y: window.scrollY, calls: window.__scrollByCalls, jsDy: window.__scrollByDy})")
            assert res_scroll.get("exec_status") == "cdp_executed:scroll", (res_scroll, st)
            assert abs(float(st["y"]) - 240.0) <= 5.0 and int(st["calls"]) == 0, st
        finally:
            eng.close()


def test_a6_failsafe_stale_port_and_new_tab(mods) -> None:
    action_executor, cua_engine, m3, roi_utils = mods["action_executor"], mods["cua_engine"], mods["m3_auto_collect_chrome"], mods["roi_utils"]

    for x, y, exp in ((-200, 2, False), (2, -50, False), (2, 2, True)):
        def fake_gcp(ptr, px=x, py=y):
            pt = ctypes.cast(ptr, ctypes.POINTER(action_executor.wintypes.POINT)).contents
            pt.x = px
            pt.y = py
            return 1
        with patch.object(action_executor.ctypes.windll.user32, "GetCursorPos", side_effect=fake_gcp):
            assert action_executor.check_corner_failsafe() is exp, (x, y, exp)

    # Stale DevToolsActivePort must be unlinked before Popen starts Chrome
    with tempfile.TemporaryDirectory(prefix="cua_reg_a6_", ignore_cleanup_errors=True) as tmp:
        prof = Path(tmp)
        port_file = prof / "DevToolsActivePort"
        port_file.write_text("99999\n/devtools/browser/stale\n", encoding="ascii")
        observed_stale_exists: list[bool] = []

        class FakeProc:
            pid = 987654
            def terminate(self): return None

        def fake_popen(_cmd, **_kw):
            observed_stale_exists.append(port_file.exists())
            port_file.write_text("54321\n/devtools/browser/fresh\n", encoding="ascii")
            return FakeProc()

        with (
            patch.object(m3.subprocess, "Popen", side_effect=fake_popen),
            patch.object(m3.win32gui, "EnumWindows", side_effect=lambda cb, _p: None),
            patch.object(m3.win32gui, "IsWindowVisible", return_value=True),
            patch.object(m3.win32gui, "GetClassName", return_value="Chrome_WidgetWin_1"),
            patch.object(m3.win32process, "GetWindowThreadProcessId", return_value=(1, 987654)),
        ):
            def fake_enum(cb, _p):
                cb(77777, None)
            with patch.object(m3.win32gui, "EnumWindows", side_effect=fake_enum):
                _p, _h, port = m3.launch_chrome_session(Path("chrome.exe"), prof, (800, 600), 1.0, False, "about:blank")
        assert observed_stale_exists == [False] and port == 54321, (observed_stale_exists, port)

    # Managed native + tab button must invoke new_tab('about:blank'), not 'chrome://newtab'
    from action_schema import ParsedAction
    ms = cua_engine.ManagedChromeSession.__new__(cua_engine.ManagedChromeSession)
    new_tab_urls: list[str] = []
    ms.hwnd = 1
    ms.cdp = SimpleNamespace(evaluate=lambda _js: [1000, 600, 1.0], send=lambda *a, **k: {})
    ms._get_uia = lambda: SimpleNamespace(query=lambda *a, **k: [])
    ms.new_tab = lambda url="about:blank": new_tab_urls.append(url) or {"tab_id": "t2", "tabs": []}
    info = _fake_win_info(roi_utils, 1, "C", 1000, 680)
    uia_stub = {"controls": {"new_tab": {"box_px": [200, 10, 240, 40]}}, "tabs": []}
    with patch.object(cua_engine, "classify_uia_controls", return_value=uia_stub):
        ok, status = ms.dispatch_viewport_action(ParsedAction(action="left_click", coordinate=(220, 36), raw="{}"), info, (1000, 680))
    assert ok and status == "cdp_native:new_tab" and new_tab_urls == ["about:blank"], (ok, status, new_tab_urls)


def test_b1_http_mcp_origin_host_and_headers_live(mods) -> None:
    cua_engine, mcp_mod = mods["cua_engine"], mods["chrome_cua_mcp"]
    eng = cua_engine.ChromeCUAEngine.__new__(cua_engine.ChromeCUAEngine)
    eng._mcp_sessions = {}
    eng._lock = threading.RLock()
    eng.register_mcp_session = lambda *a, **k: None
    eng.unregister_mcp_session = lambda *a, **k: None
    eng.prewarm_async = lambda: False
    eng.get_status = lambda: {"state": "idle"}

    srv, _th, mcp_url = mcp_mod.start_http_mcp_background(eng, port=0)
    http_port = srv.server_address[1]
    base = f"http://127.0.0.1:{http_port}"
    try:
        # Cross-origin & null Origin must be 403 across GET, POST, DELETE
        for origin in ("https://evil.example", "null"):
            assert requests.get(f"{base}/health", headers={"Origin": origin}, timeout=5).status_code == 403
            assert requests.get(mcp_url, headers={"Origin": origin}, timeout=5).status_code == 403
            assert requests.post(mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Origin": origin}, timeout=5).status_code == 403
            assert requests.delete(mcp_url, headers={"Origin": origin}, timeout=5).status_code == 403

        # Non-loopback Host header (DNS rebinding) must be 403
        assert requests.get(f"{base}/health", headers={"Host": "evil.example:18390"}, timeout=5).status_code == 403
        assert requests.post(mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Host": "evil.example:18390"}, timeout=5).status_code == 403

        # Non-JSON Content-Type must be 415
        assert requests.post(mcp_url, data='{"jsonrpc":"2.0","id":1,"method":"tools/list"}', headers={"Content-Type": "text/plain"}, timeout=5).status_code == 415

        # Invalid Content-Length must return 400
        with socket.create_connection(("127.0.0.1", http_port), timeout=5) as sock:
            sock.sendall(b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: abc\r\nConnection: close\r\n\r\n")
            resp_raw = sock.recv(1024).decode("utf-8", errors="replace")
        assert resp_raw.startswith("HTTP/1.0 400") or resp_raw.startswith("HTTP/1.1 400"), resp_raw

        # Notification 202 includes Content-Length: 0
        r_notif = requests.post(mcp_url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=5)
        assert r_notif.status_code == 202 and r_notif.headers.get("Content-Length") == "0", (r_notif.status_code, r_notif.headers)

        # Valid localhost & loopback Origin requests continue to work
        assert requests.get(f"{base}/health", timeout=5).status_code == 200
        r_ok = requests.post(mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Origin": "http://localhost:3000"}, timeout=5)
        assert r_ok.status_code == 200 and len(r_ok.json()["result"]["tools"]) == 10, r_ok.text
    finally:
        srv.shutdown()
        srv.server_close()


def test_b2_post_grounding_red_tier_all_paths(mods) -> None:
    cua_engine, cpp, roi_utils = mods["cua_engine"], mods["chrome_pilot_panel"], mods["roi_utils"]
    from action_schema import ParsedAction

    fake_info = _fake_win_info(roi_utils, 6666, "App", 1000, 700)
    frame = Image.new("RGB", (1000, 700), (255, 255, 255))

    # 1) desktop ocr_id clicking "确认支付" must require human confirmation
    _, eng = _make_panel_stub(cpp, cua_engine)
    eng.target_mode = "desktop"
    eng.desktop_hwnd = 6666
    eng._last_ocr_elements = [{"id": 1, "text": "确认支付", "center_norm": [200, 200], "rect_norm": [180, 185, 220, 215]}]
    eng._desktop_ocr = SimpleNamespace(recognize=lambda *a, **k: {"page_text": "确认支付", "ocr_elements": eng._last_ocr_elements, "ocr_ms": 1.0})
    eng._capture_desktop_window = lambda focus=False: (fake_info, frame)
    eng._focus_and_verify_desktop = lambda inf, settle_s=0.16: inf
    with (
        patch.object(cua_engine, "check_corner_failsafe", return_value=False),
        patch.object(cua_engine, "execute_action", MagicMock(return_value=(True, "ok"))) as mock_exec,
    ):
        r_ocr = eng.act(ocr_id=1)
        assert r_ocr["status"] == "needs_human_confirmation" and mock_exec.call_count == 0, r_ocr

    # 2) desktop VLM coord landing on "删除账户" must require human confirmation
    eng._last_ocr_elements = [{"id": 2, "text": "删除账户", "center_norm": [300, 300], "rect_norm": [280, 285, 320, 315]}]
    eng._desktop_ocr = SimpleNamespace(recognize=lambda *a, **k: {"page_text": "删除账户", "ocr_elements": eng._last_ocr_elements, "ocr_ms": 1.0})
    eng.grounder = SimpleNamespace(predict=lambda _img, _inst: ('{"action":"left_click","coordinate":[300,300]}', ParsedAction(action="left_click", coordinate=(300, 300), raw="{}"), 0.1, {}))
    with (
        patch.object(cua_engine, "check_corner_failsafe", return_value=False),
        patch.object(cua_engine, "execute_action", MagicMock(return_value=(True, "ok"))) as mock_exec,
    ):
        r_vlm = eng.act(instruction="点击左上角蓝色图标", strategy="vision")
        assert r_vlm["status"] == "needs_human_confirmation" and mock_exec.call_count == 0, r_vlm

    # 3) managed auto_heal predict_zoomed landing on "确认支付" DOM element must block
    eng.target_mode = "managed"
    dispatched: list[tuple] = []
    red_dom = {"id": 9, "tag": "button", "type": "button", "text": "确认支付", "aria": "", "placeholder": "", "rect_norm": [480, 480, 560, 520], "rect_css": [480, 480, 560, 520]}
    eng.chrome = SimpleNamespace(
        hwnd=6666,
        capture_full_window=lambda: (fake_info, frame),
        extract_dom_snapshot=lambda *a, **k: {"url": "http://127.0.0.1/", "title": "Shop", "elements": [red_dom], "dom_signature": "same"},
        dispatch_viewport_action=lambda parsed, *a, **k: (dispatched.append(parsed.coordinate) or (True, "ok")),
    )
    eng.grounder = SimpleNamespace(
        predict=lambda _img, _inst: ('{"action":"left_click","coordinate":[100,100]}', ParsedAction(action="left_click", coordinate=(100, 100), raw="{}"), 0.1, {}),
        predict_zoomed=lambda _img, _inst, _c: ('{"action":"left_click","coordinate":[520,500]}', ParsedAction(action="left_click", coordinate=(520, 500), raw="{}"), 0.1, {}),
    )
    r_zoom = eng.act(instruction="点击左上角图标", auto_heal=True)
    assert r_zoom["status"] == "needs_human_confirmation" and dispatched == [(100, 100)], (r_zoom, dispatched)

    # 4) Panel _launch_desktop_worker auto mode landing on "确认支付" OCR element must NOT auto-execute
    panel, peng = _make_panel_stub(cpp, cua_engine)
    panel.exec_mode = "auto"
    panel.selected_hwnd = 6666
    captured_res: list[dict] = []

    class CaptureWorker:
        def submit(self, fn, *a, **kw):
            captured_res.append(fn(*a, **kw))
            return SimpleNamespace(done=lambda: True, result=lambda: captured_res[-1])

    panel.worker = CaptureWorker()
    panel._watch = MagicMock()
    peng._desktop_ocr = SimpleNamespace(recognize=lambda *a, **k: {"page_text": "确认支付", "ocr_elements": [{"id": 1, "text": "确认支付", "center_norm": [200, 200], "rect_norm": [180, 185, 220, 215]}]})
    peng.grounder.predict = lambda _img, _inst: ('{"action":"left_click","coordinate":[200,200]}', ParsedAction(action="left_click", coordinate=(200, 200), raw="{}"), 0.1, {})
    with (
        patch.object(cua_engine, "inspect_chrome_window", return_value=fake_info),
        patch.object(cua_engine, "focus_chrome_window", return_value=fake_info),
        patch.object(cua_engine, "capture_chrome_window", return_value=frame),
        patch.object(cua_engine, "execute_confirmed_action", MagicMock(return_value=(True, "ok"))) as mock_exec,
    ):
        panel._launch_desktop_worker(6666, "点击蓝色按钮")
        assert mock_exec.call_count == 0 and captured_res[0]["auto_executed"] is False and captured_res[0]["policy"].tier == "red", captured_res


def test_b3_unified_confirm_token_and_run_steps_live(mods, base_url: str) -> None:
    cua_engine, cpp, roi_utils = mods["cua_engine"], mods["chrome_pilot_panel"], mods["roi_utils"]
    from action_schema import ParsedAction

    fake_info = _fake_win_info(roi_utils, 7777, "Doc", 800, 600)
    frame = Image.new("RGB", (800, 600), (255, 255, 255))

    # 1) click_and_type red instruction + red element_id resolves after 1 confirmation (zero deadlock)
    _, eng = _make_panel_stub(cpp, cua_engine)
    eng.target_mode = "managed"
    eng.chrome = SimpleNamespace(
        hwnd=7777,
        cdp=SimpleNamespace(send=lambda *a, **k: {}, evaluate=lambda *a, **k: {}),
        capture_full_window=lambda: (fake_info, frame),
        window_geometry=lambda: (fake_info, (800, 600)),
        extract_dom_snapshot=lambda *a, **k: {"url": "http://127.0.0.1/", "title": "Doc", "elements": [{"id": 1, "tag": "input", "type": "text", "text": "delete box", "rect_css": [10, 10, 120, 40], "rect_norm": [10, 10, 120, 40]}], "dom_signature": "s1"},
        click_dom_element=lambda *a, **k: (True, "dom_element_click"),
        send_key=lambda *a, **k: None,
    )
    spec_red = {"instruction": "点击 delete 搜索框", "element_id": 1, "text": "doc query"}
    r1 = eng._run_atomic_click_and_type(spec_red)
    assert r1["status"] == "needs_human_confirmation" and r1.get("confirm_token"), r1
    r2 = eng._run_atomic_click_and_type({**spec_red, "confirm_token": r1["confirm_token"]})
    assert r2["status"] == "ok", f"click_and_type deadlocked: {r2}"

    # 2) click_and_type with red typed text ("delete") landing on <input type="password"> must STILL confirm password_input!
    pwd_el = {"id": 2, "tag": "input", "type": "password", "text": "", "aria": "密码", "placeholder": "", "rect_css": [200, 200, 400, 240], "rect_norm": [250, 330, 500, 400]}
    eng.chrome.extract_dom_snapshot = lambda *a, **k: {"url": "http://127.0.0.1/", "title": "Doc", "elements": [pwd_el], "dom_signature": "s2"}
    eng.chrome.dispatch_viewport_action = MagicMock(return_value=(True, "ok"))
    eng.grounder = SimpleNamespace(predict=lambda _img, _inst: ('{"action":"left_click","coordinate":[300,360]}', ParsedAction(action="left_click", coordinate=(300, 360), raw="{}"), 0.1, {}))
    spec_pwd = {"instruction": "点击上方空格", "text": "delete", "auto_heal": False}
    p1 = eng._run_atomic_click_and_type(spec_pwd)
    assert p1["status"] == "needs_human_confirmation" and p1["matched_rule"] == "keyword:delete", p1
    p2 = eng._run_atomic_click_and_type({**spec_pwd, "confirm_token": p1["confirm_token"]})
    assert p2["status"] == "needs_human_confirmation" and p2["matched_rule"] == "password_input", f"Target password check was bypassed: {p2}"
    p3 = eng._run_atomic_click_and_type({**spec_pwd, "confirm_token": p2["confirm_token"]})
    assert p3["status"] == "ok", p3

    # 3) run_steps forwards confirm_token for hotkey/type/scroll/drag, verified live for kind="type"
    seen_tokens: list[tuple[str, str | None]] = []
    eng_mock = _make_panel_stub(cpp, cua_engine)[1]
    eng_mock.target_mode = "desktop"
    eng_mock._is_desktop_like = lambda: False
    eng_mock.observe = lambda **kw: {"url": "http://1", "title": "T", "dom_signature": "s"}
    eng_mock.update_task = lambda *a, **k: None
    eng_mock.act = lambda **kw: (seen_tokens.append((kw["action_json"]["action"], kw.get("confirm_token"))) or {"status": "ok", "local_usage": cua_engine._new_local_usage()})
    eng_mock.run_steps([
        {"kind": "type", "text": "a", "confirm_token": "tok-t"},
        {"kind": "hotkey", "keys": ["ctrl", "c"], "confirm_token": "tok-h"},
        {"kind": "scroll", "direction": "down", "confirm_token": "tok-s"},
        {"kind": "drag", "coordinate": [1, 1], "end_coordinate": [2, 2], "confirm_token": "tok-d"},
    ], auto_dismiss_popup=False, extract_text=False)
    assert seen_tokens == [("type", "tok-t"), ("hotkey", "tok-h"), ("scroll", "tok-s"), ("drag", "tok-d")], seen_tokens

    with tempfile.TemporaryDirectory(prefix="cua_reg_b3_", ignore_cleanup_errors=True) as tmp:
        live_eng = cua_engine.ChromeCUAEngine(profile_dir=Path(tmp), vlm_port=18903, mcp_port=18403)
        try:
            live_eng.navigate("goto", url=f"{base_url}/scroll")
            t1 = live_eng.run_steps([{"kind": "type", "text": "delete"}], auto_dismiss_popup=False)
            assert t1["status"] == "halted:needs_human_confirmation", t1
            tok = t1["trace"][0]["confirm_token"]
            t2 = live_eng.run_steps([{"kind": "type", "text": "delete", "confirm_token": tok}], auto_dismiss_popup=False)
            assert t2["status"] == "completed", t2
        finally:
            live_eng.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scripts-dir", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--uia-timeout", type=float, default=None)
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()

    sdir = str(Path(args.scripts_dir).resolve())
    if sdir not in sys.path:
        sys.path.insert(0, sdir)

    mods = {
        "action_executor": __import__("action_executor"),
        "chrome_cua_mcp": __import__("chrome_cua_mcp"),
        "chrome_pilot_panel": __import__("chrome_pilot_panel"),
        "cua_engine": __import__("cua_engine"),
        "m3_auto_collect_chrome": __import__("m3_auto_collect_chrome"),
        "roi_utils": __import__("roi_utils"),
    }

    srv, base_url = _build_fixture_server()
    tests = [
        ("A1_abort_and_panel_guard", lambda: test_a1_abort_before_input_and_panel_guard(mods)),
        ("A2_desktop_close_tab_verify", lambda: test_a2_desktop_close_tab_active_verify_and_managed_close(mods)),
        ("A3_uia_helper_fast_utf8", lambda: test_a3_uia_helper_fast_and_utf8(mods, base_url, args.uia_timeout)),
        ("A4_os_mode_target_and_confirm", lambda: test_a4_os_mode_targeting_and_confirm_capture(mods)),
        ("A5_managed_scroll_and_until", lambda: test_a5_managed_scroll_and_scroll_until_live(mods, base_url)),
        ("A6_failsafe_stale_port_newtab", lambda: test_a6_failsafe_stale_port_and_new_tab(mods)),
        ("B1_http_origin_host_headers", lambda: test_b1_http_mcp_origin_host_and_headers_live(mods)),
        ("B2_post_grounding_red_tier", lambda: test_b2_post_grounding_red_tier_all_paths(mods)),
        ("B3_unified_confirm_and_steps", lambda: test_b3_unified_confirm_token_and_run_steps_live(mods, base_url)),
    ]
    failed = 0
    try:
        for name, fn in tests:
            try:
                fn()
                print(f"[PASS] {name}")
            except Exception as exc:
                failed += 1
                print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
                if not args.report_only:
                    raise
    finally:
        srv.shutdown()
        srv.server_close()

    print(f"SUMMARY: passed={len(tests) - failed}, failed={failed}, total={len(tests)}")
    return 0 if (failed == 0 or args.report_only) else 1


if __name__ == "__main__":
    raise SystemExit(main())
