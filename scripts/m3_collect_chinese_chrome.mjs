import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import path from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { setTimeout as sleep } from 'node:timers/promises';

const repository = fileURLToPath(new URL('..', import.meta.url));
const python = path.join(repository, '.venv', 'Scripts', 'python.exe');
const chrome = process.env.CHROME_EXE || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const scale = 1.25;
const systemPrompt = 'You are a Chrome browser GUI action assistant. Given a Chrome window screenshot and a user instruction, output strictly one JSON object for the next action using [0, 1000] normalized integer coordinates. Do not output explanations or markdown.';

const trainFamilies = [
  ['bookshop','山岚书屋','图书专题','书名搜索','作者检索','新书预订','旧书回收','城市散文','青山来信','简装画册','#29594c','cards'],
  ['transit','青川交通','出行服务','出发站','到达站','查看线路','取消预约','桂花路站','环湖西门','江湾码头','#283f6a','sidebar'],
  ['cookbook','燕麦厨房','菜谱索引','输入食材','料理名称','保存菜谱','发布菜谱','酸梅汤','菌菇豆腐','番茄炖牛腩','#9b472e','columns'],
  ['community','桥西邻里','社区服务','活动名称','报名备注','查看公告','删除公告','周末纸艺课','社区合唱','楼下小花园','#613f76','cards'],
  ['library','星河图书馆','馆藏系统','关键词','索书号','续借图书','归还图书','植物漫游指南','城市手记','太空常识','#395a3b','compact'],
  ['studio','北窗工坊','作品管理','项目标题','材料备注','保存草稿','清空草稿','木纹练习','夕阳色谱','梅花书签','#854b32','sidebar'],
  ['weather','松原气象','天气查询','城市名称','区县名称','订阅预报','关闭预报','宁波','景德镇','阿勒泰','#20516f','columns'],
  ['garden','春禾园艺','种植日志','植物名称','养护记录','添加记录','导出记录','迷迭香','早春育苗','窗边薄荷','#446744','cards'],
  ['museum','云际展馆','参观管理','展览名称','预约留言','查看展讯','取消展讯','古地图展','山海绘卷','青铜艺术','#59456b','compact'],
  ['parcel','白鹭驿站','包裹查询','包裹备注','代收备注','查看包裹','删除包裹','书桌零件','织物样品','陶瓷试件','#785037','columns'],
];
const evalFamilies = [
  ['observatory','白塔观星站','观测预约','观测目标','同行说明','预约时段','取消时段','近地行星','双星观测','晚霞摄影','#364977','hero'],
  ['theatre','半城剧社','排练日程','场次名称','道具清单','添加场次','隐藏场次','纸伞和彩灯','独幕喜剧','雨夜排练','#803e59','ledger'],
  ['bicycle','远岸单车','维护预约','维修项目','车辆备注','查看工单','关闭工单','检查刹车','后轮调校','更换内胎','#395a49','split'],
  ['science','鲸岛科学馆','课程管理','课程名称','教学用具','保存课程','删除课程','显微观察','月相实验','磁力小课','#543d8f','gallery'],
];
const trainActions = ['type','type','left_click','type','scroll','type','not_found','type','left_click','type','scroll','type','not_found','left_click','type','type','scroll','left_click','type','not_found'];
const evalActions = ['type','left_click','scroll','type','left_click','not_found'];
const contexts = ['首页','分类索引','快速查询','最近使用','历史记录','操作中心'];
const snippets = ['今日安排','常用操作','主题导航','最新公告','近期更新','本周计划','帮助索引','已保存项目','操作说明','精选推荐','服务时间','更多选项'];

