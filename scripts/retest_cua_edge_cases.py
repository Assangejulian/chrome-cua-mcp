"""Check negative submission and complete local-usage reporting on an isolated fixture."""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

import cua_engine
from chrome_cua_mcp import MCPServerCore
from m35_e2e_eval import build_e2e_pages, start_e2e_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Check negative submission and local-usage reporting on isolated fixture.")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "artifacts" / "cua_edge_retest_20261120.json"),
        help="Output JSON report path.",
    )
    cli_args = parser.parse_args()
    server, base = start_e2e_server(build_e2e_pages())
    original = cua_engine.call_vlm
    observed = []

    def tracked(*args, **kwargs):
        answer, usage = original(*args, **kwargs)
        observed.append(usage)
        return answer, usage

    cua_engine.call_vlm = tracked
    results = {}
    with tempfile.TemporaryDirectory(prefix='cua_retest_edges_', ignore_cleanup_errors=True) as profile:
        engine = cua_engine.ChromeCUAEngine(profile_dir=Path(profile), vlm_port=18237)
        core = MCPServerCore(engine)
        seq = 0

        def call(name, args):
            nonlocal seq
            seq += 1
            request = {'jsonrpc':'2.0', 'id':seq, 'method':'tools/call', 'params':{'name':name,'arguments':args}}
            before = time.perf_counter()
            response = core.handle_message(request, 'retest_edges')
            result = response.get('result', {})
            if result.get('isError') or 'error' in response:
                raise RuntimeError(str(response))
            data = json.loads(result['content'][0]['text'])
            return data, round(time.perf_counter() - before, 3)

        try:
            call('cua_navigate', {'action':'goto','url':base + '/flight'})
            obs, _ = call('cua_observe', {'max_elements':60})
            element = next(e for e in obs['elements'] if e.get('placeholder') == '例如：北京')
            res, seconds = call('cua_run_steps', {'steps':[{'kind':'click_and_type','element_id':element['id'],
                                'text':'上海','press_enter':True,'wait_settle':True}]})
            state = engine.chrome.cdp.evaluate('window.__STATE__')
            results['no_submit'] = {'wall_s':seconds, 'plan_status':res.get('status'),
                                    'submitted':res.get('trace',[{}])[0].get('submitted'),
                                    'ground_truth_searched':state.get('searched'), 'local_usage':res.get('local_usage')}
            observed.clear()
            res, seconds = call('cua_run_steps', {'steps':[{'kind':'dismiss_popup'}], 'auto_dismiss_popup':False})
            results['popup_usage'] = {'wall_s':seconds, 'plan_status':res.get('status'),
                                      'reported_usage':res.get('local_usage'), 'actual_usage':list(observed),
                                      'trace':res.get('trace')}
            observed.clear()
            res, seconds = call('cua_locate', {'instruction':'点击查询航班按钮','zoom':True})
            results['zoom_usage'] = {'wall_s':seconds, 'reported_usage':res.get('local_usage'),
                                     'actual_usage':list(observed), 'action':res.get('action')}
        finally:
            engine.close()
            server.shutdown()
            server.server_close()
            cua_engine.call_vlm = original
    out = Path(cli_args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(results,ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()
