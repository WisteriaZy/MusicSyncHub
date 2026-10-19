/* ============================================================
   任务占用同步（多页面 + 刷新恢复）

   解决两件事：

   1. **多开页面**：用户可以同时开好几个界面。后端的独占锁保证了
      "一次只有一个流程在跑"，但前端得**知道**隔壁在忙 ——
      否则按钮还是亮的，点了才被 409 顶回来，体验很差。
      这里用 BroadcastChannel（不支持就退回 localStorage 事件）
      在页面之间广播，并定期拉 `/api/busy` 兜底。

   2. **刷新页面不中断任务**：任务本身跑在**后端线程**里，刷新页面
      不会杀掉它（这正是"后台处理"的意义）。但前端得能在刷新后
      **重新接上**：把正在跑的 job id 存起来，启动时读回来继续显示进度，
      而不是以为任务没了。

   任务结束后必须 `ack`：这是"一次只允许一个流程"的收尾点 ——
   不堆积任务，因为堆积的任务会基于**已经变化**的文件状态执行。
   ============================================================ */
const TaskSync = (() => {
  const KEY = 'msh.task.active';       // localStorage：当前任务（跨刷新）
  const CH = 'msh-task';               // BroadcastChannel 名字
  // 每个页面实例一个 id，用来区分"我发起的" vs "隔壁发起的"
  const ME = 'p' + Math.random().toString(36).slice(2, 10);

  let ch = null;
  let timer = null;
  let cur = null;                      // { id, kind, owner, started }
  let busy = null;                     // 后端 /api/busy 的 job 快照
  let bg = null;                       // 非独占长任务（对比/查重）快照
  const listeners = [];

  try {
    if (typeof BroadcastChannel !== 'undefined') {
      ch = new BroadcastChannel(CH);
      ch.onmessage = (e) => onMsg(e.data);
    }
  } catch (e) { ch = null; }

  // 不支持 BroadcastChannel 就退回 storage 事件（同源其他标签页能收到）
  window.addEventListener('storage', (e) => {
    if (!ch && e.key === KEY && e.newValue) {
      try { onMsg(JSON.parse(e.newValue)); } catch (err) { /* ignore */ }
    }
  });

  function onMsg(m) {
    if (!m || m.from === ME) return;
    if (m.type === 'start' || m.type === 'update') {
      busy = m.job || busy;
      fire();
    } else if (m.type === 'done') {
      busy = null;
      fire();
    }
  }

  function broadcast(type, job) {
    const m = { type, from: ME, job: job || null, at: Date.now() };
    try { if (ch) ch.postMessage(m); } catch (e) { /* ignore */ }
    try { localStorage.setItem(KEY, JSON.stringify(m)); } catch (e) { /* ignore */ }
  }

  function fire() { listeners.forEach((f) => { try { f(snapshot()); } catch (e) {} }); }

  function snapshot() {
    return {
      me: ME,
      busy: !!activeJob(),
      job: activeJob(),
      // 是不是**我这个页面**发起的
      mine: !!(activeJob() && activeJob().owner === ME),
      // 有任务在跑，但不是这个页面发起的 → 界面要禁用冲突操作
      others: !!(activeJob() && activeJob().owner !== ME),
    };
  }

  function activeJob() {
    if (cur && cur.state === 'running') return cur;
    if (busy && busy.state === 'running') return busy;
    return null;
  }

  /** 订阅占用变化。 */
  function onChange(fn) { listeners.push(fn); return () => {
    const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1);
  }; }

  /** 我这边要开始一个任务：先问后端有没有别人在跑。 */
  async function begin(kind) {
    let b = null;
    try { b = await API.busy(); } catch (e) { b = null; }
    if (b && b.busy) {
      const j = b.job || {};
      const who = j.owner && j.owner !== ME ? '另一个页面' : '当前会话';
      const err = new Error(
        `${who}正在执行「${kindName(j.kind)}」（${j.done}/${j.total || '?'}），`
        + `请等它结束后再操作。同时执行多个流程会基于过期的文件状态操作。`);
      err.code = 409;
      err.job = j;
      throw err;
    }
    cur = { id: '', kind, owner: ME, state: 'running', started: Date.now() };
    broadcast('start', cur);
    fire();
    return ME;
  }

  /** 任务真的起来了（拿到后端 job id）。 */
  function attach(job) {
    if (!job) return;
    cur = { ...(job || {}), owner: ME };
    try { localStorage.setItem(KEY + '.id', job.id || ''); } catch (e) {}
    broadcast('update', cur);
    fire();
  }

  /** 更新进度（轮询回来的）。 */
  function update(job) {
    if (!job) return;
    cur = { ...job, owner: (job.owner || ME) };
    if (job.state !== 'running') {
      busy = null;
      broadcast('done', job);
    } else {
      broadcast('update', cur);
    }
    fire();
  }

  /** 任务收尾：ack 后清掉本地状态，允许开始下一个流程。 */
  async function finish(job) {
    const jid = (job && job.id) || (cur && cur.id);
    cur = null;
    busy = null;
    try { localStorage.removeItem(KEY + '.id'); } catch (e) {}
    if (jid) { try { await API.jobAck(jid); } catch (e) { /* 尽力而为 */ } }
    broadcast('done', job || null);
    fire();
  }

  /** 刷新页面后重新接上还在跑的任务。 */
  async function resume() {
    let jid = '';
    try { jid = localStorage.getItem(KEY + '.id') || ''; } catch (e) {}
    // 先看后端到底有没有在跑（以它为准，localStorage 可能过期）
    let b = null;
    try { b = await API.busy(); } catch (e) { b = null; }
    if (b && b.busy && b.job) {
      cur = b.job;
      fire();
      return b.job;
    }
    if (jid) {
      try {
        const j = await API.job(jid);
        if (j && j.state === 'running') { cur = j; fire(); return j; }
      } catch (e) { /* 任务没了就算了 */ }
      try { localStorage.removeItem(KEY + '.id'); } catch (e) {}
    }
    return null;
  }

  /** 定期对齐后端状态（BroadcastChannel 丢消息时的兜底）。 */
  function startPolling(ms = 3000) {
    if (timer) clearInterval(timer);
    timer = setInterval(async () => {
      try {
        const b = await API.busy();
        const j = b && b.job ? b.job : null;
        const prev = busy;
        busy = j;
        // 隔壁开始了 / 结束了，都要让界面跟着变
        if ((!!prev) !== (!!j)) fire();
      } catch (e) { /* 网络抖动忽略 */ }

      // ★ 顺便看一眼**非独占**的长任务（对比、查重）。
      //
      //   这两类是有意不阻塞写操作的（`exclusive=False`），所以
      //   `/api/busy` 对它们恒 `busy:false` —— 结果是：对比跑了 40 秒，
      //   顶栏一片安静，用户以为卡死了，或者以为没点成功，又点一次。
      //
      //   不该为了显示而把它们变成独占（那会平白挡住写操作），
      //   所以这里单独走 `/api/jobs` 把"有长任务在跑"找出来给界面用。
      //   注意它**不参与 `begin()` 的占用判断**，只是显示。
      try {
        const js = await API.jobs();
        const arr = Array.isArray(js) ? js : ((js && js.jobs) || []);
        const running = arr.filter((x) => x && x.state === 'running'
          && x.kind !== 'scan') || [];
        const prevB = bg;
        // 只挑"值得提示"的：只读长任务
        bg = running.find((x) => (x.kind === 'compare' || x.kind === 'duplicates'
          || x.kind === 'hash')) || null;
        if ((!!prevB) !== (!!bg)) fire();
      } catch (e) { /* 忽略 */ }
    }, ms);
  }

  function kindName(k) {
    return { scan: '扫描', sync: '文件操作', hash: '校验',
             'upload-dedup': '上传查重', move: '移动', organize: '智能整理',
             trash: '清理', station: '中转站',
             // 这两个是只读长任务，顶栏"正在执行…"会用到
             compare: '双端对比', duplicates: '跨库查重',
             transfer: '跨库传输' }[k] || k || '任务';
  }

  return { ME, begin, attach, update, finish, resume, onChange, snapshot,
           activeJob, startPolling, kindName, get busy() { return activeJob(); },
           /** 正在跑的**非独占**长任务（对比/查重/校验），仅用于界面提示。
            *  它不参与 `begin()` 的占用判断 —— 只读任务本来就不该挡写操作。 */
           get backgroundJob() { return bg; } };
})();
