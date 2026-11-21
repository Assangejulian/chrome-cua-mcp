"""Exercise real-site search and result navigation with an isolated MCP engine."""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit, parse_qs

import cua_engine
from chrome_cua_mcp import MCPServerCore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, default=Path('artifacts/public_search_20261120.json'))
    args = parser.parse_args()
    terms = ['asyncio', 'pathlib', 'json', 'sqlite3', 'statistics', 'logging', 'subprocess', 'threading', 'unittest', 'argparse']
    rows = []
    output = args.out
    output.parent.mkdir(parents=True, exist_ok=True)
    usage, original = [], cua_engine.call_vlm
    def measured(*args, **kwargs):
        answer, counts = original(*args, **kwargs)
        usage.append(counts)
        return answer, counts
    cua_engine.call_vlm = measured
    with tempfile.TemporaryDirectory(prefix='cua_search_eval_', ignore_cleanup_errors=True) as profile:
        engine = cua_engine.ChromeCUAEngine(profile_dir=Path(profile), vlm_port=18235)
        core, seq = MCPServerCore(engine), 0
        def call(name, args, trace):
            nonlocal seq
            seq += 1
            req = {'jsonrpc':'2.0', 'id':seq, 'method':'tools/call', 'params':{'name':name, 'arguments':args}}
            started = time.perf_counter()
            response = core.handle_message(req, 'search_eval')
            res = response.get('result', {})
            content = res.get('content', [])
            data = json.loads(content[0]['text']) if content else {'error': response.get('error')}
            trace.append({'tool':name, 'seconds':round(time.perf_counter()-started,3),
                          'request_bytes':len(json.dumps(req, ensure_ascii=False).encode()),
                          'response_bytes':len(json.dumps(response, ensure_ascii=False).encode()),
                          'error':res.get('isError',False)})
            if res.get('isError') or 'error' in response:
                raise RuntimeError(str(data))
            return data
        try:
            for term in terms:
                started, u0, trace = time.perf_counter(), len(usage), []
                row = {'term':term, 'outcome':'failed', 'calls':trace, 'checks':{}}
                try:
                    call('cua_navigate', {'action':'goto','url':'https://docs.python.org/3/'}, trace)
                    obs = call('cua_observe', {'max_elements':100}, trace)
                    inputs = [e for e in obs.get('elements',[]) if e.get('tag') == 'input' and
                              ('search' in str(e.get('type','')).lower() or 'search' in (e.get('placeholder','')+' '+e.get('aria','')).lower())]
                    if not inputs:
                        row['reason'] = 'no visible search field'
                    else:
                        plan = call('cua_run_steps', {'steps':[{'kind':'click_and_type','element_id':inputs[0]['id'],
                                       'text':term,'clear_first':True,'press_enter':True,'wait_settle':True}]}, trace)
                        search_url = engine.chrome.cdp.evaluate('location.href')
                        query = parse_qs(urlsplit(search_url).query).get('q',[''])[0]
                        row['checks']['search'] = {'url':search_url,'query_matches':query.casefold()==term,'plan_status':plan.get('status')}
                        obs2 = call('cua_observe', {'max_elements':100}, trace)
                        links = [e for e in obs2.get('elements',[]) if e.get('tag')=='a' and e.get('href') and
                                 ('/library/'+term) in urljoin(search_url,e['href']).lower()]
                        if not links:
                            row['reason'] = 'no visible matching module result'
                        else:
                            expected = urljoin(search_url, links[0]['href'])
                            action = call('cua_act', {'element_id':links[0]['id'],'wait_settle':True}, trace)
                            actual = engine.chrome.cdp.evaluate('location.href')
                            page = call('cua_extract', {'max_chars':4000}, trace)
                            row['checks']['result'] = {'expected':expected,'actual':actual,'action':action.get('status'),
                                                       'module_visible':term in page.get('text','').lower()}
                            call('cua_act', {'action_json':{'action':'scroll','coordinate':[500,600],'direction':'down','amount':4}}, trace)
                            pos = engine.chrome.cdp.evaluate('window.scrollY')
                            row['checks']['scrolled'] = pos > 0
                            call('cua_navigate', {'action':'back'}, trace)
                            back = engine.chrome.cdp.evaluate('location.href')
                            row['checks']['return'] = {'url':back,'matches':parse_qs(urlsplit(back).query).get('q',[''])[0].casefold()==term}
                            if query.casefold()==term and urlsplit(actual).path==urlsplit(expected).path and row['checks']['result']['module_visible'] and pos > 0 and row['checks']['return']['matches']:
                                row['outcome'] = 'passed'
                            else:
                                row['reason'] = 'independent state check failed'
                except Exception as exc:
                    row['reason'] = (type(exc).__name__+': '+str(exc))[:250]
                row['wall_s'] = round(time.perf_counter()-started,3)
                row['local_usage'] = usage[u0:]
                rows.append(row)
                output.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps({'term':term,'outcome':row['outcome'],'reason':row.get('reason',''),'wall_s':row['wall_s'],'calls':len(trace)},ensure_ascii=False),flush=True)
        finally:
            engine.close()
            cua_engine.call_vlm = original


if __name__ == '__main__':
    main()
