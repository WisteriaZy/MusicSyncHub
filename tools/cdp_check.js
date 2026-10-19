// 通过 CDP 驱动 Edge 读取页面自检日志（放在项目 tools/ 下长期可用）。
//   node tools/cdp_check.js <port> <url> <waitMs>
// 比 --screenshot 可靠：能等页面真正跑完，也能把 console 异常带出来。
const PORT = process.argv[2] || '9223';
const URL_ = process.argv[3];
const WAIT_MS = Number(process.argv[4] || 90000);

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
  if (!ver) { console.log('!! DevTools 端口未就绪'); process.exit(1); }

  const targets = await http('/json/list');
  const page = targets.find((t) => t.type === 'page');
  if (!page) { console.log('!! 没有可用页面'); process.exit(1); }

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const consoleMsgs = [];

  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails || {};
      consoleMsgs.push('EXC: ' + (d.exception?.description || d.text || ''));
    }
  });
  await new Promise((r) => ws.addEventListener('open', r));
  const send = (method, params = {}) => new Promise((resolve) => {
    const mid = ++id;
    pending.set(mid, resolve);
    ws.send(JSON.stringify({ id: mid, method, params }));
  });

  await send('Runtime.enable');
  await send('Page.enable');
  // 固定成桌面窗口：headless 的默认视口很小（可能只有 500x320），
  // 会让媒体查询落到"窄屏"分支 —— 布局测试测出来的就不是宽屏行为了。
  try {
    await send('Emulation.setDeviceMetricsOverride', {
      width: 1600, height: 950, deviceScaleFactor: 1, mobile: false,
    });
  } catch (e) { /* 低版本不支持就按实际视口来 */ }
  await send('Page.navigate', { url: URL_ });
  await sleep(WAIT_MS);

  const r = await send('Runtime.evaluate', {
    expression: `(() => {
      const pre = document.getElementById('SELFTEST') || document.querySelector('pre');
      return JSON.stringify({ title: document.title, log: pre ? pre.textContent : '(没有日志区)' });
    })()`,
    returnByValue: true,
  });
  const value = r.result?.result?.value;
  if (value) {
    const o = JSON.parse(value);
    console.log(o.log);
  } else {
    console.log('!! 取值失败', JSON.stringify(r).slice(0, 300));
  }
  if (consoleMsgs.length) {
    console.log('\n--- console 异常 ---');
    consoleMsgs.slice(0, 10).forEach((m) => console.log('  ' + m));
  }
  ws.close();
  process.exit(0);
})().catch((e) => { console.log('!! ' + e.message); process.exit(1); });
