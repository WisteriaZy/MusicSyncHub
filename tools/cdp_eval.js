// 在**真实页面**上执行一段 JS 并把结果带回来（含 console/异常）。
//   node tools/cdp_eval.js <port> <url> <waitMs> <file-with-js>
// 比自建 HTML 探针可靠：测的就是用户真正打开的那个页面，
// 不会因为"探针页少了某个元素/脚本"而得出假结论。
const fs = require('fs');
const PORT = process.argv[2] || '9223';
const URL_ = process.argv[3];
const WAIT_MS = Number(process.argv[4] || 12000);
const JS_FILE = process.argv[5];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function http(path) {
  const res = await fetch(`http://127.0.0.1:${PORT}${path}`);
  return res.json();
}

(async () => {
  if (!URL_ || !JS_FILE) {
    console.log('用法: node cdp_eval.js <port> <url> <waitMs> <js文件>');
    process.exit(1);
  }
  const code = fs.readFileSync(JS_FILE, 'utf8');
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
  const errors = [];

  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails || {};
      errors.push(d.exception?.description || d.text || '');
    }
    if (m.method === 'Runtime.consoleAPICalled'
        && (m.params.type === 'error' || m.params.type === 'warning')) {
      const args = (m.params.args || []).map((a) => a.value ?? a.description ?? '');
      errors.push(m.params.type + ': ' + args.join(' ').slice(0, 200));
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
  try {
    await send('Emulation.setDeviceMetricsOverride', {
      width: 1600, height: 950, deviceScaleFactor: 1, mobile: false,
    });
  } catch (e) { /* ignore */ }

  await send('Page.navigate', { url: URL_ });
  await sleep(WAIT_MS);

  const r = await send('Runtime.evaluate', {
    expression: code,
    awaitPromise: true,
    returnByValue: true,
  });
  const res = r.result || {};
  if (res.exceptionDetails) {
    console.log('!! 脚本抛异常: '
      + (res.exceptionDetails.exception?.description
         || res.exceptionDetails.text || ''));
  }
  const v = res.result?.value;
  console.log(typeof v === 'string' ? v : JSON.stringify(v, null, 2));

  if (errors.length) {
    console.log('\n--- console/异常 ---');
    errors.slice(0, 12).forEach((m) => console.log('  ' + String(m).slice(0, 300)));
  }
  ws.close();
  process.exit(0);
})().catch((e) => { console.log('!! ' + e.message); process.exit(1); });
