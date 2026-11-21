"""Measure MCP task outcomes on isolated, deterministic local Chrome pages."""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from datetime import datetime
from pathlib import Path

import cua_engine
from chrome_cua_mcp import MCPServerCore
from m35_e2e_eval import build_e2e_pages, start_e2e_server
from test_cua_mcp import TEST_HTML


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, default=Path(__file__).resolve().parent.parent / 'artifacts' / 'cua_codex_benchmark_20261120.json')
    args = parser.parse_args()
    pages = build_e2e_pages()
    pages['/guard'] = TEST_HTML
    server, base = start_e2e_server(pages)
    original_call = cua_engine.call_vlm
    usage_log = []

    def measured_call(*args, **kwargs):
        answer, usage = original_call(*args, **kwargs)
        usage_log.append(usage)
        return answer, usage

    cua_engine.call_vlm = measured_call
    results = []
    with tempfile.TemporaryDirectory(prefix='cua_bench_', ignore_cleanup_errors=True) as profile:
        engine = cua_engine.ChromeCUAEngine(profile_dir=Path(profile), vlm_port=18232)
        core = MCPServerCore(engine)
        counter = 0
        def call(name, args):
            nonlocal counter
            counter += 1
            request = {'jsonrpc': '2.0', 'id': counter, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}
            started = time.perf_counter()
            response = core.handle_message(request, 'benchmark')
            seconds = time.perf_counter() - started
            result = response.get('result', {})
            content = result.get('content', [])
            payload = json.loads(content[0]['text']) if content else {'error': response.get('error')}
            return payload, {'tool': name, 'seconds': round(seconds, 3), 'request_bytes': len(json.dumps(request, ensure_ascii=False).encode('utf-8')), 'response_bytes': len(json.dumps(response, ensure_ascii=False).encode('utf-8')), 'images': sum(c.get('type') == 'image' for c in content), 'error': result.get('isError', False)}

        def case(name, path, actions, verify):
            start = time.perf_counter()
            start_usage = len(usage_log)
            calls = []
            try:
                nav, rec = call('cua_navigate', {'action': 'goto', 'url': base + path})
                calls.append(rec)
                outputs = []
                for tool, args in actions:
                    out, rec = call(tool, args)
                    outputs.append(out)
                    calls.append(rec)
                state = engine.chrome.cdp.evaluate('window.__STATE__ || window.__TEST__ || null')
                passed, detail = verify(nav, outputs, state)
            except Exception as exc:
                passed, detail, state = False, repr(exc), None
            usages = usage_log[start_usage:]
            result = {'case': name, 'passed': bool(passed), 'detail': detail, 'state': state, 'wall_s': round(time.perf_counter() - start, 3), 'calls': calls, 'local_vlm_calls': len(usages), 'local_usage': usages}
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)

        try:
            case('flight_form', '/flight', [('cua_run_steps', {'steps': [
                {'kind': 'click_and_type', 'instruction': '在出发城市输入框', 'text': '上海'},
                {'kind': 'click_and_type', 'instruction': '在目的城市输入框', 'text': '成都'},
                {'kind': 'act', 'text_hint': '查询航班'}]})],
                lambda n, o, s: (s == {'searched': True, 'from': '上海', 'to': '成都'}, {'status': o[0].get('status')}))
            case('profile_settings', '/prefs', [('cua_run_steps', {'steps': [
                {'kind': 'click_and_type', 'instruction': '在显示昵称输入框', 'text': '林远舟'},
                {'kind': 'act', 'instruction': '点击接收周报提醒复选框'},
                {'kind': 'act', 'text_hint': '保存更改'}]})],
                lambda n, o, s: (s == {'saved': True, 'nickname': '林远舟', 'weeklyAlert': True}, {'status': o[0].get('status')}))
            case('read_table', '/bg/duty', [('cua_ask_screen', {'question': '周四至周日的主值班是谁？只回答姓名。'})],
                lambda n, o, s: ('陈思远' in o[0].get('answer', ''), {'answer': o[0].get('answer')}))
            case('absent_target', '/flight', [('cua_locate', {'instruction': '点击页面中不存在的紫色取消航班按钮'})],
                lambda n, o, s: (o[0].get('action') == 'not_found', {'action': o[0].get('action'), 'status': o[0].get('status')}))
            case('dismiss_popup', '/guard', [('cua_dismiss_popup', {})],
                lambda n, o, s: (s is not None and s.get('modalClosed') is True, {'status': o[0].get('status')}))
            case('payment_guard', '/guard', [('cua_act', {'instruction': '点击确认支付按钮'})],
                lambda n, o, s: (o[0].get('status') == 'needs_human_confirmation' and not s.get('paid'), {'status': o[0].get('status')}))
        finally:
            engine.close()
            server.shutdown()
            server.server_close()
            cua_engine.call_vlm = original_call
    out = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({'timestamp': datetime.now().isoformat(), 'method': 'in-process MCP JSON-RPC; scripted plans; isolated local Chrome; DOM only for independent verification', 'results': results}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Report: {out}', flush=True)


if __name__ == '__main__':
    main()
