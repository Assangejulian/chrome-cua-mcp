"""A/B the local-VLM background prewarm (CUA_PREWARM=1 vs 0) under identical conditions.

Every trial is a fresh Python process with a fresh Chrome profile, driven through the MCP router against a
local fixture page (no network). Conditions are interleaved (on/off, off/on, ...) and one warm-up trial per
scenario is discarded. Scenarios:
  visual_first : first call is run_steps[goto, scroll, ask]          -> first task needs the model at once
  dom_first    : navigate, observe, (think gap), ask_screen           -> model first needed a few calls in
  dom_only     : navigate + 3x observe, never any vision             -> pure cost side of prewarm
After the first task, `warm_tasks` identical follow-up tasks (run_steps[scroll, ask]) are timed.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import itertools
import json
import os
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

MARK = "RESULT_JSON:"
QUESTION = "当前屏幕最上方可见的 Section 编号是多少？只回答数字。"
_COLORS = ("#c0392b", "#2471a3", "#1e8449", "#b7950b", "#6c3483", "#117a65")
PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Prewarm Fixture</title></head><body style='font:16px sans-serif'>"
    + "".join(
        f"<h2>Section {i}</h2><p><a href='#s{i}'>Link {i}</a> Fixture paragraph number {i} for the prewarm benchmark.</p>"
        f"<div style='height:90px;width:{200 + 37 * (i % 9)}px;background:{_COLORS[i % 6]}'></div>"
        for i in range(1, 61)
    )
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


def run_worker(args: argparse.Namespace) -> None:
    from benchmark_llama_flags import _proc_mem_mib
    from chrome_cua_mcp import MCPServerCore
    from cua_engine import ChromeCUAEngine
    from m0_bench_vlm import query_gpu_memory_mib

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    row: dict[str, object] = {"scenario": args.scenario, "prewarm_env": os.environ.get("CUA_PREWARM", "<unset>"), "think_s": args.think_s}

    def gpu_used() -> int:
        return int(query_gpu_memory_mib()["used_mib"])

    with tempfile.TemporaryDirectory(prefix="cua_prewarm_ab_", ignore_cleanup_errors=True) as profile:
        gpu0 = gpu_used()
        engine = ChromeCUAEngine(profile_dir=Path(profile), vlm_port=args.vlm_port)
        core = MCPServerCore(engine)
        seq = itertools.count(1)

        def call(name: str, params: dict[str, object]) -> tuple[dict, float]:
            req = {"jsonrpc": "2.0", "id": next(seq), "method": "tools/call", "params": {"name": name, "arguments": params}}
            t = time.perf_counter()
            resp = core.handle_message(req, "prewarm_ab")
            wall = time.perf_counter() - t
            res = resp.get("result", {})
            if "error" in resp or res.get("isError"):
                raise RuntimeError(json.dumps(resp, ensure_ascii=True)[:600])
            return json.loads(res["content"][0]["text"]), wall

        def mem(tag: str) -> None:
            proc = engine.grounder.proc
            row[f"vlm_running_{tag}"] = engine.grounder.is_running()
            row[f"vram_delta_mib_{tag}"] = gpu_used() - gpu0
            llama = _proc_mem_mib(proc.pid) if proc is not None and proc.poll() is None else {}
            row[f"llama_commit_mib_{tag}"] = llama.get("commit_mib", 0)
            row[f"llama_workingset_mib_{tag}"] = llama.get("working_set_mib", 0)
            row[f"python_commit_mib_{tag}"] = _proc_mem_mib(os.getpid()).get("commit_mib", 0)

        try:
            t0 = time.perf_counter()
            if args.scenario == "visual_first":
                out, wall = call("cua_run_steps", {"steps": [
                    {"kind": "goto", "url": url}, {"kind": "scroll", "direction": "down", "amount": 3}, {"kind": "ask", "question": QUESTION},
                ]})
                row["first_task_s"] = round(wall, 3)
                row["first_task_status"] = out.get("status")
                row["first_task_local_usage"] = out.get("local_usage")
            elif args.scenario == "dom_first":
                _nav, nav_s = call("cua_navigate", {"action": "goto", "url": url})
                _obs, obs_s = call("cua_observe", {})
                row["vlm_running_before_first_visual"] = engine.grounder.is_running()
                time.sleep(args.think_s)
                ask, ask_s = call("cua_ask_screen", {"question": QUESTION})
                row.update(first_nav_s=round(nav_s, 3), first_observe_s=round(obs_s, 3), first_visual_s=round(ask_s, 3),
                           first_task_s=round(nav_s + obs_s + ask_s, 3), first_task_status=ask.get("status"),
                           first_task_local_usage=ask.get("local_usage"))
            else:  # dom_only
                _nav, nav_s = call("cua_navigate", {"action": "goto", "url": url})
                obs_walls = []
                for _ in range(3):
                    time.sleep(0.5)
                    obs_walls.append(call("cua_observe", {})[1])
                row.update(first_nav_s=round(nav_s, 3), first_task_s=round(nav_s + sum(obs_walls), 3),
                           observe_median_ms=round(statistics.median(obs_walls) * 1000.0, 2), first_task_status="ok")
                time.sleep(args.settle_s)  # let an in-flight prewarm finish so its memory is visible
            row["first_task_end_since_t0_s"] = round(time.perf_counter() - t0, 3)
            mem("after_first")

            warm, infer = [], []
            if args.scenario != "dom_only":
                for _ in range(args.warm_tasks):
                    out, wall = call("cua_run_steps", {"steps": [
                        {"kind": "scroll", "direction": "down", "amount": 3}, {"kind": "ask", "question": QUESTION},
                    ]})
                    if out.get("status") != "completed":
                        raise RuntimeError(f"warm task status={out.get('status')}")
                    warm.append(round(wall, 3))
                    infer.extend(round(float(t.get("infer_s", 0.0)), 3) for t in out.get("trace", []) if t.get("kind") == "ask")
                row["warm_task_s"] = warm
                row["warm_task_median_s"] = round(statistics.median(warm), 3)
                row["warm_ask_infer_median_s"] = round(statistics.median(infer), 3) if infer else None
                mem("after_warm")
            row["ok"] = True
        except Exception as exc:
            row["ok"] = False
            row["error"] = str(exc)[:500]
        finally:
            engine.close()
            srv.shutdown()
    print(MARK + json.dumps(row, ensure_ascii=True), flush=True)


def _llama_procs() -> int:
    out = subprocess.run(["tasklist"], capture_output=True, text=True, errors="ignore").stdout.lower()
    return out.count("llama-server")


def _perm_p(a: list[float], b: list[float]) -> float | None:
    """Exact two-sided permutation p-value for the difference in means (small n only)."""
    if len(a) < 3 or len(b) < 3 or len(a) + len(b) > 16:
        return None
    pooled = a + b
    observed = abs(statistics.mean(a) - statistics.mean(b))
    hits = total = 0
    for idx in itertools.combinations(range(len(pooled)), len(a)):
        chosen = set(idx)
        ga = [pooled[i] for i in chosen]
        gb = [pooled[i] for i in range(len(pooled)) if i not in chosen]
        total += 1
        hits += abs(statistics.mean(ga) - statistics.mean(gb)) >= observed - 1e-12
    return round(hits / total, 4)


def _stats(vals: list[float]) -> dict[str, float]:
    return {
        "n": len(vals), "median": round(statistics.median(vals), 3), "mean": round(statistics.mean(vals), 3),
        "min": round(min(vals), 3), "max": round(max(vals), 3), "stdev": round(statistics.stdev(vals), 3) if len(vals) > 1 else 0.0,
    }


def run_orchestrator(args: argparse.Namespace) -> None:
    order: list[str] = []
    for i in range(args.n):
        order += ["1", "0"] if i % 2 == 0 else ["0", "1"]
    plan = [("0", True)] + [(c, False) for c in order]  # first trial is a discarded warm-up
    report: dict[str, object] = {
        "scenario": args.scenario, "n_per_condition": args.n, "warm_tasks": args.warm_tasks, "think_s": args.think_s,
        "settle_s": args.settle_s, "order": order, "llama_procs_before": _llama_procs(), "trials": [],
    }
    rows: dict[str, list[dict]] = {"1": [], "0": []}
    for idx, (cond, warmup) in enumerate(plan):
        env = dict(os.environ, CUA_PREWARM=cond, PYTHONIOENCODING="utf-8")
        cmd = [sys.executable, "-u", str(Path(__file__).resolve()), "--worker", "--scenario", args.scenario,
               "--vlm-port", str(args.base_port + idx), "--warm-tasks", str(args.warm_tasks),
               "--think-s", str(args.think_s), "--settle-s", str(args.settle_s)]
        t = time.perf_counter()
        try:
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
            line = next((ln for ln in reversed(proc.stdout.splitlines()) if ln.startswith(MARK)), None)
            row = json.loads(line[len(MARK):]) if line else {"ok": False, "error": (proc.stderr or proc.stdout)[-400:]}
        except subprocess.TimeoutExpired:
            row = {"ok": False, "error": "worker_timeout_240s"}
        row.update(trial=idx, condition="on" if cond == "1" else "off", warmup=warmup, process_wall_s=round(time.perf_counter() - t, 2))
        report["trials"].append(row)
        if not warmup and row.get("ok"):
            rows[cond].append(row)
        print(f"[{idx + 1}/{len(plan)}] prewarm={row['condition']}{' (warm-up, discarded)' if warmup else ''} ok={row.get('ok')} "
              f"first={row.get('first_task_s')} warm_med={row.get('warm_task_median_s')} vram_first={row.get('vram_delta_mib_after_first')} "
              f"err={row.get('error', '')}", flush=True)
        time.sleep(args.cooldown_s)

    report["llama_procs_after"] = _llama_procs()
    keys = sorted({k for r in rows["1"] + rows["0"] for k, v in r.items()
                   if isinstance(v, (int, float)) and not isinstance(v, bool) and k not in {"trial", "think_s", "process_wall_s"}})
    summary: dict[str, object] = {}
    print(f"\n| metric ({args.scenario}, think={args.think_s}s) | prewarm ON median [min..max] n | prewarm OFF median [min..max] n | ON-OFF median | perm p |")
    print("| --- | --- | --- | --- | --- |")
    for k in keys:
        on = [float(r[k]) for r in rows["1"] if isinstance(r.get(k), (int, float))]
        off = [float(r[k]) for r in rows["0"] if isinstance(r.get(k), (int, float))]
        if not on or not off:
            continue
        s_on, s_off = _stats(on), _stats(off)
        p = _perm_p(on, off)
        summary[k] = {"on": s_on, "off": s_off, "median_diff_on_minus_off": round(s_on["median"] - s_off["median"], 3), "perm_p": p}
        print(f"| {k} | {s_on['median']} [{s_on['min']}..{s_on['max']}] n={s_on['n']} | {s_off['median']} [{s_off['min']}..{s_off['max']}] n={s_off['n']} "
              f"| {summary[k]['median_diff_on_minus_off']} | {p} |")
    for flag in ("vlm_running_after_first", "vlm_running_before_first_visual"):
        counts = {c: [bool(r.get(flag)) for r in rows[c] if flag in r] for c in ("1", "0")}
        if counts["1"] or counts["0"]:
            summary[flag] = {"on_true": sum(counts["1"]), "on_n": len(counts["1"]), "off_true": sum(counts["0"]), "off_n": len(counts["0"])}
            print(f"{flag}: ON {sum(counts['1'])}/{len(counts['1'])}, OFF {sum(counts['0'])}/{len(counts['0'])}")
    failed = [r for r in report["trials"] if not r.get("ok")]
    print(f"failed trials: {len(failed)}; llama-server processes before/after: {report['llama_procs_before']}/{report['llama_procs_after']}")
    report["summary"] = summary
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {args.out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--scenario", choices=["visual_first", "dom_first", "dom_only"], default="visual_first")
    parser.add_argument("--n", type=int, default=6, help="Measured trials per condition (a warm-up trial is added and discarded).")
    parser.add_argument("--warm-tasks", type=int, default=5)
    parser.add_argument("--think-s", type=float, default=0.0, help="dom_first: simulated upper-model gap before the first visual call.")
    parser.add_argument("--settle-s", type=float, default=8.0, help="dom_only: wait before sampling memory.")
    parser.add_argument("--cooldown-s", type=float, default=3.0)
    parser.add_argument("--base-port", type=int, default=18300)
    parser.add_argument("--vlm-port", type=int, default=18300)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.worker:
        run_worker(args)
        return
    if args.out is None:
        parser.error("--out is required")
    run_orchestrator(args)


if __name__ == "__main__":
    main()
