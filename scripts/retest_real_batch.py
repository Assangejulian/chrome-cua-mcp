"""Time batched read-only real-site searches through the project's MCP router."""
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
    parser = argparse.ArgumentParser(description="Time batched read-only real-site searches through MCP router.")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "artifacts" / "cua_real_batch_retest_20261120.json"),
        help="Output JSON report path.",
    )
    cli_args = parser.parse_args()
    rows = []
    with tempfile.TemporaryDirectory(prefix='cua_retest_batch_', ignore_cleanup_errors=True) as profile:
        engine = ChromeCUAEngine(profile_dir=Path(profile), vlm_port=18238)
        core = MCPServerCore(engine)
        try:
            for i, term in enumerate(('asyncio', 'pathlib'), 1):
                args = {'steps':[
                    {'kind':'goto','url':'https://docs.python.org/3/'},
                    {'kind':'click_and_type','text_hint':'Quick search','text':term,'press_enter':True,'wait_settle':True},
                    {'kind':'scroll','direction':'down','amount':3},
                    {'kind':'ask','question':'请简短说出这个页面显示的主要内容。'}],
                    'verify_question':'请简短说出这个页面显示的主要内容。'}
                request = {'jsonrpc':'2.0','id':i,'method':'tools/call','params':{'name':'cua_run_steps','arguments':args}}
                start = time.perf_counter()
                response = core.handle_message(request, 'batch_retest')
                wall = time.perf_counter()-start
                result = response.get('result', {})
                if result.get('isError') or 'error' in response:
                    rows.append({'term':term,'error':response, 'wall_s':round(wall,3)})
                    continue
                content = result.get('content', [])
                payload = json.loads(content[0]['text'])
                truth = engine.chrome.cdp.evaluate('({url:location.href,scroll:window.scrollY})')
                actual_query = parse_qs(urlsplit(truth['url']).query).get('q',[''])[0]
                trace = payload.get('trace', [])
                submit = next((x.get('submitted') for x in trace if x.get('kind') == 'click_and_type'), None)
                rows.append({'term':term, 'wall_s':round(wall,3), 'status':payload.get('status'),
                             'executed_steps':payload.get('executed_steps'), 'submitted':submit,
                             'independent':{'url':truth['url'],'query_matches':actual_query == term,'scroll_y':truth['scroll']},
                             'local_usage':payload.get('local_usage'), 'verification_answer':payload.get('verification_answer'),
                             'trace':trace, 'request_bytes':len(json.dumps(request,ensure_ascii=False).encode()),
                             'response_bytes':len(json.dumps(response,ensure_ascii=False).encode()),
                             'images':sum(c.get('type') == 'image' for c in content)})
                print(json.dumps(rows[-1],ensure_ascii=False), flush=True)
        finally:
            engine.close()
    out = Path(cli_args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__ == '__main__':
    main()