function buildCases(families, split) {
  return families.flatMap((family, familyIndex) => contexts.map((context, variant) => {
    const index = familyIndex * contexts.length + variant;
    const [group, brand, topic, firstField, secondField, primary, absent, ...rest] = family;
    const [firstText, secondText, thirdText, accent, layout] = rest;
    const values = [firstText, secondText, thirdText];
    const action = split === 'train' ? trainActions[index % trainActions.length] : evalActions[variant];
    const target = action === 'type' ? (variant % 2 ? 'secondary' : 'primary') : action === 'scroll' ? 'scroll' : 'action';
    const label = target === 'primary' ? firstField : secondField;
    const text = values[(index + familyIndex) % 3];
    const pastTop = (familyIndex + variant) % 2 === 1;
    const direction = pastTop ? 'up' : 'down';
    const amount = variant % 3 + 1;
    const instruction = action === 'type' ? `在「${label}」输入「${text}」，不要提交。`
      : action === 'left_click' ? `点击「${primary}」按钮。`
      : action === 'scroll' ? `把「${topic}」列表向${pastTop ? '上' : '下'}滚动 ${amount} 格。`
      : `点击「${absent}」按钮。`;
    const marker = [111 + index % 45, 36 + index % 31, (split === 'train' ? 151 : 197) + index % 28];
    return { split, group, brand, topic, layout, accent, context, index, variant, firstField, secondField,
      primary, absent, values, text, direction, pastTop, amount, target, instruction, action, marker,
      smallTarget: action === 'left_click' && variant === 4,
      id: `${split}-${group}-${String(variant + 1).padStart(2, '0')}` };
  }));
}

function page(sample) {
  const cards = snippets.map((name, index) => `<article><i>${String(index + 1).padStart(2,'0')}</i><div><b>${sample.values[index % 3]} · ${name}</b><small>${sample.brand} / ${sample.context}</small></div><span>待查看</span></article>`).join('');
  const isDark = sample.variant === 5 || (sample.layout === 'gallery' && sample.variant % 2 === 0);
  const until = sample.variant === 3 ? '录入模式' : '操作面板';
  return `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>${sample.brand}｜${sample.context}</title>
<style>
*{box-sizing:border-box} body{margin:0;--accent:${sample.accent};--surface:${isDark ? '#212934' : '#ffffff'};--page:${isDark ? '#171d27' : '#eef1f3'};--ink:${isDark ? '#eef3fa' : '#172635'};--dim:${isDark ? '#a9b8c9' : '#536479'};--edge:${isDark ? '#445369' : '#d6dee8'};background:var(--page);color:var(--ink);font:15px 'Microsoft YaHei UI','Microsoft YaHei',sans-serif} .origin{position:fixed;z-index:20;top:0;left:0;width:16px;height:8px;background:rgb(${sample.marker.join(',')})} header{background:var(--accent);color:#fff;padding:17px 5%;display:flex;align-items:center;justify-content:space-between} header b{font-size:21px} header span{margin-left:20px} main{max-width:1130px;margin:auto;padding:23px 33px} h1{margin:0;font-size:33px;letter-spacing:1px} p{color:var(--dim);margin:8px 0 19px} .workspace{display:grid;grid-template-columns:1fr 1.35fr;gap:20px} .panel,.list-box{background:var(--surface);border:1px solid var(--edge);border-radius:13px;padding:19px;min-width:0} h2{font-size:17px;margin:0 0 14px} .fields{display:grid;gap:12px} label{display:grid;gap:5px;font-weight:600} input{width:100%;height:38px;border:1px solid var(--edge);border-radius:6px;background:var(--page);color:var(--ink);padding:0 10px;font:inherit} input:focus{outline:2px solid var(--accent);outline-offset:1px} .actions{display:flex;flex-wrap:wrap;align-items:center;gap:9px;margin-top:15px} button{font:inherit;padding:8px 13px;border:1px solid var(--edge);border-radius:6px;background:var(--surface);color:var(--ink)} #action{background:var(--accent);color:#fff;border-color:var(--accent)} .small #action{font-size:12px;padding:3px 5px;border-radius:3px} #scroll{height:244px;overflow:auto;border:1px solid var(--edge);border-radius:7px;overscroll-behavior:contain} article{padding:11px;display:grid;grid-template-columns:37px 1fr auto;align-items:center;gap:9px;border-bottom:1px solid var(--edge)} article i{font-style:normal;text-align:center;background:var(--page);padding:5px} article span, small{display:block;color:var(--dim);font-size:12px} aside{display:none} footer{color:var(--dim);margin-top:18px;font-size:13px}
.sidebar main,.split main{padding-left:195px;position:relative} .sidebar aside,.split aside{display:grid;position:absolute;top:30px;left:19px;gap:13px;padding:17px;background:var(--surface);width:155px;border:1px solid var(--edge)} .split .workspace{grid-template-columns:1.25fr .85fr} .columns .list-box{grid-column:1;grid-row:1} .columns .panel{grid-column:2;grid-row:1} .compact main{max-width:960px} .compact .workspace{grid-template-columns:1fr 1fr} .compact .panel,.compact .list-box{border-radius:3px} .hero header,.gallery header{padding-block:26px} .hero h1{font-size:43px} .hero .workspace{grid-template-columns:1.12fr .88fr} .ledger .workspace{display:flex;flex-direction:column-reverse;gap:10px} .ledger #scroll{height:133px} .ledger .fields{grid-template-columns:1fr 1fr} .ledger .panel{padding:12px 18px} .ledger .actions{margin-top:8px} .gallery .workspace{grid-template-columns:1.25fr 1fr} .gallery article:nth-child(even){background:var(--page)} .shift .panel{padding-top:28px} .swap .fields{direction:rtl} .swap .fields>*{direction:ltr} .show-note footer{border-top:1px solid var(--edge);padding-top:12px}
</style><body class="${sample.layout} ${sample.smallTarget ? 'small' : ''} ${sample.variant % 3 === 1 ? 'shift' : ''} ${sample.variant % 2 ? 'show-note' : ''}">
<div class="origin" aria-hidden="true"></div><header><b>${sample.brand}</b><div><span>导航</span><span>${sample.context}</span><span>帮助</span></div></header>
<main><aside><b>${sample.topic}</b><span>概览</span><span>我的收藏</span><span>消息记录</span><span>系统设置</span></aside>
<h1>${sample.topic}</h1><p>${sample.context} / ${snippets[sample.variant]}</p><div class="workspace"><section class="panel"><h2>${until}</h2>
<div class="fields"><label>${sample.firstField}<input id="primary" autocomplete="off" placeholder="请输入${sample.firstField}"></label><label>${sample.secondField}<input id="secondary" autocomplete="off" placeholder="请输入${sample.secondField}"></label></div>
<div class="actions"><button id="action" type="button">${sample.primary}</button><button type="button">${sample.primary.slice(0,2)}全部</button><button type="button">${sample.variant % 2 ? '更多' : '重置'}</button></div></section>
<section class="list-box"><h2>${sample.topic} · 列表</h2><div id="scroll" tabindex="0">${cards}</div></section></div><footer>${sample.brand} · ${sample.variant + 1} / 6</footer></main>
<script>document.querySelector('#action').onclick=()=>{document.body.dataset.clicked='true'};document.fonts.ready.then(()=>{const list=document.querySelector('#scroll');list.scrollTop=${sample.pastTop ? 375 : 0};document.body.dataset.ready='yes';});</script></body></html>`;
}

