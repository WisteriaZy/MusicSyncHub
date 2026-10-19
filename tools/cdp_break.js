// 在真实页面上给 app.js:767（playList 里调用 playTrack 那一行）下断点。
//
// ★ 关键点：断点一旦命中，渲染进程会**暂停**，此时任何
//   `Runtime.evaluate` 都不会返回 —— 如果用 await 去等它，
//   整个脚本就死锁（上一版就是这样挂住的）。
//   所以点击必须用"发了就不管"（不 await），命中后手动 resume。
const PORT = process.argv[2] || '9224';
const URL_ = process.argv[3];
const WAIT_MS = Number(process.argv[4] || 15000);
const LINE = Number(process.argv[5] || 767);
const FILTER = process.argv[6] || 'app.js';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function http(p) {
  const res = await fetch(`http://127.0.0.1:${PORT}${p}`);
  return res.json();
}

(async () => {
  let ver = null;
  for (let i = 0; i < 60; i++) {
    try { ver = await http('/json/version'); break; } catch (e) { await sleep(400); }
  }
  if (!ver) { console.log('!! 端口未就绪'); process.exit(1); }
  const targets = await http('/json/list');
  const page = targets.find((t) => t.type === 'page');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const pauses = [];

  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    if (m.method === 'Debugger.paused') pauses.push(m.params);
  });
  await new Promise((r) => ws.addEventListener('open', r));
  const send = (method, params = {}) => new Promise((resolve) => {
    const mid = ++id;
    pending.set(mid, resolve);
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
  // 只发送、不等回复 —— 断点命中时用它，否则会死锁
  const fire = (method, params = {}) => {
    ws.send(JSON.stringify({ id: ++id, method, params }));
  };

  await send('Runtime.enable');
  await send('Debugger.enable');
  await send('Page.enable');
  try {
    await send('Emulation.setDeviceMetricsOverride', {
      width: 1600, height: 950, deviceScaleFactor: 1, mobile: false });
  } catch (e) { /* ignore */ }

  await send('Page.navigate', { url: URL_ });
  await sleep(WAIT_MS);

  const bp = await send('Debugger.setBreakpointByUrl', {
    lineNumber: LINE - 1,
    urlRegex: '.*' + FILTER.replace('.', '\\.') + '.*',
  });
  const locs = (bp.result && bp.result.locations) || [];
  console.log('断点 ' + (bp.result && bp.result.breakpointId)
    + ' 命中位置=' + locs.length
    + (locs[0] ? ' → 第 ' + (locs[0].lineNumber + 1) + ' 行' : ''));

  // 触发：先切到曲库（这一步可以等），再点 ▶（这一步不 await）
  await send('Runtime.evaluate', {
    expression: `(async () => {
      const t = document.querySelector('.tab[data-view="library"]');
      if (t) t.dispatchEvent(new MouseEvent('click',{bubbles:true}));
      for (let i=0;i<40;i++){
        if (document.querySelectorAll('#view-library tbody tr').length) break;
        await new Promise(r=>setTimeout(r,400));
      }
      window.__pb = document.querySelector('#view-library [data-play]');
      return 'ready=' + !!window.__pb;
    })()`,
    awaitPromise: true, returnByValue: true,
  }).then((r) => console.log('准备: ' + JSON.stringify(r.result?.result?.value)));
  await sleep(600);

  console.log('点击 ▶ …（不等待返回值，避免断点暂停造成死锁）');
  fire('Runtime.evaluate', {
    expression: `window.__pb && window.__pb.dispatchEvent(
      new MouseEvent('click',{bubbles:true,cancelable:true}))`,
  });

  // 等断点命中
  for (let i = 0; i < 30 && !pauses.length; i++) await sleep(300);

  if (!pauses.length) {
    console.log('\n★ 断点**没有**命中 —— playList 里的 playTrack 调用根本没执行');
  } else {
    const p0 = pauses[0];
    const f0 = p0.callFrames[0];
    console.log('\n✓ 断点命中！');
    console.log('  函数=' + (f0.functionName || '(匿名)')
      + '  位置=' + f0.url.split('/').pop() + ':' + (f0.location.lineNumber + 1));
    console.log('  调用栈:');
    p0.callFrames.slice(0, 8).forEach((f) => console.log('    '
      + (f.functionName || '(匿名)') + ' @' + f.url.split('/').pop()
      + ':' + (f.location.lineNumber + 1)));
    // 现场变量：cur 是什么
    for (const s of f0.scopeChain) {
      let r;
      try {
        r = await send('Runtime.getProperties', {
          objectId: s.object.objectId, ownProperties: true });
      } catch (e) { continue; }
      const props = r.result?.result || [];
      const vals = props
        .filter((p) => ['cur', 'tracks', 'label', 'startId'].includes(p.name))
        .map((p) => p.name + '=' + String(
          p.value?.description || p.value?.value || '?').slice(0, 70));
      if (vals.length) console.log('    [' + s.type + '] ' + vals.join('  |  '));
      // 深挖 cur：它到底有没有 .track
      const curProp = props.find((p) => p.name === 'cur');
      if (curProp && curProp.value && curProp.value.objectId) {
        const r2 = await send('Runtime.getProperties', {
          objectId: curProp.value.objectId, ownProperties: true });
        const inner = (r2.result?.result || []).map((q) =>
          q.name + '=' + String(q.value?.description || q.value?.value || 'undefined').slice(0, 60));
        console.log('      cur 的属性: ' + inner.join('  |  '));
        const trk = (r2.result?.result || []).find((q) => q.name === 'track');
        if (trk && trk.value && trk.value.objectId) {
          const r3 = await send('Runtime.getProperties', {
            objectId: trk.value.objectId, ownProperties: true });
          const tv = (r3.result?.result || []).slice(0, 8).map((q) =>
            q.name + '=' + String(q.value?.description || q.value?.value || 'undefined').slice(0, 40));
          console.log('      cur.track 的属性: ' + tv.join('  |  '));
        } else {
          console.log('      ★ cur.track 不是对象: '
            + JSON.stringify(trk && (trk.value?.description || trk.value?.value)));
        }
      }
    }
    await send('Debugger.resume');
    await sleep(1500);
    const after = await send('Runtime.evaluate', {
      expression: `JSON.stringify({
        title: document.getElementById('np-title').textContent.trim(),
        src: document.getElementById('audio').src,
        paused: document.getElementById('audio').paused,
      })`, returnByValue: true });
    console.log('\n  继续执行后: ' + (after.result?.result?.value || '?'));
  }

  const errs = [];
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.method === 'Runtime.exceptionThrown') {
      errs.push(m.params.exceptionDetails?.exception?.description || '');
    }
  });
  await sleep(800);
  if (errs.length) {
    console.log('\n异常:');
    errs.slice(0, 5).forEach((e) => console.log('  ' + e.split('\n')[0]));
  }
  ws.close();
  process.exit(0);
})().catch((e) => { console.log('!! ' + e.message); process.exit(1); });
