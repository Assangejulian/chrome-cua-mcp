"""Run read-only, checkpointed public-site MCP chains in an isolated Chrome profile."""
from __future__ import annotations

import argparse
import json
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urldefrag

import cua_engine
from chrome_cua_mcp import MCPServerCore


CASES = [
    *[('Python docs', f'https://docs.python.org/3/{p}') for p in [
        'tutorial/index.html', 'tutorial/introduction.html', 'tutorial/controlflow.html',
        'tutorial/datastructures.html', 'tutorial/modules.html', 'tutorial/inputoutput.html',
        'tutorial/errors.html', 'tutorial/classes.html', 'library/index.html', 'reference/index.html']],
    *[('MDN', f'https://developer.mozilla.org/en-US/docs/{p}') for p in [
        'Web/HTML', 'Web/CSS', 'Web/JavaScript', 'Web/HTTP', 'Web/API',
        'Web/Accessibility', 'Web/Performance', 'Learn_web_development',
        'Web/JavaScript/Guide', 'Web/CSS/Guides']],
    *[('Wikipedia', f'https://en.wikipedia.org/wiki/{p}') for p in [
        'Artificial_intelligence', 'Machine_learning', 'Computer', 'Python_(programming_language)',
        'Internet', 'World_Wide_Web', 'Solar_System', 'Moon', 'Earth', 'Climate_change']],
    *[('NVIDIA', f'https://www.nvidia.com/en-us/{p}') for p in [
        '', 'about-nvidia/', 'technologies/', 'ai/', 'data-center/', 'data-center/dgx-cloud/',
        'solutions/', 'industries/', 'research/', 'self-driving-cars/']],
    *[('NASA', f'https://www.nasa.gov/{p}') for p in [
        '', 'missions/', 'humans-in-space/', 'earth/', 'news/']],
    *[('Dominos', f'https://www.dominos.com/{p}') for p in [
        '', 'menu', 'deals', 'content/nutrition', 'content/allergen-info']],
]


def norm(s: str) -> str:
    return re.sub(r'[^\w]+', ' ', str(s).casefold()).strip()