async function connect(url) {
  const socket = new WebSocket(url);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, {once:true});
    socket.addEventListener('error', reject, {once:true});
  });
  let sequence = 0;
  const pending = new Map();
  socket.addEventListener('message', event => {
    const reply = JSON.parse(event.data);
    const callback = pending.get(reply.id);
    if (callback) {
      pending.delete(reply.id);
      callback(reply);
    }
  });
  function send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++sequence;
      const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 9000);
      pending.set(id, reply => {
        clearTimeout(timer);
        if (reply.error) reject(new Error(JSON.stringify(reply.error)));
        else resolve(reply.result);
      });
      socket.send(JSON.stringify({ id, method, params }));
    });
  }
  async function evaluate(expression) {
    const response = await send('Runtime.evaluate', { expression, returnByValue:true });
    if (response.exceptionDetails) throw new Error(JSON.stringify(response.exceptionDetails));
    return response.result.value;
  }
  return { send, evaluate, close: () => socket.close() };
}

async function waitUntil(check, label) {
  for (let attempt = 0; attempt < 38; attempt++) {
    const result = await check();
    if (result) return result;
    await sleep(65);
  }
  throw new Error(`Timed out: ${label}`);
}

function normalized(point, size) {
  return point.map((value, axis) => Math.round(1000 * value / (size[axis] - 1)));
}

