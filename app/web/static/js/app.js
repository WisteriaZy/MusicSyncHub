/* ============================================================
   应用主控：状态、视图切换、侧栏、播放器
   ============================================================ */
const State = {
  view: 'sources',
  sources: [],
  system: null,
  activeSource: null,        // 侧栏选中的数据源 id
  tracks: [],                // 当前曲库视图的曲目
  libFilters: {},            // 曲库过滤条件
  treePrefix: '',            // 侧栏目录树当前选中的目录（与文件浏览/曲库浏览共用）
  playlist: [],              // 播放列表（当前表格的 track id 顺序）
  curTrackId: null,
  cmp: null,                 // 最近一次对比结果
  plan: null,                // 最近一次同步计划
  dup: null,                 // 最近一次查重结果
  opLog: [],                 // 操作记录
  job: null,
};

const App = (() => {
  // ------------------------------------------------------------ 视图注册
  const views = {};   // name -> {mount(el), render()}
  function registerView(name, v) { views[name] = v; }

  /** 取某个视图公开出来的方法（用于跨视图跳转，比如"曲库浏览 → 文件浏览定位"）。 */
  function viewApi(name) { return views[name] || null; }

  function setView(name) {
    State.view = name;
    // 记住上次停在哪个页面（刷新后回到原处，不用重新点一遍）
    try { if (typeof Prefs !== 'undefined') Prefs.rememberView(name); } catch (e) {}
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.view === name));
    document.querySelectorAll('.view').forEach((el) => el.classList.toggle('hidden', el.id !== 'view-' + name));
    const el = document.getElementById('view-' + name);
    if (views[name]) {
      if (!el.dataset.mounted) { views[name].mount(el); el.dataset.mounted = '1'; }
      views[name].render(el);
      // 中转站这类"每次进来都要看最新状态"的视图，render 之后自己拉一次数据
      if (typeof views[name].load === 'function' && name !== 'files') {
        try { views[name].load(); } catch (e) { /* 让视图自己提示错误 */ }
      }
    }
    // 侧栏折叠状态下的兜底导航也要跟着视图走。
    // ★ 必须在这里调：视图是**懒渲染**的（首次切过去才 render），
    //   而 applyNav 只在折叠时跑一次 —— 那时视图里还没有锚点元素，
    //   兜底条就永远插不进去（汉堡键"联动性差"的真正原因）。
    syncNavFallbacks(app.classList.contains('nav-hidden'));
    // 侧栏目录树只服务"曲库浏览"；文件浏览有自己更完整的目录树，
    // "文件浏览"里再显示一个会互相干扰（点了不会联动，反而让人以为坏了）。
    const treeBlock = document.getElementById('side-tree-block');
    if (treeBlock) {
      treeBlock.classList.toggle('hidden', name !== 'library');
    }
  }

  // ------------------------------------------------------------ 数据源
  async function loadSources() {
    State.sources = await API.sources();
    // 恢复上次选中的数据源（还在的话）。没有/失效了就用第一个。
    try {
      const saved = (typeof Prefs !== 'undefined') ? Prefs.get('activeSource', '') : '';
      if (saved && State.sources.some((s) => s.id === saved)) State.activeSource = saved;
      else if (!State.activeSource || !State.sources.some((s) => s.id === State.activeSource)) {
        State.activeSource = State.sources.length ? State.sources[0].id : '';
      }
    } catch (e) {
      if (!State.activeSource && State.sources.length) State.activeSource = State.sources[0].id;
    }
    renderSidebar();
    refreshSourceStatus();      // 顺带探活，侧栏的在线状态就靠它
    return State.sources;
  }

  function sourceById(id) { return State.sources.find((s) => s.id === id) || null; }

  /** 在线状态：{id: {ok, error, latency}}。由 refreshSourceStatus() 填充。 */
  function statusOf(id) {
    return (State.srcStatus && State.srcStatus[id]) || null;
  }

  function dotHtml(id, big) {
    const st = statusOf(id);
    const src = sourceById(id);
    const cls = big ? 'dot big' : 'dot';
    // 还没测到 → 明确画成"检测中"，不要用一个看起来像"离线"的灰点糊弄
    if (!st) {
      return `<span class="${cls} unknown" title="正在检测可用性…"></span>`;
    }
    if (st.ok) {
      return `<span class="${cls} ok" title="在线 · 可读取（${st.latency != null ? Math.round(st.latency * 1000) + ' ms' : ''}）"></span>`;
    }
    const why = st.error || '无法访问';
    return `<span class="${cls} err" title="离线 · ${U.esc(why)}"></span>`;
  }

  function statusText(id) {
    const st = statusOf(id);
    if (!st) return '检测中…';
    if (st.ok) return '在线';
    return '离线';
  }

  /** 探活所有数据源（只 stat 根目录，很便宜）。 */
  let _statusSeq = 0;
  async function refreshSourceStatus() {
    const mine = ++_statusSeq;
    try {
      const list = await API.sourcesStatus();
      if (mine !== _statusSeq) return;          // 有更新的请求了，丢弃旧结果
      const map = {};
      list.forEach((x) => { map[x.id] = x; });
      State.srcStatus = map;
      State.srcStatusAt = Date.now();
      renderSidebar();
      // 数据源页的磁贴也要跟着更新状态徽标
      if (State.view === 'sources' && views.sources && views.sources.refreshStatus) {
        views.sources.refreshStatus();
      }
    } catch (e) { /* 探活失败不影响主流程，保持"检测中" */ }
  }

  function renderSidebar() {
    const box = document.getElementById('side-sources');
    document.getElementById('src-count').textContent = State.sources.length;
    if (!State.sources.length) {
      box.innerHTML = `<div class="muted pad">还没有数据源。<br>点下面按钮添加本机或挂载盘上的音乐目录。</div>`;
      return;
    }
    box.innerHTML = '';
    State.sources.forEach((s) => {
      const el = document.createElement('div');
      el.className = 'src-item' + (State.activeSource === s.id ? ' active' : '');
      const st = statusOf(s.id);
      el.innerHTML = `
        <svg class="ico" style="color:${st && !st.ok ? 'var(--danger)' : 'var(--accent-link)'}">
          <use href="#i-${U.typeIcon(s.type)}"></use></svg>
        <div class="nm-wrap">
          <div class="nm">${U.esc(s.name)}</div>
          <div class="sub">
            <span class="st st-${st ? (st.ok ? 'ok' : 'err') : 'unknown'}">${statusText(s.id)}</span>
            · ${s.indexed || 0} 首${s.read_only ? ' · 只读' : ''}
          </div>
        </div>
        ${dotHtml(s.id)}
        <button class="src-retry" data-retry="${U.esc(s.id)}" title="重新检测可用性">${U.icon('refresh')}</button>`;
      el.onclick = () => selectSource(s.id);
      const retry = el.querySelector('[data-retry]');
      retry.onclick = (e) => {
        e.stopPropagation();
        delete (State.srcStatus || {})[s.id];
        renderSidebar();
        refreshSourceStatus();
      };
      box.appendChild(el);
    });
    // 鼠标悬停在离线项上时把原因显示出来，不用去猜
    box.querySelectorAll('.src-item').forEach((n) => {
      const id = n.querySelector('[data-retry]').dataset.retry;
      const st = statusOf(id);
      if (st && !st.ok && st.error) n.title = `${sourceById(id).name}：${st.error}`;
    });
  }

  async function selectSource(id, { keepView = false } = {}) {
    State.activeSource = id;
    // ★ 切源必须清掉"上一个源的目录前缀"，否则新源被旧源的绝对路径过滤成 0 首，
    //   文件浏览还会拿着旧路径去新源请求目录（越界）。
    State.treePrefix = '';
    if (State.libFilters) State.libFilters.prefix = '';
    // ★ 换源必须丢掉上一个源的查重结果：否则切到 B 库后打开查重页，
    //   看到的还是 A 库的分组；点「清理」就是对 A 库的文件动手。
    State.dup = null;
    // 记住选中的数据源：下次打开就在同一个库上，不用重新点
    try { if (typeof Prefs !== 'undefined') Prefs.set('activeSource', id); } catch (e) {}
    renderSidebar();
    const s = sourceById(id);
    // 只有从"数据源"页点进来才顺带跳到曲库浏览；已经在按源看东西的页面
    // （曲库浏览 / 文件浏览 / 双栏对比）就别把人踢走，原地按新源刷新即可。
    const stays = ['library', 'compare', 'files'];
    if (!keepView && !stays.includes(State.view)) setView('library');
    if (State.view === 'files' && views.files) {
      // 文件浏览有自己的目录树与列表，换源后要重新加载，否则还是旧源的内容
      views.files.reload && views.files.reload();
      renderSidebar();
      return;
    }
    await renderTree();
    if (State.view === 'library' && views.library) views.library.reload();
  }

  async function renderTree() {
    const box = document.getElementById('side-tree');
    if (!State.activeSource) { box.innerHTML = '<div class="muted pad">选择一个数据源后显示</div>'; return; }
    box.innerHTML = '<div class="muted pad">加载目录…</div>';
    let data;
    try { data = await API.folders(State.activeSource); } catch (e) { box.innerHTML = `<div class="muted pad">${U.esc(e.message)}</div>`; return; }
    if (!data.folders.length) { box.innerHTML = '<div class="muted pad">尚未扫描，先点「扫描」</div>'; return; }
    const rows = [{ path: '', rel: '(全部)', count: data.folders.reduce((a, f) => a + f.count, 0), depth: 0 }];
    const sorted = data.folders.slice().sort((a, b) => a.rel.localeCompare(b.rel, 'zh'));
    box.innerHTML = '';
    rows.concat(sorted).forEach((f) => {
      const el = document.createElement('div');
      el.className = 'node';
      el.dataset.path = f.path || '';
      el.style.paddingLeft = (6 + (f.depth || 0) * 12) + 'px';
      el.innerHTML = `<span class="n">${U.esc(f.rel || '(全部)')}</span><span class="c">${f.count}</span>`;
      el.onclick = () => treeSelect(f.path || '');
      box.appendChild(el);
    });
    // 每次重建都要把"当前选中"恢复回来 —— 否则点完刷新/切数据源之后
    // 高亮就没了，看起来像"目录和曲库没联动"。
    highlightTreeNode();
  }

  /** 把侧栏树里的某个节点标成选中（不触发加载）。 */
  function highlightTreeNode() {
    const box = document.getElementById('side-tree');
    if (!box) return;
    const want = State.treePrefix || '';
    let hit = null;
    box.querySelectorAll('.node').forEach((n) => {
      const on = (n.dataset.path || '') === want;
      n.classList.toggle('active', on);
      if (on) hit = n;
    });
    if (hit && hit.scrollIntoView) {
      try { hit.scrollIntoView({ block: 'nearest' }); } catch (e) { /* 老浏览器忽略 */ }
    }
  }

  /**
   * 点击侧栏目录：所有相关视图一起跟着走。
   *
   * 之前的毛病是"点目录只在曲库视图里生效"，在文件浏览里点目录没反应；
   * 反过来在文件浏览里进了别的目录，侧栏高亮也不会跟着变。
   */
  function treeSelect(path) {
    State.treePrefix = path || '';
    State.libFilters = Object.assign({}, State.libFilters, { prefix: State.treePrefix });
    highlightTreeNode();
    if (State.view === 'library') {
      if (views.library) views.library.reload();
    } else if (State.view === 'files') {
      if (views.files && views.files.go) views.files.go(State.treePrefix);
    } else {
      setView('library');
    }
  }

  /** 子视图切了目录之后，让侧栏高亮跟上（避免两边显示不一致）。 */
  function syncTree(path) {
    if ((State.treePrefix || '') === (path || '')) return;
    State.treePrefix = path || '';
    State.libFilters = Object.assign({}, State.libFilters, { prefix: State.treePrefix });
    highlightTreeNode();
  }

  // ------------------------------------------------------------ 测试模式
  /**
   * 测试模式横幅（只在 `MSH_TEST_MODE=1` 时出现）。
   *
   * 服务端已经往 HTML 里注入了常驻水印 `#msh-test-watermark`（关不掉），
   * 这里这条是**可关的**补充说明。
   *
   * ★ 位置必须**在顶栏下方**（`top:48px`）。第一版我写在 `top:22px`，
   *   结果正好盖住顶部导航栏（数据源/曲库浏览/文件浏览…），
   *   整排标签点不动 —— 截图一眼就看出来了。测试包最忌讳这种东西。
   *
   * 幂等：`loadSystem()` 每 20 秒会跑一次，这里必须自己判断"已经画过就别再画"。
   */
  function paintTestModeBanner(on) {
    const ID = 'msh-test-banner';
    // 布局开关：给 <html> 加 .test-mode，让 CSS 把整个 #app 往下顶出横幅的高度。
    // 只把横幅改 fixed 是不够的 —— 顶部导航（.topbar）就在 #app 里面，
    // 任何覆盖它上方的东西都会挡住标签的点击热区。实测 8 个标签全被挡住。
    const root = document.documentElement;
    if (!on) {
      const old = document.getElementById(ID);
      if (old) old.remove();
      root.classList.remove('test-mode');
      return;
    }
    if (document.getElementById(ID)) return;      // 已经画过
    // ★ 必须先查"用户是否关过"，**再**加 class。
    //   原来先 add('test-mode') 再查 Prefs 就 return —— 于是用户关掉横幅之后，
    //   每 20 秒的 `loadSystem()` 又会把 class 加回去：横幅没了，
    //   但顶部永远留着 30px 空白带。
    let closed = false;
    try {
      closed = !!(Prefs && Prefs.get && Prefs.get('testBannerClosed'));
    } catch (e) { /* Prefs 没起来就照画 */ }
    if (closed) return;
    root.classList.add('test-mode');
    const bar = document.createElement('div');
    bar.id = ID;
    bar.className = 'test-banner';
    bar.innerHTML = `<b style="white-space:nowrap">测试模式 · TEST MODE</b>
      <span>这是<b>演示环境</b>：曲库是随包附带的示例音频，不是你的真实音乐。
      可以随便点、随便试（含删除和搬运），但别把它当真实库用。</span>
      <span style="flex:1"></span>
      <button class="btn sm ghost" id="msh-test-close"
        style="white-space:nowrap">知道了</button>`;
    document.body.appendChild(bar);
    const btn = bar.querySelector('#msh-test-close');
    if (btn) {
      btn.onclick = () => {
        bar.remove();
        root.classList.remove('test-mode');       // 关掉横幅就把空间还回去
        try { if (Prefs && Prefs.set) Prefs.set('testBannerClosed', true); } catch (e) {}
      };
    }
  }

  // ------------------------------------------------------------ 系统状态
  async function loadSystem() {
    try {
      State.system = await API.system();
      State._sysTs = Date.now();
      // ★ 测试模式横幅：服务端已经注入了一条常驻水印（`#msh-test-watermark`），
      //   这里再补一条**说清怎么办**的提示，且允许用户关掉。
      //   两个都要：水印防"脚本没跑起来看不到"，横幅负责解释。
      try { paintTestModeBanner(State.system && State.system.test_mode); } catch (e) {}
      const cap = State.system.capabilities || {};
      const dbSize = U.fmtBytes(State.system.db_size);
      document.getElementById('sys-badge').innerHTML =
        `${State.system.source_count} 源 · 索引 ${dbSize}` +
        (cap.mutagen ? '' : ' · <span style="color:#ff9daa">缺 mutagen</span>') +
        (cap.pillow ? '' : ' · <span style="color:#ffd982">缺 Pillow</span>');
    } catch (e) { /* ignore */ }
  }

  // ------------------------------------------------------------ 任务
  async function refreshJobs() {
    let list = [];
    try { list = await API.jobs(); } catch (e) { return; }
    const active = list.filter((j) => j.state === 'running');
    const pill = document.getElementById('job-pill');
    if (active.length) {
      const j = active[0];
      pill.classList.remove('hidden');
      document.getElementById('job-text').textContent =
        `${jobKindName(j.kind)} ${j.done}/${j.total || '?'}` + (active.length > 1 ? ` (+${active.length - 1})` : '');
    } else {
      pill.classList.add('hidden');
    }
    State.jobs = list;
  }

  function jobKindName(k) {
    return (typeof TaskSync !== 'undefined' ? TaskSync.kindName(k) : null)
      || { scan: '扫描', hash: '校验', sync: '同步' }[k] || k;
  }

  /**
   * 统一的"开始一个会改文件的操作"入口。
   *
   * 所有写操作都得走这里，因为它做了两件必须的事：
   *   1. **问占用**：别人在跑就直接拒绝（不排队 —— 排队会基于过期状态操作）；
   *   2. **登记**：让别的页面知道"这个页面在忙"，并把任务 id 记下来，
   *      这样**刷新页面也能接上**，任务不会因为刷新而中断。
   *
   * `fn` 是真正干活的函数，返回 /api/execute 那样的结果。
   */
  async function runExclusive(kind, fn) {
    try {
      await TaskSync.begin(kind);
    } catch (e) {
      if (e.code === 409) {
        UI.modal({
          title: '暂时不能操作',
          size: 'sm',
          body: `<div style="line-height:1.8">${U.esc(e.message)}</div>
            <div class="muted" style="margin-top:10px;font-size:12px">
              任务在后台继续跑，刷新页面也不会中断。
              结束后这一页会自动恢复可用。</div>`,
          footer: '<button class="btn primary" data-act="ok">知道了</button>',
        });
      }
      throw e;
    }
    State.exclusiveBusy = true;
    paintBusyUi();
    // ★ 只在这里 finish **一次**。
    //   之前 try 里 finish(r.job)、finally 里又 finish(null)，
    //   每个写操作会发两次 ack + 两次跨页广播（历史缺陷又回来了）。
    let out;
    try {
      out = await fn();
      return out;
    } finally {
      State.exclusiveBusy = false;
      await TaskSync.finish(out && out.job);
      paintBusyUi();
    }
  }

  /** 有别人在忙时，把会冲突的按钮禁掉并说明原因。 */
  function paintBusyUi() {
    const s = (typeof TaskSync !== 'undefined') ? TaskSync.snapshot() : { busy: false };
    document.body.classList.toggle('task-busy', !!s.busy);
    document.querySelectorAll('[data-exclusive]').forEach((b) => {
      if (s.others) {
        b.disabled = true;
        if (!b.dataset.busyTitle) b.dataset.busyTitle = b.title || '';
        b.title = '另一个页面正在执行任务，请等它结束';
      } else if (b.dataset.busyTitle !== undefined) {
        b.disabled = false;
        b.title = b.dataset.busyTitle;
        delete b.dataset.busyTitle;
      }
    });
    const bar = document.getElementById('busy-bar');
    if (bar) {
      const bj = (typeof TaskSync !== 'undefined' && TaskSync.backgroundJob)
        ? TaskSync.backgroundJob : null;
      if (s.others && s.job) {
        bar.classList.remove('hidden');
        bar.innerHTML = `<span class="spinner"></span>
          <span>另一个页面正在执行 <b>${U.esc(jobKindName(s.job.kind))}</b>
          ${s.job.done || 0}/${s.job.total || '?'} —— 为避免文件状态冲突，
          本页暂时不能执行写操作</span>`;
      } else if (bj) {
        // ★ 非独占的长任务（对比 / 查重）也要有个"在跑"的提示。
        //   它们是有意不阻塞写操作的，所以 `/api/busy` 对它们恒 false ——
        //   结果就是对比跑了 40 秒、界面一片安静，用户以为卡死又点一次。
        //   这里只做**提示**，不禁用任何按钮（只读任务不该挡写操作）。
        bar.classList.remove('hidden');
        bar.innerHTML = `<span class="spinner"></span>
          <span>正在执行 <b>${U.esc(jobKindName(bj.kind))}</b>
          ${bj.done || 0}/${bj.total || '?'} ——
          <span class="muted">这是只读任务，不会影响你继续做其它操作</span></span>`;
      } else {
        bar.classList.add('hidden');
      }
    }
  }

  /**
   * 点 `[data-exclusive]` 按钮时的**最后一道防线**。
   *
   * `paintBusyUi()` 是"进入忙状态那一刻，把页面上**当时存在**的按钮禁掉"。
   * 但它有两个漏：弹窗里的按钮是**之后**才插进 DOM 的；文件视图的候选条
   * 每次选中变化都会重建（按钮全是新的、没被禁过）。
   * 所以光靠它，"另一个页面正在忙时别点写操作"是**拦不住**的。
   *
   * 这里挂在 document 的捕获阶段：不管按钮什么时候出现、由谁创建，
   * 只要它标了 `data-exclusive`，忙的时候就吃掉这次点击并说清原因。
   * 不依赖渲染时机，比逐个"记得调 paintBusyUi"可靠。
   */
  document.addEventListener('click', (ev) => {
    const t = ev.target && ev.target.closest
      ? ev.target.closest('[data-exclusive]') : null;
    if (!t) return;
    let s = { busy: false };
    try { s = TaskSync.snapshot() || s; } catch (e) { /* 没起来就放行 */ }
    if (!s.others) return;            // 自己这边忙不算（自己知道在干嘛）
    ev.preventDefault();
    ev.stopPropagation();
    if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
    const j = s.job || {};
    UI.toast(`另一个页面正在执行「${jobKindName(j.kind)}」`
      + `（${j.done || 0}/${j.total || '?'}），请等它结束后再操作 —— `
      + `同时改文件会基于过期的状态操作。`, 'warn', 7000);
  }, true);

  /**
   * 把一次执行记进操作日志，并**挂上重试能力**。
   *
   * 重试只挑"重试有意义"的步骤（被占用、空间不足这类）——
   * 文件突然不见了、目标已存在这些重试也是白搭，
   * 该做的是先扫描或改策略，所以不放进清单。
   *
   * @param {string} plan   显示名，如「查重清理」
   * @param {object} result /api/execute 的返回
   * @param {Array}  steps  原始步骤，重试时要用
   */
  function logOp(plan, result, steps) {
    const s = (result && result.summary) || {};
    const retryable = new Set(s.retryable_steps || []);
    const failed = (result && result.results) || [];
    State.opLog.unshift({
      at: Date.now(), plan, result,
      // ★ 契约：`retry()` **只生成"要重试哪几步"并返回一个闭包**，
      //   自己**不执行**。
      //
      //   踩过：这里原来直接 `await API.execute({execute:true})` 并返回
      //   undefined，而调用方（views.ops.js）把这个返回值当成"待执行的动作"：
      //     const steps = entry.retry(b);   // 里面已经把文件改了
      //     if (!steps) return;             // undefined → 直接退出
      //   → 点「重试」真的会改文件，但**不经过独占锁**，
      //     而且按钮不置灰、不显示"重试中…"，用户看到的是"点了没反应"。
      //   现在由调用方在 runExclusive 里执行，两边职责清楚。
      retry: retryable.size
        ? () => {
          const again = (steps || []).filter((x) => retryable.has(String(x.id)));
          if (!again.length) return null;
          return again;
        }
        : null,
      /** 给调用方显示用：这次能重试几步。 */
      retryCount: retryable.size,
      failedCount: failed.filter((x) => x.state === 'error').length,
    });
  }

  function showJobs() {
    const ctl = UI.modal({
      title: '任务中心',
      size: 'md',
      body: '<div id="jobs-body" class="muted">加载中…</div>',
      footer: '<button class="btn ghost" data-act="close">关闭</button>',
    });
    ctl.footer.querySelector('[data-act=close]').onclick = () => ctl.close();
    const render = async () => {
      const list = await API.jobs();
      const box = ctl.body.querySelector('#jobs-body');
      if (!list.length) { box.innerHTML = '<div class="muted">暂无任务</div>'; return; }
      box.innerHTML = list.map((j) => `
        <div class="card" style="margin-bottom:10px;padding:11px 13px">
          <div class="row" style="justify-content:space-between">
            <b>${jobKindName(j.kind)} · ${stateLabel(j)}</b>
            <span class="muted">${U.fmtAgo(j.started)} 开始${j.elapsed ? ' · ' + j.elapsed + 's' : ''}${j.eta ? ' · 预计剩余 ' + Math.round(j.eta) + 's' : ''}</span>
          </div>
          <div class="muted" style="margin:6px 0;font-size:12px">${U.esc(j.message || '')}</div>
          ${j.total ? `<div class="progress"><i style="width:${j.percent || 0}%"></i></div>
            <div class="muted" style="margin-top:4px;font-size:11px">${j.done}/${j.total} (${j.percent || 0}%)</div>` : ''}
          ${j.skipped_count ? `<div class="muted" style="margin-top:4px;font-size:11.5px">
            已跳过 ${j.skipped_count} 个（超时或出错，其余照常处理）</div>` : ''}
          ${j.stale ? `<div style="color:var(--warn);margin-top:6px;font-size:12px">
            心跳已停 ${j.idle}s，可能已经中断</div>` : ''}
          ${j.error ? `<div style="color:#ff9daa;margin-top:6px;font-size:12px">${U.esc(j.error)}</div>` : ''}
          <div class="row wrap" style="margin-top:8px;gap:6px">
            ${j.can_pause && !j.paused
              ? `<button class="btn sm ghost" data-pause="${j.id}">
                   ${U.icon('pause')}<span>暂停</span></button>` : ''}
            ${j.paused ? `<button class="btn sm primary" data-resume="${j.id}">
                   ${U.icon('resume')}<span>继续</span></button>` : ''}
            ${j.can_cancel ? `<button class="btn sm ghost" data-cancel="${j.id}">
                   ${U.icon('close')}<span>取消</span></button>` : ''}
            ${j.recoverable && !j.acked
              ? `<button class="btn sm" data-recover="${j.id}"
                   title="用 skip 策略重放未完成的步骤；已完成的会因源不存在被跳过，不会覆盖或重复">
                   ${U.icon('rollback')}<span>接着做完</span></button>` : ''}
          </div>
        </div>`).join('');
      box.querySelectorAll('[data-cancel]').forEach((b) => {
        b.onclick = async () => { await API.cancelJob(b.dataset.cancel); setTimeout(render, 400); };
      });
      box.querySelectorAll('[data-pause]').forEach((b) => {
        b.onclick = async () => {
          await API.pauseJob(b.dataset.pause);
          UI.toast('已暂停；已完成的步骤不会回退，点「继续」接着做', 'info', 5000);
          setTimeout(render, 400);
        };
      });
      box.querySelectorAll('[data-resume]').forEach((b) => {
        b.onclick = async () => { await API.resumeJob(b.dataset.resume); setTimeout(render, 400); };
      });
      box.querySelectorAll('[data-recover]').forEach((b) => {
        b.onclick = async () => {
          const ok = await UI.confirm(
            '接着做完这个被中断的任务？<br>'
            + '<span class="muted">会用「跳过同名」策略重放剩下的步骤：'
            + '已经完成的会因源文件不存在被跳过，不会覆盖、不会重复。</span>',
            { title: '接着做完', okText: '开始' });
          if (!ok) return;
          try {
            const r = await App.runExclusive('sync',
              () => API.recoverJob({ job: b.dataset.recover, client: TaskSync.ME }));
            const s = (r.result && r.result.summary) || {};
            UI.toast(`恢复完成：成功 ${s.done || 0}，跳过 ${s.skipped || 0}`
              + (s.error ? `，失败 ${s.error}` : ''), 'ok', 8000);
          } catch (e) {
            UI.toast('恢复失败：' + e.message, 'err', 8000);
          }
          setTimeout(render, 600);
        };
      });
    };
    render();
    const t = setInterval(() => { if (!document.body.contains(ctl.el)) { clearInterval(t); return; } render(); }, 1500);
  }

  /** 任务状态的中文标签（以前直接把 state 原样显示，看到的是英文）。 */
  function stateLabel(j) {
    if (j.paused) return '已暂停';
    return {
      running: '进行中', done: '已完成', error: '失败',
      cancelled: '已取消', paused: '已暂停',
    }[j.state] || j.state;
  }

  /**
   * 启动时检查"上次没做完的任务"。
   *
   * 程序崩溃/被强杀/断电后，DB 里会留着没完成的任务记录。
   * 这里主动问用户要不要接着做完 —— 而不是让他自己回忆改了哪些文件。
   */
  async function checkUnfinished() {
    let r = null;
    try { r = await API.unfinished(); } catch (e) { return; }
    if (!r || !r.count) return;
    const rec = r.recoverable || 0;
    const ctl = UI.modal({
      title: '上次有任务没做完',
      size: 'sm',
      body: `
        <div style="line-height:1.7;font-size:13px">
          检测到 <b>${r.count}</b> 个上次没完成的任务${rec ? `，其中 <b>${rec}</b> 个可以接着做完` : ''}。
          <div class="muted" style="margin-top:8px;font-size:12px">
            可能是程序异常退出（崩溃 / 被强制结束 / 断电）留下的。
            恢复会用「跳过同名」策略重放：已经处理过的会被跳过，
            <b>不会覆盖也不会重复</b>。
          </div>
        </div>
        <div class="hard-list" style="margin-top:10px">
          ${r.jobs.slice(0, 6).map((j) => `
            <div>${U.esc(jobKindName(j.kind))} · ${U.esc(stateLabel(j))} ·
              ${j.done}/${j.total || '?'} ${j.recoverable ? '（可恢复）' : '（无可重放步骤）'}</div>`).join('')}
        </div>`,
      footer: `${rec ? '<button class="btn primary" data-act="go">接着做完</button>' : ''}
        <button class="btn ghost" data-act="later">稍后再说</button>`,
    });
    const go = ctl.footer.querySelector('[data-act=go]');
    if (go) go.onclick = async () => {
      ctl.close();
      const targets = r.jobs.filter((j) => j.recoverable);
      let okN = 0, failN = 0;
      for (const j of targets) {
        try {
          await App.runExclusive('sync',
            () => API.recoverJob({ job: j.id, client: TaskSync.ME }));
          okN++;
        } catch (e) { failN++; }
      }
      UI.toast(`恢复完成：成功 ${okN} 个${failN ? `，失败 ${failN} 个` : ''}`,
        failN ? 'warn' : 'ok', 8000);
      await loadSources();
    };
    ctl.footer.querySelector('[data-act=later]').onclick = () => ctl.close();
  }

  /**
   * 数据自动更新。
   *
   * 目标：用户不该为了看到"刚做完的操作生效了"而按 F5。
   *
   * 做法是**变化检测**而不是无脑轮询重载：
   *   · 每 4 秒问一次 `/api/state`（只读索引 + 一个 stat，开销可忽略）
   *   · 数字没变 → 什么都不做（不闪、不打断滚动和选中）
   *   · 变了 → 只刷新**当前可见**的那个视图
   *   · 页面在后台（切到别的标签）时完全不请求
   *   · 有任务在跑时拉长间隔 —— 那时数据一直在变，
   *     频繁重载反而会打断用户看进度
   */
  let lastRev = null;
  let refreshTimer = null;
  let refreshTick = 0;

  function currentSig(r) {
    return [r.tracks, r.sources, r.jobs_done].join(':');
  }

  async function autoRefreshTick() {
    if (document.hidden) return;                 // 后台标签不做无用功
    refreshTick++;
    // 有任务在跑：每 4 次（约 16 秒）才查一次；空闲时每次都查
    if (lastRev && lastRev.busy && refreshTick % 4 !== 0) return;
    let r = null;
    try { r = await API.state(State.activeSource || ''); } catch (e) { return; }
    const sig = currentSig(r);
    const prevSig = lastRev ? currentSig(lastRev) : null;
    const wasBusy = lastRev ? lastRev.busy : false;
    lastRev = r;
    if (prevSig === null) return;                // 第一次只记基线
    const jobJustFinished = wasBusy && !r.busy;
    if (sig === prevSig && !jobJustFinished) return;

    // 数据真的变了（或任务刚结束）→ 只刷新当前视图
    refreshCurrentView();
    // 任务刚结束：状态栏也刷一下，用户能看到"已完成"
    if (jobJustFinished) { refreshJobs(); loadSystem(); }
  }

  function refreshCurrentView() {
    const v = State.view;
    const mod = views[v];
    if (!mod) return;
    try {
      if (v === 'files') {
        // 文件浏览：重载当前目录。保留选中（按 did 过滤掉不存在的）
        mod.reload && mod.reload();
      } else if (v === 'library') {
        mod.reload && mod.reload();
      } else if (v === 'station') {
        mod.load && mod.load();
      } else if (v === 'ops' || v === 'duplicates' || v === 'compare') {
        mod.render && mod.render(document.getElementById('view-' + v));
      } else if (v === 'sources') {
        loadSources();
      }
    } catch (e) { /* 刷新失败不该影响使用 */ }
  }

  function startAutoRefresh(ms = 4000) {
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = setInterval(() => { autoRefreshTick().catch(() => {}); }, ms);
    // 切回前台时立刻查一次（后台期间可能已经变了很多）
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) autoRefreshTick().catch(() => {});
    });
  }

  // ------------------------------------------------------------ 扫描
  function scanDialog(sid) {
    const s = sourceById(sid);
    const ctl = UI.modal({
      title: `扫描 · ${s ? s.name : sid}`,
      size: 'sm',
      body: `
        <div class="field"><label>只扫描子目录（留空 = 整库）</label>
          <input type="text" id="scan-subpath" placeholder="例如 Rock/2024"></div>
        <div class="field"><label>并发线程数</label>
          <input type="number" id="scan-workers" value="${(State.system && 8) || 8}" min="1" max="32"></div>
        <label class="check"><input type="checkbox" id="scan-hash"> 扫描时顺带计算抽样哈希（更慢，但查重更准）</label>
        <div class="hint" style="margin-top:8px">增量扫描：文件大小/修改时间没变就直接用索引缓存，不会重复解析。</div>`,
      footer: `<button class="btn ghost" data-act="no">取消</button>
               <button class="btn primary" data-act="yes">开始扫描</button>`,
      onMount(c) {
        c.footer.querySelector('[data-act=no]').onclick = () => c.close();
        c.footer.querySelector('[data-act=yes]').onclick = async () => {
          const opts = {
            subpath: c.body.querySelector('#scan-subpath').value.trim(),
            workers: Number(c.body.querySelector('#scan-workers').value) || 8,
            compute_hash: c.body.querySelector('#scan-hash').checked,
          };
          c.close();
          try {
            await API.scan(sid, opts);
            UI.toast('扫描任务已启动，进度显示在右上角', 'ok');
            pollScan(sid);
          } catch (e) { UI.toast('启动失败：' + U.esc(e.message), 'err'); }
        };
      },
    });
    void ctl;
  }

  async function pollScan(sid) {
    for (;;) {
      await new Promise((r) => setTimeout(r, 800));
      const j = await API.jobs();
      await refreshJobs();
      const running = j.find((x) => x.state === 'running' && x.kind === 'scan');
      if (!running) {
        const last = j.find((x) => x.kind === 'scan');
        if (last && last.state === 'done') {
          const r = last.result || {};
          UI.toast(`扫描完成：${r.seen || 0} 个文件，解析 ${r.parsed || 0}，缓存命中 ${r.cached || 0}，失败 ${r.errors || 0}`, 'ok', 6000);
        } else if (last && last.state === 'error') {
          UI.toast('扫描失败：' + U.esc(last.error || ''), 'err', 8000);
        }
        await loadSources();
        if (State.activeSource === sid) await renderTree();
        if (State.view === 'library' && views.library) views.library.reload();
        return;
      }
    }
  }

  // ------------------------------------------------------------ 播放器
  const audio = () => document.getElementById('audio');

  /**
   * 播放队列。
   *
   * 原来只有 `State.tracks`（当前这一页的曲目）能播，"下一首"走到页尾就停了 ——
   * 而曲库是**分页**的，翻页后 `State.tracks` 会被换成新的一页，
   * 队列跟着断掉。所以这里维护一个独立的队列：
   *   · 队列里存**完整的曲目对象**，不只是 id（翻页后也能继续播）；
   *   · 支持"接着放 / 插到下一首 / 替换"三种入队方式；
   *   · 队列对用户可见可编辑（播放器上的队列按钮）。
   */
  const queue = {
    items: [],          // [{ track, label }]
    index: -1,

    set(tracks, startId, label = '') {
      this.items = (tracks || []).filter(Boolean).map((t) => ({ track: t, label }));
      this.index = Math.max(0, this.items.findIndex((x) => x.track.id === startId));
      return this;
    },

    add(tracks, label = '', { next = false } = {}) {
      const add = (tracks || []).filter(Boolean)
        .filter((t) => !this.items.some((x) => x.track.id === t.id))
        .map((t) => ({ track: t, label }));
      if (!add.length) return 0;
      if (next && this.index >= 0) this.items.splice(this.index + 1, 0, ...add);
      else this.items.push(...add);
      return add.length;
    },

    clear() { this.items = []; this.index = -1; },

    /**
     * 剪掉已经失效的条目（曲目对象没了）。
     *
     * 为什么需要：曲目可能因为重新扫描/删除而消失，队列里留着旧对象，
     * 播到它就会静默失败（`playTrack(undefined)` 直接 return），
     * 表现是"播放器点了没反应"。播之前先剪一遍最省事。
     */
    prune() {
      // 剪掉失效条目（track 为空 / id 为空）。曲目被删或重扫之后会出现。
      //
      // ★ 当前位置按 **track id** 认，不按 index 认：
      //   别处可能对 items 做过 unshift/splice，index 会顺移。
      // ★ 当前这首本身也被删了的话：落到**它原来位置之后的第一个有效条目**，
      //   后面没有就往前退。原来我写"退到同序号"，序号会越界，
      //   把用户不期望的条目选成当前（测试就是用这个抓到的）。
      const ok = (x) => !!(x && x.track && x.track.id);
      const before = this.items;
      const oldIndex = this.index;
      const cur = (oldIndex >= 0 && oldIndex < before.length && ok(before[oldIndex]))
        ? before[oldIndex] : null;
      if (!before.some((x) => !ok(x))) return 0;      // 没有坏条目，什么都不动
      const out = before.filter(ok);
      const removed = before.length - out.length;
      this.items = out;
      if (!out.length) { this.index = -1; return removed; }
      let i = cur ? out.findIndex((x) => x.track.id === cur.track.id) : -1;
      if (i < 0) {
        // 当前这首自己被剪掉了：取"原来位置之后"的第一个有效条目
        let idxAfter = -1;
        for (let k = oldIndex + 1; k < before.length; k++) {
          if (ok(before[k])) { idxAfter = out.indexOf(before[k]); break; }
        }
        i = idxAfter >= 0 ? idxAfter : out.length - 1;
      }
      this.index = Math.max(0, Math.min(i, out.length - 1));
      return removed;
    },

    /**
     * 当前这一项 —— ★ 返回的是 `{track, label}` **条目**，不是裸曲目。
     *
     * 踩过（这是"点了播放没反应"的真凶）：
     * 这里原来返回 `this.items[this.index].track`（裸曲目），
     * 而仅有的两个使用方都按**条目**在用：
     *     playList:   `const cur = queue.current; playTrack(cur.track, {label: cur.label})`
     *     stepTrack:  `const nx = queue.step(dir); playTrack(nx.track, {label: nx.label})`
     * 于是 `cur.track` 是 `undefined` → `playTrack(undefined)` 在开头一句
     * `if (!t) return;` 就静静返回了。
     *
     * 表现完全符合用户反馈的"播放器中看不中用"：
     * 队列被正确填满、`queue.current` 返回的对象看起来也对（就是那首歌），
     * 但标题一直是"未播放"、`audio.src` 一直是空、`audio.paused` 一直是 true。
     * 而且**不抛任何异常**，控制台只有一句 `[playTrack] 没有曲目对象`。
     *
     * `step()` 返回的是条目，两者形状保持一致，调用方就不用记两套约定。
     */
    // 注意：这里**不过滤**坏条目 —— 契约就是"返回当前位置上的条目"，
    // 由 `prune()` 负责先把坏条目清掉。早先我想在这里偷偷返回 null，
    // 会破坏 `current` 的形状契约（有测试盯着），已回退。
    get current() {
      return this.index >= 0 && this.index < this.items.length
        ? this.items[this.index] : null;
    },

    /** 前后跳一首。列表结尾**不循环**（循环会让人误以为放完了还在放）。 */
    /**
     * 前后跳一首。
     *
     * ★ 必须**跳过失效条目**，不能把它返回给调用方。
     *   踩过：队列里混进 `{track: null}`（曲目被删/重扫）之后，
     *   `step()` 把坏条目交出去 → `playTrack(null)` 在开头静默 return →
     *   表现就是"按了下一首完全没反应"。
     */
    step(dir) {
      if (!this.items.length) return null;
      const ok = (x) => !!(x && x.track && x.track.id);
      let i = this.index + dir;
      if (i < 0) i = 0;
      while (i >= 0 && i < this.items.length && !ok(this.items[i])) i += dir;
      if (i < 0 || i >= this.items.length) return null;
      this.index = i;
      return this.items[i];
    },
  };

  /**
   * 播一首。
   *
   * `_fromQueue` 是内部参数：`playList` / 上一首下一首 / 队列弹窗点选
   * 都已经把队列安排好了，不要再让这里重排。
   */
  function playTrack(t, { label = '', _fromQueue = false } = {}) {
    if (!t) {
      try { console.warn('[playTrack] 没有曲目对象'); } catch (e) {}
      return;
    }
    // ★ 已经失效的条目一律剪掉 —— 老 bug 正是这个：播完之后
    //   `next()` 一路跳过不存在的曲目，看起来像"播放器没反应"。
    queue.prune();
    // ★ 保证"正在播的这首"一定在队列里。
    //
    //   踩过的 bug：`playTrack` 原来**只换歌、不动队列**。
    //   于是在「曲库」播过一次（队列被填成整库 2301 首）之后，
    //   再去「文件浏览」双击单曲 → 歌换了，队列还是那 2301 首，
    //   而**正在播的这首根本不在队列里**。
    //   用户看到的就是"播单个音乐时，播放列表里出现整个库源"。
    //
    //   规则（可预期、不毁用户手动排的队列）：
    //     · 这首已经在队列里 → 只把当前位置移过去，队列整体不动；
    //     · 不在队列里、且队列非空 → 队列换成**只这一首**（旧队列本来就跟当前无关了）；
    //     · 队列为空 → 补成只这一首。
    if (!_fromQueue) {
      const at = queue.items.findIndex((x) => x.track && x.track.id === t.id);
      if (at >= 0) {
        // 已经在队列里：只移动当前位置，**不动**用户可能手动排好的其余条目
        queue.index = at;
      } else {
        // 不在队列里：队列本来就跟"正在播的这首"无关了，整个换成这一首。
        queue.set([t], t.id, label);
      }
    }
    State.curTrackId = t.id;
    const a = audio();
    if (!a) {
      try { console.warn('[playTrack] 找不到 #audio 元素'); } catch (e) {}
      return;
    }
    a.src = t.stream_url || ('/api/preview/' + t.id);
    a.volume = (document.getElementById('vol').value || 100) / 100;
    a.play().catch((e) => UI.toast('播放失败：' + U.esc(e.message), 'err'));
    const src = sourceById(t.source_id);
    document.getElementById('np-title').textContent = t.title || t.name;
    document.getElementById('np-sub').textContent =
      [t.artist, t.album].filter(Boolean).join(' · ') || U.parent(t.path);
    const img = t.cover_url || (t.has_cover ? `/api/cover/${t.id}?size=96` : null);
    document.getElementById('np-cover').innerHTML = img
      ? `<img src="${img}" alt="">` : U.icon('music', 'ico lg');
    document.getElementById('stream-badge').textContent =
      (src ? src.name + ' · ' : '') + (label || U.qualityLabel(t));
    document.querySelectorAll('tr.sel').forEach((r) => r.classList.remove('sel'));
    paintNowPlaying();
  }

  /** 把"正在播放"高亮到当前列表里（曲库页/文件页都会用到）。 */
  function paintNowPlaying() {
    const cur = State.curTrackId;
    document.querySelectorAll('[data-play-id]').forEach((el) => {
      el.classList.toggle('playing', Number(el.dataset.playId) === Number(cur));
    });
  }

  /** 播放一组曲目：自动填队列并从这里开始。 */
  function playList(tracks, startId, label = '') {
    queue.set(tracks, startId, label);
    const cur = queue.current;
    if (!cur) {
      // 宁可明确报出来，也不要"点了没反应" —— 之前这里静默 return，
      // 表现就是"专辑点了播放但什么都没发生"，极难查。
      try {
        console.warn('[playList] 队列里找不到要播的曲目', {
          startId, items: queue.items.length, index: queue.index,
          ids: queue.items.slice(0, 5).map((x) => x.track && x.track.id),
        });
      } catch (e) { /* ignore */ }
      UI.toast('这一组里没有可播放的曲目', 'warn', 4000);
      return 0;
    }
    playTrack(cur.track, { label: cur.label, _fromQueue: true });
    return queue.items.length;
  }

  function stepTrack(dir, { quiet = false } = {}) {
    // 队列为空时退回"当前列表"，保持旧行为
    if (!queue.items.length) {
      const list = State.tracks || [];
      if (!list.length) return false;
      const i = Math.max(0, list.findIndex((t) => t.id === State.curTrackId));
      const t = list[i + dir] || list[dir > 0 ? 0 : list.length - 1];
      if (t) {
        // ★ 顺手把这份列表装进队列，再从这个位置往后走。
        //   原来这里直接 `playTrack(t)` —— 于是"队列为空时按下一首"每按一次
        //   都会重新退回列表算一遍，而且队列永远是空的。装进来之后
        //   "上一首/下一首"就有了真实上下文，队列弹窗里也看得到。
        queue.set(list, t.id, '当前列表');
        playTrack(t, { label: queue.current ? queue.current.label : '', _fromQueue: true });
        return true;
      }
      return false;
    }
    const nx = queue.step(dir);
    if (!nx) {
      // `quiet` 用于"放完了自动下一首"这种场景：到头了别弹提示打扰人
      if (!quiet) UI.toast(dir > 0 ? '已经是队列最后一首' : '已经是队列第一首', 'info', 2600);
      return false;
    }
    playTrack(nx.track, { label: nx.label, _fromQueue: true });
    return true;
  }

  function mountPlayer() {
    const a = audio();
    const btnPlay = document.getElementById('btn-play');
    const seek = document.getElementById('seek');
    btnPlay.onclick = () => {
      if (!a.src) { stepTrack(1); return; }
      if (a.paused) a.play(); else a.pause();
    };
    document.getElementById('btn-prev').onclick = () => stepTrack(-1);
    document.getElementById('btn-next').onclick = () => stepTrack(1);
    a.onplay = () => { btnPlay.textContent = '⏸'; };
    a.onpause = () => { btnPlay.innerHTML = U.icon('play'); };
    a.onerror = () => { UI.toast('音频加载失败：源文件可能不可达', 'err'); };
    a.ontimeupdate = () => {
      const d = a.duration || 0;
      document.getElementById('time').textContent = U.fmtDur(a.currentTime) + ' / ' + U.fmtDur(d);
      if (d && !seek.dataset.dragging) seek.value = Math.round(a.currentTime / d * 1000);
    };
    a.onended = () => {
      if (loopMode === 'one') { a.currentTime = 0; a.play().catch(() => {}); return; }
      // ★ 随机播放必须在这里处理。另外加一个 `ended` 监听器会在同一轮事件里
      //   两个回调都触发 —— 一首播完切两首歌，UI 还跟着竞态。
      if (shuffle && queue.items.length >= 2) {
        let i = queue.index;
        for (let k = 0; k < 8 && i === queue.index; k++) {
          i = Math.floor(Math.random() * queue.items.length);
        }
        queue.index = i;
        const it = queue.current;
        if (it) playTrack(it.track, { label: it.label, _fromQueue: true });
        return;
      }
      const ok = stepTrack(1, { quiet: true });
      if (!ok && loopMode === 'all' && queue.items.length) {
        queue.index = 0;
        const f = queue.current;
        if (f) playTrack(f.track, { label: f.label, _fromQueue: true });
      }
    };
    seek.oninput = () => { seek.dataset.dragging = '1'; };
    seek.onchange = () => {
      const d = a.duration || 0;
      if (d) a.currentTime = seek.value / 1000 * d;
      delete seek.dataset.dragging;
    };
    document.getElementById('vol').oninput = (e) => {
      a.volume = e.target.value / 100;
      // 记住音量：以前每次刷新都回到 100%，容易炸耳朵
      try { Prefs.set('volume', Number(e.target.value)); } catch (err) {}
    };
    const savedVol = (() => {
      try { return Prefs.get('volume', null); } catch (e) { return null; }
    })();
    if (savedVol !== null) {
      document.getElementById('vol').value = savedVol;
      a.volume = Number(savedVol) / 100;
    }
    document.getElementById('job-pill').onclick = showJobs;
    mountPlayerExtras();
  }

  /** 随机 / 循环 / 队列面板。 */
  let loopMode = 'off';          // off | all | one
  let shuffle = false;

  function mountPlayerExtras() {
    const btnS = document.getElementById('btn-shuffle');
    const btnL = document.getElementById('btn-loop');
    const btnQ = document.getElementById('btn-queue');
    const paintModes = () => {
      if (btnS) btnS.classList.toggle('on', shuffle);
      if (btnL) {
        btnL.classList.toggle('on', loopMode !== 'off');
        btnL.title = loopMode === 'one' ? '单曲循环（点击关闭）'
          : loopMode === 'all' ? '队列循环（点击切单曲）'
            : '循环：关（点击开启队列循环）';
        const use = btnL.querySelector('use');
        if (use) use.setAttribute('href', loopMode === 'one' ? '#i-loop-one' : '#i-loop');
      }
      const c = document.getElementById('q-count');
      if (c) {
        c.textContent = queue.items.length ? String(queue.items.length) : '';
        c.classList.toggle('hidden', !queue.items.length);
      }
    };
    if (btnS) btnS.onclick = () => { shuffle = !shuffle; paintModes();
      UI.toast(shuffle ? '随机播放已开' : '随机播放已关', 'info', 2200); };
    if (btnL) btnL.onclick = () => {
      loopMode = loopMode === 'off' ? 'all' : loopMode === 'all' ? 'one' : 'off';
      paintModes();
      UI.toast({ off: '循环已关', all: '队列循环', one: '单曲循环' }[loopMode], 'info', 2200);
    };
    if (btnQ) btnQ.onclick = () => queueDialog();
    paintModes();
    App._paintPlayerModes = paintModes;

    // 随机切歌已经并进 `a.onended`（见 audio 初始化处）。
    // 这里**不能**再加 `ended` 监听器：同一轮事件会触发两次切歌。
  }

  /** 播放队列面板：可看、可跳、可清、可移出单首。 */
  function queueDialog() {
    if (!queue.items.length) {
      return UI.toast('播放队列是空的。在曲库页点专辑或曲目的播放按钮就会填进来。',
        'info', 6000);
    }
    const ctl = UI.modal({
      title: `播放队列（${queue.items.length}）`,
      size: 'md',
      body: `<div class="q-list" id="q-list"></div>`,
      footer: `<button class="btn ghost" data-act="clr">清空队列</button>
        <div class="grow"></div>
        <button class="btn primary" data-act="close">关闭</button>`,
    });
    const paint = () => {
      const box = ctl.body.querySelector('#q-list');
      box.innerHTML = queue.items.map((it, i) => {
        const t = it.track;
        const cur = i === queue.index;
        return `<div class="q-item ${cur ? 'cur' : ''}" data-i="${i}">
          <span class="q-i">${cur ? U.icon('eq') : (i + 1)}</span>
          ${UI.coverHtml(t, 'cover sm')}
          <div class="q-meta">
            <div class="ell">${U.esc(t.title || t.name)}</div>
            <div class="muted ell" style="font-size:11.5px">${
              U.esc([t.artist, t.album].filter(Boolean).join(' · ')
                || U.parent(t.path))}</div>
          </div>
          <span class="muted" style="font-size:11.5px">${U.fmtDur(t.duration)}</span>
          <button class="btn ghost icon sm" data-rm="${i}" title="移出队列">
            ${U.icon('close')}</button>
        </div>`;
      }).join('');
      box.querySelectorAll('.q-item').forEach((el) => {
        el.onclick = (e) => {
          if (e.target.closest('[data-rm]')) return;
          queue.index = Number(el.dataset.i);
          const it = queue.current;
          if (it) playTrack(it.track, { label: it.label, _fromQueue: true });
          paint();
        };
      });
      box.querySelectorAll('[data-rm]').forEach((b) => {
        b.onclick = (e) => {
          e.stopPropagation();
          const i = Number(b.dataset.rm);
          queue.items.splice(i, 1);
          if (i < queue.index) queue.index--;
          else if (i === queue.index) queue.index = Math.min(queue.index, queue.items.length - 1);
          if (!queue.items.length) { ctl.close(); }
          paint();
          if (App._paintPlayerModes) App._paintPlayerModes();
        };
      });
    };
    paint();
    ctl.footer.querySelector('[data-act=close]').onclick = () => ctl.close();
    ctl.footer.querySelector('[data-act=clr]').onclick = () => {
      queue.clear();
      if (App._paintPlayerModes) App._paintPlayerModes();
      ctl.close();
      UI.toast('队列已清空', 'info', 2600);
    };
  }

  /**
   * 左上角汉堡键：显示/隐藏左侧导航窗格。
   *
   * 以前它只切 `.open`，而那个类只在窄屏媒体查询里有样式 —— 宽屏下点了
   * 完全没反应，看起来就是个废按钮。现在宽屏也能折叠，并把选择记在
   * localStorage 里，下次打开保持。
   *
   * 放在 boot() 里而不是 mountPlayer() 里：以前它挂在播放器初始化流程上，
   * 一旦那条路径变了按钮就悄悄失效（这正是它"没用"的原因之一）。
   */
  function mountNavToggle() {
    const navBtn = document.getElementById('btn-nav');
    if (!navBtn) return;
    const app = document.getElementById('app');
    const sidebar = document.getElementById('sidebar');
    const applyNav = (hidden) => {
      app.classList.toggle('nav-hidden', !!hidden);
      // 折叠时清掉浮层态，免得下次在窄屏上撞车
      if (hidden && sidebar) sidebar.classList.remove('open');
      navBtn.title = hidden
        ? '显示导航窗格（Ctrl+B）—— 曲库的目录选择、数据源切换都在这里'
        : '隐藏导航窗格（Ctrl+B）';
      navBtn.setAttribute('aria-label', navBtn.title);
      navBtn.setAttribute('aria-expanded', hidden ? 'false' : 'true');
      try { Prefs.set('navHidden', !!hidden); } catch (e) { /* ignore */ }
      // ★ 联动：侧栏收起来后，依赖它的视图要把入口搬到页面里，
      //   否则"曲库按目录浏览"就没法用了 —— 这正是汉堡键被说"联动性差"的原因。
      syncNavFallbacks(!!hidden);
    };
    let navHidden = false;
    try { navHidden = Prefs.get('navHidden', false) === true; } catch (e) { /* ignore */ }
    applyNav(navHidden);
    navBtn.onclick = () => {
      // 窄屏下侧栏是浮层，走 .open 那套；宽屏走折叠
      let narrow = false;
      try { narrow = window.matchMedia('(max-width: 840px)').matches; } catch (e) { /* ignore */ }
      if (narrow && sidebar) {
        sidebar.classList.toggle('open');
        return;
      }
      applyNav(!app.classList.contains('nav-hidden'));
    };
    // Ctrl+B 是各家文件管理器的通用快捷键，顺手支持
    document.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && (e.key === 'b' || e.key === 'B')) {
        e.preventDefault();
        applyNav(!app.classList.contains('nav-hidden'));
      }
      // Q = 队列，S = 随机，L = 循环：播放器常用键（输入框里不抢）
      const tag = (e.target && e.target.tagName) || '';
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag) || e.ctrlKey || e.metaKey) return;
      const k = e.key.toLowerCase();
      if (k === 'q') { e.preventDefault(); queueDialog(); }
      else if (k === 's') { e.preventDefault(); document.getElementById('btn-shuffle')?.click(); }
      else if (k === 'l') { e.preventDefault(); document.getElementById('btn-loop')?.click(); }
    });
  }

  /**
   * 侧栏折叠后，把"必须在侧栏里才能做的事"补到页面内。
   *
   * 具体就是目录树：曲库浏览的"按目录筛选"和数据源切换原本只在侧栏，
   * 一折叠就等于功能消失。这里在视图顶部放一个可展开的入口。
   */
  function syncNavFallbacks(hidden) {
    const map = [
      ['library', '#lb-src'],
      ['files', '#fb-src'],
    ];
    map.forEach(([view, anchor]) => {
      const root = document.getElementById('view-' + view);
      if (!root) return;
      let strip = root.querySelector('.nav-fallback');
      if (!hidden) {
        if (strip) strip.remove();
        return;
      }
      if (strip) { paintNavFallback(view, strip); return; }
      const anchorEl = root.querySelector(anchor);
      if (!anchorEl) return;
      strip = document.createElement('div');
      strip.className = 'nav-fallback';
      root.insertBefore(strip, root.firstChild);
      paintNavFallback(view, strip);
    });
  }

  function paintNavFallback(view, strip) {
    const srcs = State.sources || [];
    const cur = State.activeSource || '';
    strip.innerHTML = `
      <span class="muted" style="font-size:12px">导航窗格已收起</span>
      <select class="nav-fb-src" style="width:auto;min-width:150px">
        ${srcs.map((s) => `<option value="${U.esc(s.id)}" ${
          s.id === cur ? 'selected' : ''}>${U.esc(s.name)}</option>`).join('')}
      </select>
      <button class="btn sm ghost" data-fb-tree>
        ${U.icon('folder')}<span>选择目录…</span></button>
      <button class="btn sm ghost" data-fb-show
        title="把左侧导航窗格显示回来（Ctrl+B）">
        ${U.icon('menu')}<span>显示侧栏</span></button>`;
    const sel = strip.querySelector('.nav-fb-src');
    if (sel) sel.onchange = () => selectSource(sel.value);
    strip.querySelector('[data-fb-tree]').onclick = () => {
      const v = views[view];
      if (v && v.pickFolder) v.pickFolder();
      else setView(view);
    };
    strip.querySelector('[data-fb-show]').onclick = () => {
      const app = document.getElementById('app');
      app.classList.remove('nav-hidden');
      try { Prefs.set('navHidden', false); } catch (e) {}
      syncNavFallbacks(false);
      const b = document.getElementById('btn-nav');
      if (b) { b.title = '隐藏导航窗格（Ctrl+B）'; b.setAttribute('aria-expanded', 'true'); }
    };
  }

  // ------------------------------------------------------------ 启动
  async function boot() {
    // 逐段初始化并各自兜底：以前是串成一条直线，中间任何一步抛错，
    // 后面的初始化就**全部悄悄跳过** —— 表现是"按钮点了没反应"，
    // 而且不报错，极难查。
    const step = (name, fn) => {
      try { fn(); return true; }
      catch (e) {
        State.bootErrors = (State.bootErrors || []);
        State.bootErrors.push(`${name}: ${e && e.message}`);
        try { console.error(`[boot] ${name} 失败`, e); } catch (err) { /* ignore */ }
        return false;
      }
    };
    step('播放器', mountPlayer);
    step('导航窗格', mountNavToggle);
    step('恢复后台任务', mountBackgroundTaskSync);
    // 上次没做完的任务：问一句要不要接着做完（崩溃恢复）
    setTimeout(() => { checkUnfinished().catch(() => {}); }, 1500);
    document.querySelectorAll('.tab').forEach((t) => { t.onclick = () => setView(t.dataset.view); });
    document.getElementById('btn-add-source').onclick = () => {
      setView('sources');
      if (views.sources) views.sources.openAdd();
    };
    document.getElementById('btn-reload-cfg').onclick = async () => {
      await API.reload();
      await loadSources();
      UI.toast('配置已重新加载', 'ok');
    };
    try {
      await loadSources();
      await loadSystem();
      // 回到上次停留的页面（首次使用还是"数据源"）
      let first = 'sources';
      try {
        const lv = Prefs.get('lastView', '');
        if (lv && document.getElementById('view-' + lv)) first = lv;
      } catch (e) { /* 用默认 */ }
      setView(first);
    } catch (e) {
      UI.toast('初始化失败：' + U.esc(e.message), 'err', 9000);
    }
    setTimeout(() => {
      if (State.bootErrors && State.bootErrors.length) {
        UI.toast('部分功能初始化失败：' + State.bootErrors.join('；'),
          'err', 12000);
      }
    }, 800);
    setInterval(refreshJobs, 1500);
    setInterval(loadSystem, 20000);
    // 每隔 60 秒重新探活一次（只 stat 根目录，开销可以忽略）：
    // 移动硬盘/网络盘掉线时侧栏能自己变红，不用手点
    setInterval(refreshSourceStatus, 60000);
    window.addEventListener('focus', () => {
      if (Date.now() - (State.srcStatusAt || 0) > 30000) refreshSourceStatus();
    });
    refreshJobs();
    // 数据自动更新：不用用户手动刷新
    try { startAutoRefresh(4000); } catch (e) { /* 起不来也不影响使用 */ }
  }

  /** 多页面同步 / 刷新恢复（独立成函数，方便单独 try 住）。 */
  function mountBackgroundTaskSync() {
    if (typeof TaskSync === 'undefined') return;
    TaskSync.onChange(() => paintBusyUi());
    TaskSync.startPolling(3000);
    TaskSync.resume().then((resumed) => {
      if (resumed) {
        UI.toast(`已接上后台任务：${jobKindName(resumed.kind)} `
          + `${resumed.done || 0}/${resumed.total || '?'}`, 'info', 6000);
      }
    }).catch(() => { /* 接不上就算了，不影响使用 */ });
    paintBusyUi();
  }

  return {
    boot, setView, registerView, viewApi, loadSources, loadSystem, renderSidebar, renderTree, logOp,
    treeSelect, syncTree, highlightTreeNode, mountNavToggle,
    refreshSourceStatus, statusOf, dotHtml,
    selectSource, sourceById, scanDialog, pollScan, showJobs, refreshJobs,
    playTrack, jobKindName, audio,
    runExclusive, paintBusyUi,
    queue, playList, paintNowPlaying,
  };
})();
