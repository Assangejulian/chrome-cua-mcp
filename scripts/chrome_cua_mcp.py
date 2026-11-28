"""Dual-transport (Stdio + Streamable HTTP) MCP 2025-03-26 server for Chrome Pilot CUA."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sys
import threading
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from cua_engine import ChromeCUAEngine

MCP_PROTOCOL_VERSION = "2025-03-26"
SERVER_INFO = {"name": "chrome-pilot-cua", "version": "0.3.0"}
MCP_INSTRUCTIONS = (
    "Chrome Pilot CUA is text-first and local-model-autonomous to save upper-LLM tokens and wall time:\n"
    "1) Zero-screenshot default: cua_observe, cua_navigate, cua_act, and cua_run_steps return real url, title, "
    "page_text/text_excerpt, clickable elements/ocr_elements, and click_effect directly in JSON. Only pass "
    "include_screenshot=true when raw pixel inspection is truly required.\n"
    "2) Use local Qwen3.5 (M4) before requesting screenshots: call cua_ask_screen(question, around_last_click=...) "
    "or pass verify_question to cua_run_steps to inspect post text, heart/toggle state, or counters (~0.2-1.2s). Note: "
    "verification_answer is only a local VLM text summary, not an authoritative business assertion.\n"
    "3) Universal Windows Computer Use (mode='os'): call cua_navigate(action='list_windows', all_apps=True) to "
    "enumerate all desktop apps, then cua_navigate(action='focus_window', hwnd=...) or set_mode(mode='os', hwnd=0) "
    "for full primary screen. Supports drag via action_json/run_steps and extended keys (F1-F12, home/end/pgup/pgdn).\n"
    "4) Prefer direct URLs over manual search typing in Chrome (e.g. https://x.com/search?q=from%3AOfficialLoganK%20gemini&f=live).\n"
    "5) Batch 2-12 operations into ONE cua_run_steps call using structured steps: kind='goto', 'click_and_type' "
    "(atomic click+clear+paste+enter+settle; pass require_submit=true when subsequent steps depend on form/search submission, "
    "or expect_submit=false for multiline newlines/shortcuts; for async results without navigation provide a specific new until_text), "
    "'scroll_until', 'wait_until', 'act', 'ask', 'dismiss_popup', 'drag'.\n"
    "6) Prefer element_id (managed) / ocr_id / text_hint for labeled text controls (0ms); use natural-language "
    "instruction for icons or relative spatial targets.\n"
    "7) Task lifecycle feedback: for multi-step user tasks, call cua_task(event='start', title=..., total_steps=...) "
    "at the start and ALWAYS call cua_task(event='done', message=...) (or 'error') when finished so the floating "
    "bar immediately shows the green completion banner and plays the soft chime."
)

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "cua_observe",
        "description": (
            "Observe desktop or managed Chrome: returns URL, title, page_text/text_excerpt, clickable DOM/OCR elements "
            "with [0,1000] coords, CAPTCHA/login gate check, and optional screenshot (include_screenshot=False default)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_screenshot": {"type": "boolean", "default": False},
                "screenshot_format": {"type": "string", "enum": ["jpeg", "png"], "default": "jpeg"},
                "annotate_som": {"type": "boolean", "default": False, "description": "Draw numbered ID badges on screenshot."},
                "max_elements": {"type": "integer", "default": 60, "minimum": 1, "maximum": 150},
                "detail": {"type": "string", "enum": ["brief", "full"], "default": "brief"},
            },
        },
    },
    {
        "name": "cua_navigate",
        "description": (
            "Navigate, wait for frame settle, and return page URL/title/OCR-or-DOM text; "
            "or switch target mode ('desktop' Chrome, 'managed' Chrome, or 'os' any Windows app / full screen) and focus windows."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "goto",
                        "back",
                        "forward",
                        "reload",
                        "list_tabs",
                        "new_tab",
                        "switch_tab",
                        "close_tab",
                        "list_windows",
                        "focus_window",
                        "set_mode",
                    ],
                    "default": "goto",
                },
                "url": {"type": "string", "description": "Target http/https URL for goto or new_tab."},
                "tab_id": {"type": ["string", "integer"], "description": "Tab ID, index, or window title/process substring."},
                "mode": {"type": "string", "enum": ["desktop", "managed", "os"], "description": "Target mode for action='set_mode'."},
                "hwnd": {"type": "integer", "description": "Target window HWND for set_mode/focus_window (0 = full primary screen in mode='os')."},
                "all_apps": {"type": "boolean", "default": False, "description": "When action='list_windows', also list all visible Windows desktop applications."},
                "max_elements": {"type": "integer", "default": 45, "description": "Managed: cap returned elements."},
                "max_text_chars": {"type": "integer", "default": 2500, "description": "Managed: cap text_excerpt."},
                "detail": {"type": "string", "enum": ["brief", "full"], "default": "brief"},
                "include_screenshot": {"type": "boolean", "default": False},
                "screenshot_format": {"type": "string", "enum": ["jpeg", "png"], "default": "jpeg"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "cua_act",
        "description": (
            "Execute one GUI action via DOM element_id, OCR ocr_id/text_hint (0ms), action_json, or M4 visual grounding "
            "(with pixel+OCR effect verification, zoom self-healing on misses, optional wait_settle, and Red-tier guard)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "instruction": {"type": "string", "description": "Single-step Chinese/English GUI action instruction."},
                "element_id": {"type": "integer", "description": "DOM element id returned by cua_observe (managed)."},
                "ocr_id": {"type": "integer", "description": "OCR element id returned by cua_observe (desktop)."},
                "text_hint": {"type": "string", "description": "Exact button/tab label to match deterministically before M3.5."},
                "action_json": {"type": "object", "description": "Explicit Phase-1 action JSON payload."},
                "strategy": {"type": "string", "enum": ["hybrid", "vision", "dom", "ocr"], "default": "hybrid"},
                "auto_heal": {"type": "boolean", "default": True},
                "wait_settle": {"type": "boolean", "default": False, "description": "Wait for page/frame stabilization and return updated text."},
                "scroll_retry": {"type": "integer", "default": 0, "description": "Extra scroll-down-and-re-ground rounds when M3.5 reports not_found (0=off; search/scroll-worded instructions auto-enable 2). Model-initiated scroll actions always chain re-grounding up to 2 rounds."},
                "include_text": {"type": "boolean", "default": False, "description": "Include post-action OCR/DOM text excerpt."},
                "confirm_token": {"type": "string", "description": "One-time token after human confirms a Red-tier step."},
                "include_screenshot": {"type": "boolean", "default": False},
                "screenshot_format": {"type": "string", "enum": ["jpeg", "png"], "default": "jpeg"},
            },
        },
    },
    {
        "name": "cua_run_steps",
        "description": (
            "Execute up to 12 sequential steps autonomously on localhost (goto, click_and_type, scroll_until, "
            "wait_until, act, ask, dismiss_popup, hotkey/type/scroll) with popup healing, Red-tier/CAPTCHA guards, "
            "and optional local VLM verify_question—returning full text summary without screenshots unless requested. "
            "Returns status='completed' when all steps finished and any expected Enter submission was observed; "
            "'completed_unverified_submit' when actions finished (require_submit=false) without observing Enter submission; "
            "or 'halted:submit_not_observed' when a require_submit=true step did not observe submission (skipping remaining steps). "
            "Always verify critical business state independently rather than relying solely on status or verification_answer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {
                        "oneOf": [
                            {"type": "string"},
                            {
                                "type": "object",
                                "properties": {
                                    "kind": {
                                        "type": "string",
                                        "enum": [
                                            "act",
                                            "goto",
                                            "click_and_type",
                                            "scroll_until",
                                            "wait_until",
                                            "wait_settle",
                                            "dismiss_popup",
                                            "ask",
                                            "hotkey",
                                            "type",
                                            "scroll",
                                            "drag",
                                        ],
                                    },
                                    "url": {"type": "string"},
                                    "instruction": {"type": "string"},
                                    "target": {"type": "string"},
                                    "element_id": {"type": "integer"},
                                    "ocr_id": {"type": "integer"},
                                    "text_hint": {"type": "string"},
                                    "action_json": {"type": "object"},
                                    "strategy": {"type": "string"},
                                    "auto_heal": {"type": "boolean"},
                                    "confirm_token": {"type": "string"},
                                    "text": {"type": "string"},
                                    "clear_first": {"type": "boolean"},
                                    "press_enter": {"type": "boolean"},
                                    "require_submit": {
                                        "type": "boolean",
                                        "default": False,
                                        "description": "For click_and_type with press_enter=true: halt as halted:submit_not_observed if submission is unverified. An input echo alone is not proof; for async results without navigation, provide a specific new until_text and verify business state independently.",
                                    },
                                    "expect_submit": {
                                        "type": "boolean",
                                        "description": "For click_and_type with press_enter=true: set false for newlines/shortcuts where no submission is expected.",
                                    },
                                    "wait_settle": {"type": "boolean"},
                                    "until_text": {"type": ["string", "array"]},
                                    "coordinate": {
                                        "type": "array",
                                        "items": {"type": "integer"},
                                        "minItems": 2,
                                        "maxItems": 2,
                                    },
                                    "end_coordinate": {
                                        "type": "array",
                                        "items": {"type": "integer"},
                                        "minItems": 2,
                                        "maxItems": 2,
                                    },
                                    "direction": {"type": "string"},
                                    "amount": {"type": "integer"},
                                    "max_scrolls": {"type": "integer"},
                                    "max_wait_s": {"type": "number"},
                                    "keys": {"type": "array", "items": {"type": "string"}},
                                    "question": {"type": "string"},
                                    "around_last_click": {"type": "boolean"},
                                },
                            },
                        ]
                    },
                    "minItems": 1,
                    "maxItems": 12,
                },
                "stop_on_popup": {"type": "boolean", "default": True},
                "auto_dismiss_popup": {"type": "boolean", "default": True},
                "max_steps": {"type": "integer", "default": 12, "minimum": 1, "maximum": 12},
                "include_screenshot": {"type": "boolean", "default": False},
                "screenshot_on_error": {"type": "boolean", "default": False},
                "screenshot_format": {"type": "string", "enum": ["jpeg", "png"], "default": "jpeg"},
                "verify_question": {"type": "string", "description": "Optional question answered locally via M3.5 at plan end."},
                "extract_text": {"type": "boolean", "default": True},
                "detail": {"type": "string", "enum": ["brief", "full"], "default": "brief", "description": "full also echoes step inputs and raw_prediction."},
            },
            "required": ["steps"],
        },
    },
    {
        "name": "cua_ask_screen",
        "description": (
            "Ask the local M3.5 VLM to read/verify screen details (e.g. heart color/count, latest post, loading status) "
            "and return a concise pure-text answer (~0.2-1.2s) without transferring raw screenshots."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Factual visual question to ask about the current page."},
                "around_last_click": {"type": "boolean", "default": False, "description": "Use a 2x zoomed crop around the last click."},
                "include_screenshot": {"type": "boolean", "default": False},
                "screenshot_format": {"type": "string", "enum": ["jpeg", "png"], "default": "jpeg"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "cua_locate",
        "description": "Preview M3.5 visual grounding coordinates without dispatching any mouse or keyboard input.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "instruction": {"type": "string"},
                "zoom": {"type": "boolean", "default": False, "description": "Run a 2x zoomed local crop refinement pass."},
            },
            "required": ["instruction"],
        },
    },
    {
        "name": "cua_dismiss_popup",
        "description": (
            "Attempt to close an active modal, banner, or dialog overlay via DOM close button, M3.5 click, "
            "or Escape, and report local_usage (including usage_complete status)."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cua_extract",
        "description": "Extract clean visible text (DOM in managed, WinRT OCR in desktop) and links from the current page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "max_chars": {"type": "integer", "default": 4000, "minimum": 100, "maximum": 20000},
            },
        },
    },
    {
        "name": "cua_status",
        "description": "Inspect local CUA status: active model, VRAM usage, managed Chrome state, URL, harness, and task progress.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cua_task",
        "description": (
            "Announce task lifecycle ('start', 'progress', 'done', 'error') on the floating Chrome Pilot command bar "
            "with step counter, completion badge, and optional soft chime."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "event": {"type": "string", "enum": ["start", "progress", "done", "error"]},
                "title": {"type": "string", "description": "Short task title."},
                "message": {"type": "string", "description": "Current step detail or completion summary."},
                "step": {"type": "integer", "minimum": 0},
                "total_steps": {"type": "integer", "minimum": 0},
                "play_sound": {"type": "boolean", "default": True},
            },
            "required": ["event"],
        },
    },
]


class MCPServerCore:
    """Protocol-compliant JSON-RPC 2.0 router for chrome-pilot-cua."""

    def __init__(self, engine: ChromeCUAEngine) -> None:
        self.engine = engine

    def handle_message(self, msg: dict[str, Any], session_id: str | None = None) -> dict[str, Any] | None:
        req_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        if req_id is None:
            # Notification (e.g. notifications/initialized, notifications/cancelled)
            return None

        try:
            if method == "initialize":
                cinfo = dict(params.get("clientInfo") or {})
                self.engine.register_mcp_session(
                    session_id or "stdio",
                    str(cinfo.get("name", "")),
                    str(cinfo.get("version", "")),
                )
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": SERVER_INFO,
                        "instructions": MCP_INSTRUCTIONS,
                    },
                }
            if method == "ping":
                return {"jsonrpc": "2.0", "id": req_id, "result": {}}
            if method == "tools/list":
                return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOL_DEFINITIONS}}
            if method == "tools/call":
                name = str(params.get("name", ""))
                args = dict(params.get("arguments") or {})
                self.engine.mark_mcp_activity(session_id, name)
                result = self._call_tool(name, args)
                return {"jsonrpc": "2.0", "id": req_id, "result": result}
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(exc)},
            }

    def _call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        dispatch = {
            "cua_observe": lambda a: self.engine.observe(
                include_screenshot=bool(a.get("include_screenshot", False)),
                annotate_som=bool(a.get("annotate_som", False)),
                max_elements=int(a.get("max_elements", 60)),
                screenshot_format=str(a.get("screenshot_format", "jpeg")),
                detail=str(a.get("detail", "brief")),
            ),
            "cua_navigate": lambda a: self.engine.navigate(
                action=str(a.get("action", "goto")),
                url=a.get("url"),
                tab_id=a.get("tab_id"),
                mode=a.get("mode"),
                hwnd=a.get("hwnd"),
                include_screenshot=bool(a.get("include_screenshot", False)),
                screenshot_format=str(a.get("screenshot_format", "jpeg")),
                max_elements=int(a.get("max_elements", 45)),
                max_text_chars=int(a.get("max_text_chars", 2500)),
                detail=str(a.get("detail", "brief")),
                all_apps=bool(a.get("all_apps", False)),
            ),
            "cua_act": lambda a: self.engine.act(
                instruction=a.get("instruction"),
                element_id=a.get("element_id"),
                action_json=a.get("action_json"),
                strategy=str(a.get("strategy", "hybrid")),
                auto_heal=bool(a.get("auto_heal", True)),
                confirm_token=a.get("confirm_token"),
                include_screenshot=bool(a.get("include_screenshot", False)),
                screenshot_format=str(a.get("screenshot_format", "jpeg")),
                ocr_id=a.get("ocr_id"),
                text_hint=a.get("text_hint"),
                wait_settle=bool(a.get("wait_settle", False)),
                include_text=bool(a.get("include_text", False)),
                scroll_retry=int(a.get("scroll_retry", 0)),
            ),
            "cua_run_steps": lambda a: self.engine.run_steps(
                steps=list(a["steps"]),
                stop_on_popup=bool(a.get("stop_on_popup", True)),
                auto_dismiss_popup=bool(a.get("auto_dismiss_popup", True)),
                max_steps=int(a.get("max_steps", 12)),
                include_screenshot=bool(a.get("include_screenshot", False)),
                screenshot_on_error=bool(a.get("screenshot_on_error", False)),
                screenshot_format=str(a.get("screenshot_format", "jpeg")),
                verify_question=a.get("verify_question"),
                extract_text=bool(a.get("extract_text", True)),
                detail=str(a.get("detail", "brief")),
            ),
            "cua_ask_screen": lambda a: self.engine.ask_screen(
                question=str(a["question"]),
                around_last_click=bool(a.get("around_last_click", False)),
                include_screenshot=bool(a.get("include_screenshot", False)),
                screenshot_format=str(a.get("screenshot_format", "jpeg")),
            ),
            "cua_locate": lambda a: self.engine.locate(
                instruction=str(a["instruction"]),
                zoom=bool(a.get("zoom", False)),
            ),
            "cua_dismiss_popup": lambda _a: self.engine.dismiss_popup(),
            "cua_extract": lambda a: self.engine.extract(
                selector=a.get("selector"),
                max_chars=int(a.get("max_chars", 4000)),
            ),
            "cua_status": lambda _a: self.engine.get_status(),
            "cua_task": lambda a: self.engine.update_task(
                event=str(a["event"]),
                title=a.get("title"),
                message=a.get("message"),
                step=a.get("step"),
                total_steps=a.get("total_steps"),
                play_sound=bool(a.get("play_sound", True)),
            ),
        }
        if name not in dispatch:
            return {"content": [{"type": "text", "text": json.dumps({"error": f"Unknown tool: {name}"})}], "isError": True}
        if name not in {"cua_status", "cua_task"}:
            # Opt-in (CUA_PREWARM=1): load the local VLM in the background while Chrome starts/navigates
            self.engine.prewarm_async()

        try:
            payload = dispatch[name](args)
        except Exception as exc:
            return {"content": [{"type": "text", "text": json.dumps({"error": str(exc)}, ensure_ascii=False)}], "isError": True}

        image_b64 = None
        image_mime = "image/jpeg"
        if isinstance(payload, dict) and payload.get("screenshot_b64"):
            image_b64 = str(payload["screenshot_b64"])
            image_mime = str(payload.get("screenshot_mime") or "image/jpeg")
        clean_payload = (
            {k: v for k, v in payload.items() if k not in {"screenshot_b64", "screenshot_mime"}}
            if isinstance(payload, dict)
            else payload
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": json.dumps(clean_payload, ensure_ascii=False, separators=(",", ":"))}]
        if image_b64:
            content.append({"type": "image", "data": image_b64, "mimeType": image_mime})
        return {"content": content, "isError": False}


def run_stdio(engine: ChromeCUAEngine) -> int:
    """Serve MCP JSON-RPC 2.0 over newline-delimited stdin/stdout."""
    # MCP streams are UTF-8 regardless of the Windows system code page.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    core = MCPServerCore(engine)
    try:
        for line in sys.stdin:
            raw = line.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as exc:
                err = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
                real_stdout.write(json.dumps(err, ensure_ascii=False) + "\n")
                real_stdout.flush()
                continue
            resp = core.handle_message(msg, session_id="stdio")
            if resp is not None:
                real_stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                real_stdout.flush()
    finally:
        sys.stdout = real_stdout
        engine.unregister_mcp_session("stdio")
        engine.close()
    return 0


def create_http_server(engine: ChromeCUAEngine, host: str = "127.0.0.1", port: int = 18390) -> ThreadingHTTPServer:
    """Create a localhost Streamable HTTP MCP 2025-03-26 server."""
    core = MCPServerCore(engine)
    sessions: dict[str, str] = {}
    sessions_lock = threading.Lock()

    class MCPHTTPHandler(BaseHTTPRequestHandler):
        def _send_json(self, code: int, body: dict[str, Any] | list[Any], headers: dict[str, str] | None = None) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            if headers:
                for k, v in headers.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send_json(200, {"ok": True, **SERVER_INFO, **engine.get_status()})
                return
            if self.path == "/mcp":
                self._send_json(
                    405,
                    {"error": "Method Not Allowed: use POST or DELETE on /mcp"},
                    headers={"Allow": "POST, DELETE"},
                )
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            if self.path != "/mcp":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}})
                return

            sid = self.headers.get("Mcp-Session-Id")
            extra_headers: dict[str, str] = {}
            if isinstance(payload, dict) and payload.get("method") == "initialize":
                sid = secrets.token_hex(12)
                cinfo = dict((payload.get("params") or {}).get("clientInfo") or {})
                with sessions_lock:
                    sessions[sid] = str(cinfo.get("name", ""))
                extra_headers["Mcp-Session-Id"] = sid
            elif sid:
                extra_headers["Mcp-Session-Id"] = sid

            if isinstance(payload, list):
                resps = [r for r in (core.handle_message(item, session_id=sid) for item in payload) if r is not None]
                if not resps:
                    self.send_response(202)
                    self.end_headers()
                    return
                self._send_json(200, resps, headers=extra_headers)
                return

            resp = core.handle_message(payload, session_id=sid)
            if resp is None:
                self.send_response(202)
                for k, v in extra_headers.items():
                    self.send_header(k, v)
                self.end_headers()
                return
            self._send_json(200, resp, headers=extra_headers)

        def do_DELETE(self) -> None:
            if self.path != "/mcp":
                self.send_response(404)
                self.end_headers()
                return
            sid = self.headers.get("Mcp-Session-Id")
            if sid:
                with sessions_lock:
                    removed = sid in sessions
                    sessions.pop(sid, None)
                if removed:
                    engine.unregister_mcp_session(sid)
            self._send_json(200, {"closed": True, "session_id": sid})

        def log_message(self, format: str, *args: Any) -> None:
            return

    class QuietHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = True

        def handle_error(self, request: Any, client_address: Any) -> None:
            return

    return QuietHTTPServer((host, port), MCPHTTPHandler)


def start_http_mcp_background(
    engine: ChromeCUAEngine,
    host: str = "127.0.0.1",
    port: int = 18390,
) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    server = create_http_server(engine, host=host, port=port)
    actual_port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="chrome-cua-mcp-http")
    thread.start()
    return server, thread, f"http://{host}:{actual_port}/mcp"


def client_config(kind: str = "stdio", fmt: str = "json", url: str = "http://127.0.0.1:18390/mcp") -> str:
    """MCP client configuration for this checkout, wherever it lives: fmt 'json' (mcpServers) or 'codex' (config.toml)."""
    if kind == "http":
        entry: dict[str, Any] = {"url": url}
    else:
        python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
        if not python.is_file():  # pythonw has no stdio, so never hand that one out
            python = Path(sys.executable).with_name("python.exe") if Path(sys.executable).name.lower() == "pythonw.exe" else Path(sys.executable)
        entry = {"command": str(python), "args": [str(Path(__file__).resolve()), "--stdio"]}
    if fmt == "codex":
        lines = [f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in entry.items()]  # JSON strings are valid TOML basic strings
        return "[mcp_servers.chrome-pilot-cua]\n" + "\n".join(lines) + "\n"
    return json.dumps({"mcpServers": {"chrome-pilot-cua": entry}}, indent=2, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Chrome Pilot CUA dual-transport MCP server.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stdio", action="store_true", help="Serve JSON-RPC 2.0 over stdin/stdout (default).")
    mode.add_argument("--http", action="store_true", help="Serve Streamable HTTP MCP over 127.0.0.1.")
    parser.add_argument("--port", type=int, default=18390)
    parser.add_argument("--vlm-port", type=int, default=18086)
    parser.add_argument("--profile-dir", type=Path, default=None)
    parser.add_argument("--target-mode", choices=["managed", "desktop", "os"], default="managed")
    parser.add_argument("--print-config", choices=["json", "codex"], help="Print the Stdio MCP client config for this checkout and exit.")
    args = parser.parse_args()
    if args.print_config:
        sys.stdout.reconfigure(encoding="utf-8")
        print(client_config("stdio", args.print_config))
        return 0

    engine = ChromeCUAEngine(
        profile_dir=args.profile_dir,
        vlm_port=args.vlm_port,
        mcp_port=args.port,
        target_mode=args.target_mode,
    )
    if args.http:
        server = create_http_server(engine, host="127.0.0.1", port=args.port)
        print(f"[MCP HTTP] Listening on http://127.0.0.1:{server.server_address[1]}/mcp", file=sys.stderr, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            server.server_close()
            engine.close()
        return 0
    return run_stdio(engine)


if __name__ == "__main__":
    raise SystemExit(main())
