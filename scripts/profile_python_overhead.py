"""Break one managed-mode tool call into Python glue vs Chrome/Win32/subprocess/llama.cpp time.

Evidence for the "should this be rewritten in Rust?" question: prints and saves where wall time goes.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import threading
import time

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Perf Fixture</title></head><body>"
    "<h1>Perf fixture</h1><form><input id='q' placeholder='Quick search'><button type='button'>Go</button></form>"
    + "".join(f"<p><a href='#s{i}'>Section link {i}</a> Lorem ipsum dolor sit amet, paragraph {i}.</p>" for i in range(60))
    + "</body></html>"
).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, format: str, *args: object) -> None:
        return


def _ms(fn, n: int = 20) -> dict[str, float]:
    samples = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t) * 1000.0)
    return {"median_ms": round(statistics.median(samples), 3), "min_ms": round(min(samples), 3), "max_ms": round(max(samples), 3), "n": n}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--vlm-port", type=int, default=18246)
    parser.add_argument("--skip-vlm", action="store_true")
    args = parser.parse_args()
    report: dict[str, object] = {}

    t = time.perf_counter()
    subprocess.run([sys.executable, "-c", "pass"], check=True)
    bare = time.perf_counter() - t
    t = time.perf_counter()
    subprocess.run([sys.executable, "-c", f"import sys;sys.path.insert(0,{str(SCRIPT_DIR)!r});import chrome_cua_mcp"], check=True)
    report["process_cold_start"] = {"bare_interpreter_s": round(bare, 3), "import_mcp_server_s": round(time.perf_counter() - t, 3)}

    from chrome_cua_mcp import MCPServerCore
    from cua_engine import ChromeCUAEngine, _compact_dom_elements
    from m0_bench_vlm import encode_image_b64, query_gpu_memory_mib, resize_to_token_budget

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"

    with tempfile.TemporaryDirectory(prefix="cua_prof_", ignore_cleanup_errors=True) as profile:
        engine = ChromeCUAEngine(profile_dir=Path(profile), vlm_port=args.vlm_port)
        core = MCPServerCore(engine)
        try:
            engine.navigate("goto", url=url)
            chrome = engine.chrome
            info, img = chrome.capture_full_window()
            w, h = img.size
            report["frame_size"] = [w, h]

            report["win32_printwindow_capture"] = _ms(chrome.capture_full_window)
            report["cdp_dom_snapshot_roundtrip"] = _ms(lambda: chrome.extract_dom_snapshot(info, w, h, max_elements=60))
            snap = chrome.extract_dom_snapshot(info, w, h, max_elements=60)
            out = {"status": "ok", "mode": "managed", **snap, "elements": _compact_dom_elements(snap["elements"])}
            report["python_compact_plus_json_dumps"] = _ms(lambda: json.dumps({**out, "elements": _compact_dom_elements(snap["elements"])}, ensure_ascii=False), n=200)
            report["observe_total"] = _ms(lambda: engine.observe())
            req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "cua_observe", "arguments": {}}}
            report["mcp_observe_end_to_end"] = _ms(lambda: core.handle_message(req, "prof"))
            report["mcp_tools_list_dispatch"] = _ms(lambda: json.dumps(core.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "prof")), n=200)

            report["nvidia_smi_subprocess"] = _ms(query_gpu_memory_mib, n=8)
            report["get_status_total"] = _ms(engine.get_status, n=8)
            engine.status_listener = lambda _st: None
            report["observe_total_with_panel_listener"] = _ms(lambda: engine.observe(), n=8)
            engine.status_listener = None

            report["pil_resize_plus_png_b64_for_vlm"] = _ms(lambda: encode_image_b64(resize_to_token_budget(img, max_tokens=1024)), n=10)
            if not args.skip_vlm:
                t = time.perf_counter()
                engine.grounder.ensure_started()
                report["llama_server_cold_load_s"] = round(time.perf_counter() - t, 3)
                engine.ask_screen("页面标题是什么？")
                asks = []
                for _ in range(4):
                    t = time.perf_counter()
                    res = engine.ask_screen("页面标题是什么？")
                    asks.append({"wall_ms": round((time.perf_counter() - t) * 1000.0, 1), "infer_ms": round(float(res.get("infer_s", 0.0)) * 1000.0, 1)})
                report["ask_screen_warm"] = asks
        finally:
            engine.close()
            srv.shutdown()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
