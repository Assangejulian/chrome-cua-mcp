"""Compare the same read-only workflow with sequential and batched MCP calls."""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from chrome_cua_mcp import MCPServerCore
from cua_engine import ChromeCUAEngine


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18240)
    args = parser.parse_args()
    rows = []
    with tempfile.TemporaryDirectory(prefix='cua_batch_ab_', ignore_cleanup_errors=True) as profile:
        engine = ChromeCUAEngine(profile_dir=Path(profile), vlm_port=args.port)
        core = MCPServerCore(engine)
        seq = 0

        def call(name, params, records):
            nonlocal seq
            seq += 1
            request = {'jsonrpc':'2.0','id':seq,'method':'tools/call','params':{'name':name,'arguments':params}}
            t = time.perf_counter()
            response = core.handle_message(request, 'batch_ab')
            wall = time.perf_counter() - t
            result = response.get('result', {})
            if result.get('isError') or 'error' in response:
                raise RuntimeError(str(response))
            content = result.get('content', [])
            payload = json.loads(content[0]['text'])
            records.append({'tool':name,'seconds':round(wall,3),
                            'request_bytes':len(json.dumps(request,ensure_ascii=False).encode()),
                            'response_bytes':len(json.dumps(response,ensure_ascii=False).encode()),
                            'images':sum(c.get('type')=='image' for c in content),
                            'local_usage':payload.get('local_usage')})
            return payload

        try:
            engine.grounder.ensure_started()
            engine.chrome.ensure_started()
            search = {'kind':'click_and_type','text_hint':'Quick search','text':'asyncio',
                      'press_enter':True,'wait_settle':True,'require_submit':True}
            scroll = {'kind':'scroll','direction':'down','amount':3}
            question = '请简短说出这个页面显示的主要内容。'
            for mode in ('sequential','batch','batch','sequential'):
                records = []
                started = time.perf_counter()
                row = {'mode':mode}
                try:
                    if mode == 'sequential':
                        nav = call('cua_navigate', {'action':'goto','url':'https://docs.python.org/3/'}, records)
                        sub = call('cua_run_steps', {'steps':[search]}, records)
                        action = call('cua_act', {'action_json':{'action':'scroll','direction':'down','amount':3}}, records)
                        answer = call('cua_ask_screen', {'question':question}, records)
                        status_ok = nav.get('status')=='ok' and sub.get('status')=='completed' and action.get('status')=='ok' and answer.get('status')=='ok'
                        submit = sub.get('trace',[{}])[0].get('submitted')
                        row['answer'] = answer.get('answer')
                    else:
                        plan = call('cua_run_steps', {'steps':[{'kind':'goto','url':'https://docs.python.org/3/'},search,scroll,{'kind':'ask','question':question}]}, records)
                        status_ok = plan.get('status')=='completed' and plan.get('executed_steps')==4
                        submit = next((x.get('submitted') for x in plan.get('trace',[]) if x.get('kind')=='click_and_type'),None)
                        row['answer'] = next((x.get('answer') for x in plan.get('trace',[]) if x.get('kind')=='ask'),None)
                    truth = engine.chrome.cdp.evaluate('({url:location.href,scroll:window.scrollY})')
                    query = parse_qs(urlsplit(truth['url']).query).get('q',[''])[0]
                    row.update({'passed':bool(status_ok and submit is True and query=='asyncio' and truth['scroll']>0),
                                'submitted':submit,'truth':truth})
                except Exception as exc:
                    row.update({'passed':False,'error':str(exc)[:300]})
                row.update({'wall_s':round(time.perf_counter()-started,3),'calls':records})
                rows.append(row)
                args.out.parent.mkdir(parents=True,exist_ok=True)
                args.out.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps({'mode':mode,'passed':row['passed'],'wall_s':row['wall_s'],'calls':len(records)},ensure_ascii=False),flush=True)
        finally:
            engine.close()


if __name__ == '__main__':
    main()
