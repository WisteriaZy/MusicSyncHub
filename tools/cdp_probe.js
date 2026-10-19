// 逐个试改动，找出到底是哪一条 CSS 让目录树塌掉。
const PORT = process.argv[2] || '9223';
const URL_ = process.argv[3] || 'http://127.0.0.1:8787/';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function http(p) { return (await fetch(`http://127.0.0.1:${PORT}${p}`)).json(); }

(async () => {
  let ver = null;
  for (let i = 0; i < 60; i++) {
    try { ver = await http('/json/version'); break; } catch (e) { await sleep(400); }
  }
  const page = (await http('/json/list')).find((t) => t.type === 'page');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  });
  await new Promise((r) => ws.addEventListener('open', r));
  const send = (method, params = {}) => new Promise((resolve) => {
    const mid = ++id; pending.set(mid, resolve);
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
  const ev = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    return r.result?.result?.value;
  };

  await send('Runtime.enable');
  await send('Page.enable');
  await send('Page.navigate', { url: URL_ });
  await sleep(9000);
  await ev(`document.querySelectorAll('.tab').forEach(t=>{if(t.dataset.view==='files')t.click()});true`);
  await sleep(6000);

  const measure = () => ev(`(() => {
    const H=(s)=>{const e=document.querySelector(s);return e?Math.round(e.getBoundingClientRect().height):null;};
    return JSON.stringify({split:H('.fb-split'),wrap:H('.fb-tree-wrap'),tree:H('#fb-tree'),list:H('#fb-list'),view:H('#view-files')});
  })()`);

  console.log('基线        :', await measure());

  // 试验 1：给 .fb-split 明确的高度百分比
  await ev(`(() => {
    const s=document.createElement('style'); s.id='t1';
    s.textContent='.fb-split{height:100%}'; document.head.appendChild(s); return true;
  })()`);
  await sleep(1200);
  console.log('t1 split h100%:', await measure());
  await ev(`document.getElementById('t1').remove(); true`);
  await sleep(800);

  // 试验 2：.view 去掉 padding（看是不是 padding 挤压）
  await ev(`(() => {
    const s=document.createElement('style'); s.id='t2';
    s.textContent='#view-files{padding:0}'; document.head.appendChild(s); return true;
  })()`);
  await sleep(1200);
  console.log('t2 view pad0 :', await measure());
  await ev(`document.getElementById('t2').remove(); true`);
  await sleep(800);

  // 试验 3：给 .fb-main 加 min-height:0 再给 .fb-split 一个 min-height
  await ev(`(() => {
    const s=document.createElement('style'); s.id='t3';
    s.textContent='.fb-split{min-height:60vh}'; document.head.appendChild(s); return true;
  })()`);
  await sleep(1200);
  console.log('t3 split 60vh:', await measure());
  await ev(`document.getElementById('t3').remove(); true`);
  await sleep(800);

  // 试验 4：把 .view 的 overflow 恢复成 auto（对照，确认我改的那条有没有生效）
  await ev(`(() => {
    const s=document.createElement('style'); s.id='t4';
    s.textContent='#view-files{overflow:auto}'; document.head.appendChild(s); return true;
  })()`);
  await sleep(1200);
  console.log('t4 view auto :', await measure());
  await ev(`document.getElementById('t4').remove(); true`);
  await sleep(800);

  ws.close();
  process.exit(0);
})().catch((e) => { console.log('!! ' + e.message); process.exit(1); });