async function record(browser, capture, sample, route, output) {
  const {send, evaluate} = browser;
  const navigation = await send('Page.navigate', {url:route});
  if (navigation.errorText) throw new Error(`Navigation error: ${navigation.errorText}`);
  const expectedTitle = `${sample.brand}｜${sample.context}`;
  const state = await waitUntil(() => evaluate(`document.title===${JSON.stringify(expectedTitle)}&&document.body?.dataset.ready==='yes'&&(()=>{const target=document.getElementById(${JSON.stringify(sample.target)});const box=target.getBoundingClientRect();return {bbox:[box.left,box.top,box.right,box.bottom],scale:devicePixelRatio,scroll:target.scrollTop,interactive:[...document.querySelectorAll('button')].map(button=>button.innerText),window:[innerWidth,innerHeight]}})()`),sample.id).catch(async error => {
    throw new Error(`${error.message} ${JSON.stringify(await evaluate('({title:document.title,url:location.href,ready:document.body?.dataset.ready,status:document.readyState,fonts:document.fonts.status})'))}`);
  });
  if (Math.abs(state.scale-scale) > 0.01) throw new Error(`Wrong device scale: ${state.scale}`);
  const [left, top, right, bottom] = state.bbox;
  if (!(left >= 0 && top >= 0 && right <= state.window[0] && bottom <= state.window[1])) throw new Error(`Target clipped: ${sample.id}`);
  const name = `${sample.id}.png`;
  const {data} = await send('Page.captureScreenshot',{format:'png'});
  let {origin,size,mode} = await capture({name,marker:sample.marker});
  if (mode === 'unavailable') {
    const png = Buffer.from(data,'base64');
    size = [png.readUInt32BE(16),png.readUInt32BE(20)];
    if (size.some((value,axis)=>Math.abs(value-state.window[axis]*scale)>2)) throw new Error(`Unexpected viewport dimensions: ${sample.id}`);
    origin = [0,0];
    mode = 'viewport';
    await writeFile(path.join(output,'frames',name),png);
  }
  const pixelBox = state.bbox.map((value,index) => Math.round(value * scale) + origin[index % 2]);
  const bbox = [...normalized(pixelBox.slice(0, 2),size), ...normalized(pixelBox.slice(2),size)];
  const x = (left+right)/2;
  const y = (top+bottom)/2;
  const coordinate = normalized([x * scale + origin[0], y * scale + origin[1]], size);
  let expected = {action:sample.action, coordinate};
  let verified;
  if (sample.action === 'not_found') {
    verified = !state.interactive.includes(sample.absent) && state.interactive.includes(sample.primary);
    expected = {action:'not_found',reason:'The requested control is absent from this test page.'};
  } else if (sample.action === 'scroll') {
    const deltaY = 120 * sample.amount * (sample.direction === 'down' ? 1 : -1);
    await send('Input.dispatchMouseEvent',{type:'mouseWheel',x,y,deltaX:0,deltaY});
    verified = await waitUntil(() => evaluate(`(document.querySelector('#scroll').scrollTop-${state.scroll}) * ${Math.sign(deltaY)} > 0`),sample.id);
    expected = {...expected,direction:sample.direction,amount:sample.amount};
  } else {
    await send('Input.dispatchMouseEvent',{type:'mousePressed',x,y,button:'left',clickCount:1});
    await send('Input.dispatchMouseEvent',{type:'mouseReleased',x,y,button:'left',clickCount:1});
    if (sample.action === 'type') {
      if (!await evaluate(`document.activeElement.id===${JSON.stringify(sample.target)}`)) throw new Error(`Focus mismatch: ${sample.id}`);
      await send('Input.insertText',{text:sample.text});
      verified = await evaluate(`document.activeElement.value===${JSON.stringify(sample.text)}`);
      expected = {...expected,text:sample.text};
    } else {
      verified = await evaluate(`document.body.dataset.clicked==='true'`);
    }
  }
  if (!verified) throw new Error(`Ground-truth action failed: ${sample.id}`);
  const imageHash = createHash('sha256').update(await readFile(path.join(output,'frames',name))).digest('hex');
  return {sample_id:sample.id, split:sample.split, site_domain:sample.group, session_id:`fixture-${sample.group}`,
    source:'isolated_windows_chrome_local_fixture', image_path:`images/${name}`, instruction:sample.instruction,
    ...expected, bbox:sample.action === 'not_found' ? null : bbox, bbox_px:sample.action === 'not_found' ? null : pixelBox,
    image_width:size[0], image_height:size[1], chrome_forced_scale:scale, capture_mode:mode, image_sha256:imageHash,
    tags:['zh-CN',sample.smallTarget ? 'small-control' : sample.action], ground_truth_verified:true};
}

