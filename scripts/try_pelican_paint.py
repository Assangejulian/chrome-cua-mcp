"""Try a real paint site; prototype drag strokes outside the current MCP action set."""
from __future__ import annotations

import base64
import json
import math
import tempfile
import time
from pathlib import Path

from chrome_cua_mcp import MCPServerCore
from cua_engine import ChromeCUAEngine


def circle(cx: float, cy: float, rx: float, ry: float, count: int = 36):
    return [[cx + rx * math.cos(2 * math.pi * i / count), cy + ry * math.sin(2 * math.pi * i / count)] for i in range(count + 1)]


def main() -> None:
    out = Path(__file__).resolve().parent.parent / 'artifacts' / 'pelican_paint_20261120'
    out.mkdir(parents=True, exist_ok=True)
    trace = []
    with tempfile.TemporaryDirectory(prefix='cua_pelican_', ignore_cleanup_errors=True) as profile:
        engine = ChromeCUAEngine(profile_dir=Path(profile), vlm_port=18236)
        core = MCPServerCore(engine)
        seq = 0
        def call(name, args):
            nonlocal seq
            seq += 1
            t = time.perf_counter()
            response = core.handle_message({'jsonrpc':'2.0', 'id':seq, 'method':'tools/call', 'params':{'name':name, 'arguments':args}}, 'pelican')
            result = response.get('result', {})
            if result.get('isError') or 'error' in response:
                raise RuntimeError(str(response))
            data = json.loads(result['content'][0]['text'])
            trace.append({'tool':name, 'seconds':round(time.perf_counter()-t,3), 'result':data})
            return data
        try:
            call('cua_navigate', {'action':'goto', 'url':'https://utilorca.com/paint-online/'})
            locate = call('cua_locate', {'instruction':'点击画布中间的白色绘画区域'})
            call('cua_act', {'action_json':{'action':'scroll','coordinate':[500,700],'direction':'down','amount':3}})
            assert engine.chrome.cdp is not None
            cdp = engine.chrome.cdp
            rect = cdp.evaluate("(() => {let r=document.querySelector('canvas').getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height,vh:innerHeight}})()")
            if rect['y'] < 0 or rect['y'] + rect['h'] > rect['vh']:
                raise RuntimeError(f'Canvas is not fully visible: {rect}')
            # The upper-layer script specifies the drawing geometry. Drag is not an MCP tool.
            groups = [
                ('#1e293b', [circle(250,390,92,92),circle(755,390,92,92),
                    [[250,390],[455,390]], [[755,390],[675,223],[640,208],[604,208]],
                    [[413,248],[483,248]], [[432,248],[457,390]],
                    [[455,390],[475,408]], [[475,408],[495,408]],
                    circle(250,390,9,9,16),circle(755,390,9,9,16)]),
                ('#0ea5e9', [
                    [[250,390],[378,270],[455,390],[250,390]],
                    [[378,270],[638,270],[455,390]], [[638,270],[755,390]],
                    [[250,390],[310,315]], [[637,270],[676,230]]]),
                ('#1e293b', [
                    [[329,162],[355,147],[388,137],[425,133],[465,137],[500,151],[520,173],[521,194],[505,211],[476,224],[440,230],[401,228],[366,217],[343,198],[329,162]],
                    [[365,162],[391,158],[422,165],[451,183],[476,192],[459,207],[431,211],[406,203],[385,186],[365,162]],
                    [[329,162],[305,149],[319,177],[338,184]],
                    [[470,143],[491,123],[508,94],[516,70],[528,55],[545,49],[563,54],[574,68],[575,83],[568,94],[556,99],[547,119],[541,145],[520,173]],
                    circle(550,68,3.5,3.5,12),
                    [[549,79],[574,81],[603,88],[625,102],[601,112],[572,110],[555,102],[548,91],[549,79]],
                    [[387,222],[397,253],[426,281]], [[438,226],[462,255],[489,280]],
                    [[426,281],[443,281]], [[489,280],[508,280]]]),
                ('#f97316', [
                    [[568,78],[610,85],[671,98],[612,103],[568,96],[568,78]],
                    [[575,97],[589,110],[612,117],[637,113],[657,103]],
                    [[397,253],[426,281]], [[462,255],[489,280]]]),
            ]
            count = 0
            for colour, strokes in groups:
                call('cua_act', {'action_json':{'action':'scroll','coordinate':[500,400],'direction':'up','amount':3}})
                palette = call('cua_observe', {'max_elements':80})
                candidates = [e for e in palette.get('elements', []) if e.get('tag') == 'button' and e.get('text') == colour]
                if len(candidates) != 1:
                    raise RuntimeError(f'Expected one visible palette button for {colour}, got {len(candidates)}')
                colour_result = call('cua_act', {'element_id':candidates[0]['id']})
                if colour_result.get('status') != 'ok':
                    raise RuntimeError(f'Could not select palette colour {colour}: {colour_result}')
                call('cua_act', {'action_json':{'action':'scroll','coordinate':[500,700],'direction':'down','amount':3}})
                rect = cdp.evaluate("(() => {let r=document.querySelector('canvas').getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height,vh:innerHeight}})()")
                if rect['y'] < 0 or rect['y'] + rect['h'] > rect['vh']:
                    raise RuntimeError(f'Canvas is not fully visible: {rect}')
                for stroke in strokes:
                    coords = [(rect['x'] + float(x) / 1000 * rect['w'], rect['y'] + float(y) / 536 * rect['h']) for x,y in stroke]
                    x,y = coords[0]
                    cdp.send('Input.dispatchMouseEvent', {'type':'mouseMoved','x':x,'y':y,'button':'none'})
                    cdp.send('Input.dispatchMouseEvent', {'type':'mousePressed','x':x,'y':y,'button':'left','buttons':1,'clickCount':1})
                    for x,y in coords[1:]:
                        cdp.send('Input.dispatchMouseEvent', {'type':'mouseMoved','x':x,'y':y,'button':'left','buttons':1})
                    cdp.send('Input.dispatchMouseEvent', {'type':'mouseReleased','x':x,'y':y,'button':'left','buttons':0,'clickCount':1})
                    count += 1
            dataurl = cdp.evaluate("document.querySelector('canvas').toDataURL('image/png')")
            image_path = out / 'pelican_bicycle.png'
            image_path.write_bytes(base64.b64decode(dataurl.split(',',1)[1]))
            answer = call('cua_ask_screen', {'question':'请简短描述画布上画的内容。'})
            report = {'site':'https://utilorca.com/paint-online/', 'model':'M3.5 Q4', 'model_canvas_location':locate,
                      'canvas_rect_css':rect, 'stroke_count':count, 'image':str(image_path),
                      'model_description':answer.get('answer'),
                      'method':'Upper-layer hand-authored geometry; experimental CDP held-mouse strokes, not native MCP drag; MCP used for navigation, palette and model observation.',
                      'trace':trace}
            (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({'image':str(image_path),'strokes':count,'model_location':locate,'model_description':answer.get('answer')},ensure_ascii=False),flush=True)
        finally:
            engine.close()


if __name__ == '__main__':
    main()
