// 在真实页面上按指定尺寸跑一段 JS，并（可选）存截图。
//   node tools/cdp_vp.js <port> <url> <waitMs> <js文件> <宽度列表> [截图目录] [高度列表]
//
// 宽度和高度都是逗号分隔的列表，**按位置配对**；只给一个高度就全部用它。
//   cdp_vp.js 9224 http://x/ 20000 p.js 1280,900 shots       → 高度默认 900
//   cdp_vp.js 9224 http://x/ 20000 p.js 1280,900 shots 500   → 都是高 500
//   cdp_vp.js 9224 http://x/ 20000 p.js 1440,1440 shots 900,420
//
// 为什么需要它：`window.resizeTo` 对普通标签页无效，要真的改视口
// 必须走 CDP 的 `Emulation.setDeviceMetricsOverride`。
// 布局问题（尤其是"窗口又矮又宽"或"小屏幕"）只有**真的把视口改掉**才测得出来 ——
// 在 1600x900 的窗口里量 `innerWidth` 永远得到 1600，看起来"没问题"。
const fs = require('fs');
const path = require('path');
const PORT = process.argv[2] || '9224';
const URL_ = process.argv[3];
const WAIT_MS = Number(process.argv[4] || 12000);
const JS_FILE = process.argv[5];
const WIDTHS = String(process.argv[6] || '1280').split(',').map((x) => Number(x.trim()));
const SHOT_DIR = process.argv[7] || '';
const HEIGHTS = String(process.argv[8] || '900').split(',').map((x) => Number(x.trim()));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function http(p) {
  const res = await fetch(`http://127.0.0.1:${PORT}${p}`);
  return res.json();
}

(async () => {
  if (!URL_ || !JS_FILE) {
    console.log('用法: node cdp_vp.js <port> <url> <waitMs> <js文件> <宽度,宽度> [截图目录]');
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
      errors.push((d.exception && d.exception.description) || d.text || '');
    }
  });
  await new Promise((r) => ws.addEventListener('open', r));
  const send = (method, params) => new Promise((res) => {
    const mid = ++id;
    pending.set(mid, res);
    ws.send(JSON.stringify({ id: mid, method, params: params || {} }));
  });

  await send('Runtime.enable');
  await send('Page.enable');
  await send('Page.navigate', { url: URL_ });
  await sleep(WAIT_MS);

  if (SHOT_DIR) fs.mkdirSync(SHOT_DIR, { recursive: true });

  for (let i = 0; i < WIDTHS.length; i++) {
    const w = WIDTHS[i];
    // 高度按位置配对；只给一个高度就所有宽度都用它
    const h = HEIGHTS.length === 1 ? HEIGHTS[0] : (HEIGHTS[i] || HEIGHTS[0] || 900);
    // 顺序很重要：**先导航、再设尺寸、最后才跑探针**。
    //   · 只设尺寸不导航：页面仍按旧视口布局，`innerHeight` 不变
    //     （实测设了 height=420 结果还是 900）；
    //   · 导航**会清掉**尺寸覆盖，所以设完再导航也白设。
    //   两个坑叠在一起，最容易得出"没问题"的错误结论。
    await send('Page.navigate', { url: URL_ });
    await sleep(WAIT_MS);
    await send('Emulation.setDeviceMetricsOverride', {
      width: w, height: h, deviceScaleFactor: 1, mobile: false,
    });
    // 让浏览器按新尺寸重排一次
    await sleep(1500);
    const r = await send('Runtime.evaluate', {
      expression: code, awaitPromise: true, returnByValue: true,
    });
    const val = r.result && r.result.result ? r.result.result.value : null;
    console.log(`\n########## viewport=${w}x${h} ##########`);
    console.log(val != null ? val : '(无返回值)');
    if (r.result && r.result.exceptionDetails) {
      console.log('!! JS 异常: ' + JSON.stringify(r.result.exceptionDetails).slice(0, 300));
    }
    if (SHOT_DIR) {
      const shot = await send('Page.captureScreenshot', { format: 'png' });
      const d = shot.result && shot.result.data;
      if (d) {
        const f = path.join(SHOT_DIR, `vp-${w}x${h}.png`);
        fs.writeFileSync(f, Buffer.from(d, 'base64'));
        console.log(`(截图: ${f})`);
      }
    }
  }
  await send('Emulation.clearDeviceMetricsOverride');
  if (errors.length) console.log('\n运行期异常: ' + errors.slice(0, 3).join(' | '));
  ws.close();
})().catch((e) => { console.log('!! ' + e.message); process.exit(1); });