async function collect(output, smoke) {
  const training = buildCases(trainFamilies,'train');
  const evaluation = buildCases(evalFamilies,'eval');
  const cases = smoke ? [training[0],training[2],training[4],training[6], evaluation[0],evaluation[3]] : [...training,...evaluation];
  const byId = new Map(cases.map(sample=>[sample.id,sample]));
  const server = createServer((request,response) => {
    const sample = byId.get(request.url.slice(1));
    response.setHeader('Content-Type','text/html; charset=utf-8');
    response.setHeader('Cache-Control','no-store');
    response.end(sample ? page(sample) : '<!doctype html><title>Chrome Pilot M3</title>');
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const base = `http://127.0.0.1:${server.address().port}/`;
  let browser, child, childClosed;
  try {
    await mkdir(path.join(output,'frames'),{recursive:true});
    child = spawn(python,[path.join(repository,'scripts','m3_capture_fixture_window.py'),'--chrome',chrome,'--url',base,
      '--output',path.join(output,'frames'),'--scale',String(scale)],{stdio:['pipe','pipe','inherit'],windowsHide:true});
    childClosed = new Promise(resolve=>child.once('close',resolve));
    child.stdin.on('error',error=>console.error(error.message));
    const lines = createInterface({input:child.stdout})[Symbol.asyncIterator]();
    async function receive() {
      const next = await lines.next();
      if (next.done) throw new Error('Window capture stopped unexpectedly');
      return JSON.parse(next.value);
    }
    const launch = await receive();
    const debug = `http://127.0.0.1:${launch.port}`;
    const version = await (await fetch(`${debug}/json/version`)).json();
    const targets = await (await fetch(`${debug}/json/list`)).json();
    browser = await connect(targets.find(target=>target.type==='page' && target.url === base).webSocketDebuggerUrl);
    const records = [];
    for (const sample of cases) {
      records.push(await record(browser, async request=>{
        child.stdin.write(`${JSON.stringify(request)}\n`);
        return receive();
      }, sample,base+sample.id,output));
      if (records.length % 10 === 0) console.log(`Verified ${records.length}/${cases.length}`);
    }
    const groups = Object.fromEntries(['train','eval'].map(split=>[split,[...new Set(records.filter(row=>row.split===split).map(row=>row.site_domain))]]));
    if (new Set(records.map(row=>row.image_sha256)).size !== records.length || groups.train.some(group=>groups.eval.includes(group))) {
      throw new Error('Found duplicate frames or a group crossing dataset splits');
    }
    await writeFile(path.join(output,'records.jsonl'),records.map(row=>JSON.stringify(row)).join('\n')+'\n','utf8');
    await writeFile(path.join(output,'capture.json'),JSON.stringify({chrome:version.Browser, started_at:launch.started_at,
      window_dpi:launch.windows_dpi, dpi_awareness:launch.dpi_awareness, chrome_forced_scale:scale,
      capture:'headed Windows Chrome on a dedicated desktop; local controlled pages with automatically verified actions',
      capture_modes:Object.fromEntries(['full_window','viewport'].map(mode=>[mode,records.filter(row=>row.capture_mode===mode).length])),
      groups, counts:Object.fromEntries(['train','eval'].map(split=>[split,Object.fromEntries([...new Set(records.filter(row=>row.split===split).map(row=>row.action))].map(action=>[action,records.filter(row=>row.split===split&&row.action===action).length]))])),
      expected_training_system_prompt:systemPrompt},null,2)+'\n','utf8');
    console.log(`Verified all ${records.length} distinct frames in ${output}`);
  } finally {
    if (browser) browser.close();
    if (child && child.exitCode === null) {
      child.stdin.end('{"stop":true}\n');
      await childClosed;
    }
    server.closeAllConnections();
    server.close();
  }
}

if (!process.argv[2]) throw new Error('Usage: node scripts/m3_collect_chinese_chrome.mjs <output-dir> [--smoke]');
const output = path.resolve(process.argv[2]);
if (existsSync(output)) throw new Error(`Output must be a new directory: ${output}`);
await collect(output, process.argv.includes('--smoke'));
