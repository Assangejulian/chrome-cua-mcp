"""Small local MCP HTTP client with per-call metrics for supervised live evaluation."""
from __future__ import annotations
import argparse
import base64
import json
import os
import tempfile
import time
from pathlib import Path
import requests

STATE = Path(tempfile.gettempdir()) / 'chrome_pilot_live_eval_state.json'
URL = 'http://127.0.0.1:18390/mcp'

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('name')
    ap.add_argument('arguments', nargs='?', default='{}')
    args = ap.parse_args()
    if args.name == 'init':
        r = requests.post(URL, json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','clientInfo':{'name':'Codex Live Evaluation','version':'1.0'},'capabilities':{}}}, timeout=20)
        r.raise_for_status()
        state={'sid':r.headers['Mcp-Session-Id'],'started_at':time.time(),'calls':[]}
        STATE.write_text(json.dumps(state),encoding='utf-8')
        result=r.json().get('result',{})
        print(json.dumps({'initialized':True,'server':result.get('serverInfo'),'instructions':bool(result.get('instructions'))},ensure_ascii=False));return
    state=json.loads(STATE.read_text(encoding='utf-8'))
    headers={'Mcp-Session-Id':state['sid']}
    if args.name == 'close':
        requests.delete(URL,headers=headers,timeout=10).raise_for_status()
        print(json.dumps({'closed':True,'total_wall_s':round(time.time()-state['started_at'],2),'calls':state['calls']},ensure_ascii=False)); STATE.unlink(missing_ok=True); return
    payload={'jsonrpc':'2.0','id':len(state['calls'])+2,'method':'tools/call','params':{'name':args.name,'arguments':json.loads(args.arguments)}}
    started=time.perf_counter()
    r=requests.post(URL,json=payload,headers=headers,timeout=120)
    elapsed=time.perf_counter()-started
    r.raise_for_status();msg=r.json(); result=msg.get('result',{}); content=result.get('content',[])
    data=json.loads(content[0]['text']) if content else {'error':msg.get('error')}
    images=[x for x in content if x.get('type')=='image']
    image_bytes=0
    if images:
        raw=base64.b64decode(images[0]['data']);image_bytes=len(raw)
        image_path=Path(tempfile.gettempdir())/'chrome_pilot_live_eval.jpg'
        image_path.write_bytes(raw);data['saved_screenshot']=str(image_path)
    rec={'tool':args.name,'seconds':round(elapsed,3),'request_bytes':len(json.dumps(payload,ensure_ascii=False).encode()),'response_bytes':len(r.content),'image_bytes':image_bytes,'status':data.get('status'),'is_error':result.get('isError',False),'usage':data.get('usage'),'infer_s':data.get('infer_s')}
    state['calls'].append(rec);STATE.write_text(json.dumps(state),encoding='utf-8')
    if 'ocr_elements' in data and 'elements' in data:
        data.pop('elements')
    for k in ('elements','ocr_elements'):
        if k in data:
            data[k]=[{j:x.get(j) for j in ('id','text','center_norm','rect_norm') if j in x} for x in data[k]]
    print(json.dumps({'metrics':rec,'data':data},ensure_ascii=False))

if __name__=='__main__':main()