def safe_link(base: str, item: dict, site: str) -> str | None:
    if item.get('tag') != 'a' or not item.get('text') or not item.get('href'):
        return None
    u = urlsplit(urljoin(base, item['href']))
    b = urlsplit(base)
    if u.scheme not in ('http', 'https') or u.hostname != b.hostname or u.query or u.fragment:
        return None
    if urldefrag(u.geturl())[0].rstrip('/') == urldefrag(base)[0].rstrip('/'):
        return None
    low = (u.path + ' ' + item['text']).lower()
    if any(x in low for x in ('login', 'sign-in', 'signin', 'register', 'account', 'cart',
                               'checkout', 'order', 'buy', 'shop', 'subscribe', 'download',
                               'privacy', 'cookie', 'consent', 'contact', 'careers', 'jobs',
                               'edit', 'delete', 'special:', 'donate', 'logout', 'feedback')):
        return None
    prefixes = {'Python docs': '/3/', 'MDN': '/en-US/docs/', 'Wikipedia': '/wiki/',
                'NVIDIA': '/en-us/', 'NASA': '/', 'Dominos': '/content/'}
    if not u.path.startswith(prefixes[site]):
        return None
    return u.geturl()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=50)
    parser.add_argument('--start', type=int, default=1)
    parser.add_argument('--ids', type=str, default='', help='Comma-separated case IDs to run instead of a range.')
    parser.add_argument('--port', type=int, default=18233)
    parser.add_argument('--out', type=Path, default=Path('artifacts/public_chains_20261120.json'))
    args = parser.parse_args()
    selected = {int(value) for value in args.ids.split(',') if value.strip()}
    assert len(CASES) == 50
    report = {'method': 'Scripted adaptive read-only MCP JSON-RPC chains on public sites, isolated managed Chrome; DOM used for independent verification; not a live upper-model autonomy test',
              'started': datetime.now().isoformat(), 'cases': []}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    original = cua_engine.call_vlm
    usages = []
    def counted(*a, **kw):
        result, usage = original(*a, **kw)
        usages.append(usage)
        return result, usage
    cua_engine.call_vlm = counted
    with tempfile.TemporaryDirectory(prefix='cua_public_eval_', ignore_cleanup_errors=True) as profile:
        engine = cua_engine.ChromeCUAEngine(profile_dir=Path(profile), vlm_port=args.port)
        core = MCPServerCore(engine)
        seq = 0
        def call(name, params, trace):
            nonlocal seq
            seq += 1
            req = {'jsonrpc': '2.0', 'id': seq, 'method': 'tools/call', 'params': {'name': name, 'arguments': params}}
            t = time.perf_counter()
            resp = core.handle_message(req, 'public_benchmark')
            duration = time.perf_counter() - t
            res = resp.get('result', {})
            blocks = res.get('content', [])
            data = json.loads(blocks[0]['text']) if blocks else {'error': resp.get('error')}
            trace.append({'tool': name, 'seconds': round(duration, 3), 'status': data.get('status'),
                          'error': res.get('isError', False), 'request_bytes': len(json.dumps(req, ensure_ascii=False).encode()),
                          'response_bytes': len(json.dumps(resp, ensure_ascii=False).encode()),
                          'images': sum(x.get('type') == 'image' for x in blocks)})
            if res.get('isError') or 'error' in resp:
                raise RuntimeError(str(data))
            return data

        try:
            for i, (site, url) in enumerate(CASES, 1):
                if (selected and i not in selected) or (not selected and (i < args.start or i >= args.start + args.limit)):
                    continue
                t0, u0, trace = time.perf_counter(), len(usages), []
                item = {'id': i, 'site': site, 'start_url': url, 'outcome': 'failed', 'reason': '', 'checks': {}, 'calls': trace}
                try:
                    nav = call('cua_navigate', {'action': 'goto', 'url': url}, trace)
                    observed = call('cua_observe', {'max_elements': 100}, trace)
                    gate = observed.get('auth_or_captcha_gate')
                    title = observed.get('title', '')
                    text = observed.get('text_excerpt', '')
                    page_url = observed.get('url', '')
                    if gate or any(x in (title + ' ' + text[:300]).lower() for x in ('403 forbidden', 'access denied', 'proxy error', 'site can’t be reached', 'site can\'t be reached')) or page_url.startswith('chrome-error:'):
                        item['outcome'], item['reason'] = 'blocked', str(gate or title or 'network error')[:160]
                    elif not text or len(text) < 80:
                        item['outcome'], item['reason'] = 'blocked', 'empty or unavailable content'
                    else:
                        # Independent verification reads state only; it never drives the page.
                        ground = engine.chrome.cdp.evaluate("({h1:(document.querySelector('h1')||{}).innerText||'',url:location.href,scroll:window.scrollY})")
                        item['checks']['source'] = {'url': ground['url'], 'title': title, 'heading': ground['h1'][:160]}
                        extracted = call('cua_extract', {'max_chars': 4000}, trace)
                        ask = call('cua_ask_screen', {'question': 'What is the main heading visible on this page? Reply with only the heading.'}, trace)
                        answer = str(ask.get('answer', '')).strip()
                        heading = ground['h1'].strip()
                        hn, an = norm(heading), norm(answer)
                        item['checks']['read'] = {'text_available': bool(extracted.get('text')), 'answer': answer[:300],
                                                  'heading_match': bool(hn and an and (hn in an or an in hn) and len(an) >= 3)}
                        scroll = call('cua_act', {'action_json': {'action': 'scroll', 'coordinate': [500, 600], 'direction': 'down', 'amount': 4}}, trace)
                        after = call('cua_observe', {'max_elements': 100}, trace)
                        item['checks']['scroll'] = {'before': ground['scroll'], 'after': after.get('scroll_y'), 'status': scroll.get('status')}
                        candidates = [(e, safe_link(after.get('url', page_url), e, site)) for e in after.get('elements', [])]
                        candidates = [(e, href) for e, href in candidates if href]
                        if not candidates:
                            item['reason'] = 'no safe visible same-site content link after scrolling'
                        else:
                            target, href = candidates[0]
                            item['checks']['target'] = {'text': target['text'], 'expected_url': href}
                            click = call('cua_act', {'element_id': target['id'], 'wait_settle': True}, trace)
                            dest = call('cua_extract', {'max_chars': 4000}, trace)
                            destination = engine.chrome.cdp.evaluate('location.href')
                            expected = urldefrag(href)[0].rstrip('/')
                            actual = urldefrag(destination)[0].rstrip('/')
                            clicked = actual == expected
                            item['checks']['link'] = {'status': click.get('status'), 'actual_url': destination, 'destination_matches': clicked, 'text_available': bool(dest.get('text'))}
                            if clicked:
                                back = call('cua_navigate', {'action': 'back'}, trace)
                                returned = engine.chrome.cdp.evaluate('location.href')
                                back_ok = urldefrag(returned)[0].rstrip('/') == urldefrag(ground['url'])[0].rstrip('/')
                                item['checks']['return'] = {'url': returned, 'matches': back_ok}
                                if back_ok and extracted.get('text') and dest.get('text') and item['checks']['read']['heading_match'] and (after.get('scroll_y') or 0) > ground['scroll']:
                                    item['outcome'] = 'passed'
                                else:
                                    item['reason'] = 'one or more independent checks failed'
                            else:
                                item['reason'] = 'clicked destination did not match selected link'
                except Exception as exc:
                    item['reason'] = (type(exc).__name__ + ': ' + str(exc))[:300]
                item['wall_s'] = round(time.perf_counter() - t0, 3)
                item['local_usage'] = usages[u0:]
                report['cases'].append(item)
                report['updated'] = datetime.now().isoformat()
                args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps({'id': i, 'site': site, 'outcome': item['outcome'], 'seconds': item['wall_s'], 'calls': len(trace), 'reason': item['reason']}, ensure_ascii=False), flush=True)
        finally:
            engine.close()
            cua_engine.call_vlm = original


if __name__ == '__main__':
    main()
