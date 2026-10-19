// 在**真实应用页面**里点开「文件」标签，测量目录树布局。
// 这比自建夹具可靠：夹具少了骨架元素很容易测出假问题。
const PORT = process.argv[2] || '9223';
const URL_ = process.argv[3] || 'http://127.0.0.1:8787/';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function http(path) {
  const res = await fetch(`http://127.0.0.1:${PORT}${path}`);
  return res.json();
}

(async () => {
  let ver = null;
  for (let i = 0; i < 60; i++) {
    try { ver = await http('/json/version'); break; } catch (e) { await sleep(400); }
  }
  if (!ver) { console.log('!! DevTools 未就绪'); process.exit(1); }
  const page = (await http('/json/list')).find((t) => t.type === 'page');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const errors = [];
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails || {};
      errors.push(d.exception?.description || d.text || '');
    }
  });
  await new Promise((r) => ws.addEventListener('open', r));
  const send = (method, params = {}) => new Promise((resolve) => {
    const mid = ++id;
    pending.set(mid, resolve);
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
  const evaluate = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    return r.result?.result?.value;
  };

  await send('Runtime.enable');
  await send('Page.enable');
  // 用接近用户窗口的尺寸，别拿 320px 高的小窗去量布局
  try {
    const t = (await http('/json/list')).find((x) => x.type === 'page');
    await send('Emulation.setDeviceMetricsOverride', {
      width: 1560, height: 900, deviceScaleFactor: 1, mobile: false,
    });
  } catch (e) { /* 低版本不支持就算了 */ }
  await send('Page.navigate', { url: URL_ });
  await sleep(9000);

  // 切到「文件」视图
  await evaluate(`(() => {
    document.querySelectorAll('.tab').forEach((t) => {
      if (t.dataset.view === 'files') t.click();
    });
    return true;
  })()`);
  await sleep(6000);

  const out = await evaluate(`(() => {
    const H = (sel) => {
      const e = document.querySelector(sel);
      return e ? Math.round(e.getBoundingClientRect().height) : null;
    };
    const tree = document.querySelector('#fb-tree');
    const wrap = document.querySelector('.fb-tree-wrap');
    const cbs = tree ? tree.querySelectorAll('input.fb-pick') : [];
    const cs = cbs.length ? getComputedStyle(cbs[0]) : null;
    return JSON.stringify({
      heights: {
        app: H('#app'), body: H('#app > .body'), main: H('#main'),
        view: H('#view-files'), split: H('.fb-split'),
        wrap: H('.fb-tree-wrap'), tree: H('#fb-tree'),
        list: H('#fb-list'), footer: H('#player'),
      },
      treeScroll: tree ? { sh: tree.scrollHeight, ch: tree.clientHeight } : null,
      wrapOverflow: wrap ? getComputedStyle(wrap).overflow : null,
      views: Array.from(document.querySelectorAll('.view')).map(
        (v) => v.id + ':' + (v.classList.contains('hidden') ? 'hidden' : 'shown')),
      checkbox: {
        count: cbs.length,
        visible: cs ? (cs.display !== 'none' && +cs.opacity > 0.5) : null,
        width: cs ? cs.width : null,
        type: cbs.length ? cbs[0].type : null,
      },
      nodeCount: tree ? tree.querySelectorAll('.fb-node').length : 0,
      barHidden: (() => { const b = document.querySelector('#fb-tree-bar');
        return b ? b.classList.contains('hidden') : null; })(),
    });
  })()`);
  console.log('=== 真实页面测量 ===');
  console.log(out);
  if (errors.length) { console.log('\n--- 页面异常 ---'); errors.slice(0, 8).forEach((e) => console.log('  ' + e.slice(0, 200))); }
  ws.close();
  process.exit(0);
})().catch((e) => { console.log('!! ' + e.message); process.exit(1); });
