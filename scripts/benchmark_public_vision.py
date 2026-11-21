"""Measure non-mutating visual link localization on real public pages."""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

import cua_engine
from benchmark_public_chains import CASES, safe_link
from chrome_cua_mcp import MCPServerCore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, default=Path('artifacts/public_vision_20261120.json'))
    args = parser.parse_args()
    choices = [1, 2, 3, 4, 5, 11, 12, 13, 14, 15, 21, 22, 23, 24, 25]
    records = []
    usage = []
    original = cua_engine.call_vlm
    def measured(*args, **kwargs):
        answer, counts = original(*args, **kwargs)
        usage.append(counts)
        return answer, counts
    cua_engine.call_vlm = measured
    output = args.out
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cua_public_vision_', ignore_cleanup_errors=True) as profile:
        engine = cua_engine.ChromeCUAEngine(profile_dir=Path(profile), vlm_port=18234)
        core = MCPServerCore(engine)
        seq = 0
        def call(name, args):
            nonlocal seq
            seq += 1
            request = {'jsonrpc': '2.0', 'id': seq, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}
            response = core.handle_message(request, 'vision_eval')
            result = response.get('result', {})
            if result.get('isError') or 'error' in response:
                raise RuntimeError(str(response))
            return json.loads(result['content'][0]['text'])
        try:
            for index in choices:
                site, url = CASES[index-1]
                started, u0 = time.perf_counter(), len(usage)
                row = {'id': index, 'site': site, 'url': url, 'outcome': 'failed'}
                try:
                    call('cua_navigate', {'action': 'goto', 'url': url})
                    obs = call('cua_observe', {'max_elements': 60})
                    els = [e for e in obs.get('elements', []) if safe_link(obs.get('url', url), e, site)
                           and 5 <= len(e.get('text', '')) <= 65 and e.get('rect_norm')]
                    if not els:
                        row['outcome'], row['reason'] = 'blocked', 'no eligible visible link'
                    else:
                        e = els[len(els)//2]
                        label, box = e['text'], e['rect_norm']
                        result = call('cua_locate', {'instruction': f'点击页面里的「{label}」链接', 'zoom': False})
                        xy = result.get('coordinate')
                        hit = bool(xy and len(xy) == 2 and box[0] <= xy[0] <= box[2] and box[1] <= xy[1] <= box[3])
                        row.update({'label': label, 'box_norm': box, 'prediction': xy, 'action': result.get('action'),
                                    'outcome': 'passed' if hit and result.get('action') == 'left_click' else 'failed'})
                except Exception as exc:
                    row['reason'] = str(exc)[:250]
                row['wall_s'] = round(time.perf_counter() - started, 3)
                row['usage'] = usage[u0:]
                records.append(row)
                output.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps(row, ensure_ascii=False), flush=True)
        finally:
            engine.close()
            cua_engine.call_vlm = original


if __name__ == '__main__':
    main()
