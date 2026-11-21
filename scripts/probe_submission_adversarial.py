"""Exercise submission verification against unrelated updates in isolated Chrome."""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from chrome_cua_mcp import MCPServerCore
from cua_engine import ChromeCUAEngine
from m35_e2e_eval import start_e2e_server


def page(mode: str) -> str:
    if mode == 'unrelated_elements':
        update = ("setTimeout(() => { const d=document.getElementById('noise'); "
                  "for(let i=0;i<3;i++){const a=document.createElement('a');a.textContent='Unrelated link '+i;"
                  "a.href='#noise-'+i;d.appendChild(a);}}, 300);")
    elif mode == 'unrelated_query_echo':
        update = ("setTimeout(() => { document.getElementById('noise').textContent = "
                  "'Draft preview: alpha (not submitted)'; }, 300);")
    else:
        update = ''
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><title>Submission test</title>
<body><h1>Submission test</h1><p>READY-FOUND</p>
<input placeholder="Test input"><button type="button" id="next">Continue</button>
<div id="noise"></div><script>
window.__STATE__ = {{submitted:false, continued:false}};
document.querySelector('input').addEventListener('keydown', e => {{if(e.key==='Enter') {{{update}}}}});
document.getElementById('next').addEventListener('click', () => window.__STATE__.continued=true);
</script></body></html>'''


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18239)
    args = parser.parse_args()
    cases = ('unrelated_elements','preexisting_until_text','unrelated_query_echo')
    server, base = start_e2e_server({f'/{name}':page(name) for name in cases})
    rows = []
    try:
        with tempfile.TemporaryDirectory(prefix='cua_submit_adversarial_', ignore_cleanup_errors=True) as profile:
            engine = ChromeCUAEngine(profile_dir=Path(profile), vlm_port=args.port)
            core = MCPServerCore(engine)
            seq = 0

            def call(name, params):
                nonlocal seq
                seq += 1
                r = core.handle_message({'jsonrpc':'2.0','id':seq,'method':'tools/call',
                                         'params':{'name':name,'arguments':params}}, 'adversarial')
                result = r.get('result', {})
                if result.get('isError') or 'error' in r:
                    raise RuntimeError(str(r))
                return json.loads(result['content'][0]['text'])

            try:
                for name in cases:
                    call('cua_navigate', {'action':'goto','url':base+'/'+name})
                    obs = call('cua_observe', {'max_elements':60})
                    element = next(e for e in obs['elements'] if e.get('placeholder') == 'Test input')
                    spec = {'kind':'click_and_type','element_id':element['id'],'text':'alpha',
                            'press_enter':True,'wait_settle':True,'require_submit':True,'max_wait_s':2.5}
                    if name == 'preexisting_until_text':
                        spec['until_text'] = 'READY-FOUND'
                    start = time.perf_counter()
                    result = call('cua_run_steps', {'steps':[spec, {'kind':'act','text_hint':'Continue'}]})
                    truth = engine.chrome.cdp.evaluate('({state:window.__STATE__,noise:document.querySelectorAll("#noise a").length})')
                    row = {'case':name,'wall_s':round(time.perf_counter()-start,3),'status':result.get('status'),
                           'trace':result.get('trace'),'ground_truth':truth,'local_usage':result.get('local_usage')}
                    rows.append(row)
                    print(json.dumps(row,ensure_ascii=False),flush=True)
            finally:
                engine.close()
    finally:
        server.shutdown()
        server.server_close()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__ == '__main__':
    main()
