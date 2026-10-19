/* ============================================================
   视图：曲库浏览（文件名 / 封面 / 歌曲信息 / 试听）
   ============================================================ */
(() => {
  let el = null;
  let data = null;
  let detailId = null;
  let pageSize = 300;
  let loading = false;
// 上一次查询还没回来时又来了一次刷新 → 记下来，结束后补跑一次
let pendingReload = false;
  // 显示方式：list（一行一首）/ album（按专辑分组，像网页播放器）
  let viewMode = 'list';
  try { viewMode = Prefs.get('libMode', 'list') || 'list'; } catch (e) { /* ignore */ }

  function mount(root) { el = root; }

  function render() {
    // 先把上次的偏好恢复出来，再画界面 —— 否则控件会显示默认值，
    // 和实际生效的筛选对不上（用户会以为设置没记住）。
    loadLibPrefs();
    el.innerHTML = `
      <div class="toolbar">
        <select id="lb-src" style="width:auto;min-width:170px"></select>
        <input type="text" id="lb-q" placeholder="搜歌名 / 作者 / 专辑（支持错别字与缩写）…"
               style="flex:1;min-width:220px">
        <label class="chip on" id="lb-fuzzy"
               title="模糊匹配：容忍错别字（青花词→青花瓷）、缩写（zjl→zhoujielun）与词序差异；关掉就是精确子串匹配">模糊</label>
        <select id="lb-sort" style="width:auto">
          <option value="path">按路径</option><option value="name">按文件名</option>
          <option value="title">按标题</option><option value="artist">按作者</option>
          <option value="album">按专辑</option><option value="duration">按时长</option>
          <option value="bitrate">按码率</option><option value="size">按体积</option>
          <option value="quality">按音质分</option><option value="mtime">按修改时间</option>
        </select>
        <button class="btn ghost sm" id="lb-desc" title="切换升/降序">↑ 升序</button>
        <span class="sep-v"></span>
        <label class="chip" id="lb-ll">无损</label>
        <select id="lb-br" style="width:auto" title="码率下限">
          <option value="">码率不限</option><option value="128000">≥128k</option>
          <option value="192000">≥192k</option><option value="256000">≥256k</option>
          <option value="320000">≥320k</option><option value="900000">≥900k</option>
        </select>
        <label class="chip" id="lb-cover">有封面</label>
        <button class="btn ghost sm" id="lb-reset">${U.icon('refresh')}<span>重置</span></button>
        <div class="grow"></div>
        <div class="seg" id="lb-mode">
          <button class="chip on" data-mode="list" title="一行一首">曲目</button>
          <button class="chip" data-mode="album"
            title="按专辑分组显示，像网页播放器一样：封面 + 专辑名 + 曲目，可整张播放">专辑</button>
        </div>
        <button class="btn ghost sm hidden" id="lb-more"
                title="按当前排序继续取下一批；已显示的行不会被重新渲染"></button>
        <span class="muted" id="lb-info"></span>
        <button class="btn ghost sm" id="lb-cols"
                title="选择显示哪些列；列太多横着看很累，选完会记住">
          ${U.icon('list')}<span>显示列</span></button>
        <button class="btn ghost sm" id="lb-queue-all"
                title="把当前列表（已加载的部分）整体加入播放列表；单个曲目请用行尾的 +">${U.icon('queue-add')}<span>全部入队</span></button>
        <button class="btn ghost sm" id="lb-export">${U.icon('download')}<span>导出 CSV</span></button>
      </div>
      <div class="grid c2" id="lb-stats" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr))"></div>
      <div class="row wrap hidden" id="lb-scope" style="gap:6px;margin:-4px 0 2px">
        <span class="muted" style="font-size:12px">目录范围：</span>
        <span class="tag mono ell" data-scope-path style="max-width:420px"></span>
        <button class="btn ghost sm" id="lb-scope-clear">清除范围</button>
      </div>
      <div class="tbl-wrap" id="lb-tbl"></div>`;

    const srcSel = el.querySelector('#lb-src');
    State.sources.forEach((s) => {
      const o = document.createElement('option');
      o.value = s.id; o.textContent = `${s.name}（${s.indexed || 0}）`;
      if (s.id === (State.activeSource || (State.sources[0] && State.sources[0].id))) o.selected = true;
      srcSel.appendChild(o);
    });
    if (!State.activeSource && State.sources.length) State.activeSource = State.sources[0].id;

    srcSel.onchange = () => {
      State.activeSource = srcSel.value;
      State.libFilters = {};
      pageSize = 300;
      saveLibPrefs();
      App.renderSidebar(); App.renderTree(); reload();
    };
    const qa = el.querySelector('#lb-queue-all');
    if (qa) qa.onclick = () => {
      const list = (data && data.tracks) || [];
      if (!list.length) return UI.toast('当前列表还没有曲目', 'warn', 3000);
      const n = App.queue.add(list, '曲库');
      UI.toast(n ? `已加入播放列表 ${n} 首` : '这些曲目都已在播放列表里',
        n ? 'ok' : 'info', 3600);
      if (App._paintPlayerModes) App._paintPlayerModes();
    };
    const q = el.querySelector('#lb-q');
    q.value = State.libFilters.q || '';
    q.oninput = U.debounce(() => { State.libFilters.q = q.value.trim(); pageSize = 300; saveLibPrefs(); reload(); }, 300);
    // 模糊开关：默认开。关掉就退回 SQL 的精确子串匹配（更快，但不认错别字）
    const fz = el.querySelector('#lb-fuzzy');
    if (State.libFilters.fuzzy === undefined) State.libFilters.fuzzy = true;
    fz.classList.toggle('on', !!State.libFilters.fuzzy);
    fz.onclick = () => {
      State.libFilters.fuzzy = !State.libFilters.fuzzy;
      fz.classList.toggle('on', !!State.libFilters.fuzzy);
      pageSize = 300;
      reload();
    };
    el.querySelector('#lb-sort').value = State.libFilters.sort || 'path';
    el.querySelector('#lb-sort').onchange = (e) => { State.libFilters.sort = e.target.value; saveLibPrefs(); reload(); };
    const descBtn = el.querySelector('#lb-desc');
    descBtn.onclick = () => {
      State.libFilters.order = (State.libFilters.order === 'desc') ? 'asc' : 'desc'; saveLibPrefs();
      descBtn.textContent = State.libFilters.order === 'desc' ? '↓ 降序' : '↑ 升序';
      reload();
    };
    if (State.libFilters.order === 'desc') descBtn.textContent = '↓ 降序';
    const ll = el.querySelector('#lb-ll');
    ll.classList.toggle('on', !!State.libFilters.lossless);
    ll.onclick = () => {
      State.libFilters.lossless = State.libFilters.lossless ? '' : '1'; saveLibPrefs();
      ll.classList.toggle('on', !!State.libFilters.lossless);
      reload();
    };
    el.querySelector('#lb-br').value = State.libFilters.min_bitrate || '';
    el.querySelector('#lb-br').onchange = (e) => { State.libFilters.min_bitrate = e.target.value; reload(); };
    const cv = el.querySelector('#lb-cover');
    cv.classList.toggle('on', !!State.libFilters.has_cover);
    cv.onclick = () => {
      State.libFilters.has_cover = State.libFilters.has_cover ? '' : '1'; saveLibPrefs();
      cv.classList.toggle('on', !!State.libFilters.has_cover);
      reload();
    };
    el.querySelector('#lb-reset').onclick = () => {
      State.libFilters = {}; el.querySelector('#lb-q').value = ''; pageSize = 300; render(); reload();
    };
    const scClear = el.querySelector('#lb-scope-clear');
    if (scClear) scClear.onclick = () => setScope('');
    // 刷新页面后范围还在（libFilters 不持久化），这里补一次显示
    setScopeChip();
    el.querySelector('#lb-export').onclick = exportCsv;
    el.querySelector('#lb-cols').onclick = columnDialog;
    el.querySelector('#lb-more').onclick = loadMore;
    // 曲目 / 专辑 两种显示方式（记住选择）
    el.querySelectorAll('#lb-mode .chip').forEach((c) => {
      c.onclick = () => {
        viewMode = c.dataset.mode;
        try { Prefs.set('libMode', viewMode); } catch (e) {}
        el.querySelectorAll('#lb-mode .chip')
          .forEach((x) => x.classList.toggle('on', x.dataset.mode === viewMode));
        paint();          // 重画表格 / 专辑网格（函数名是 paint）
      };
    });

    reload();
  }

  /* ============================================================
     列定义与显示偏好

     11 列全铺开太宽，横着看很累。做成可勾选：默认只留最常看的
     6 列，其余按需打开，选择记在 localStorage 里。
     `always` 的两列（封面、文件名）不允许关 —— 关掉就不知道在看什么了。
     ============================================================ */
  const COLUMNS = [
    { key: 'cover', label: '封面', always: true, cls: 'lb-c' },
    { key: 'name', label: '文件名', always: true, sortField: 'name' },
    { key: 'title', label: '标题', sortField: 'title' },
    { key: 'artist', label: '作者', sortField: 'artist' },
    { key: 'album', label: '专辑', sortField: 'album' },
    { key: 'codec', label: '格式' },
    { key: 'bitrate', label: '码率', num: true, sortField: 'bitrate' },
    { key: 'sample_rate', label: '采样率', num: true },
    { key: 'duration', label: '时长', num: true, sortField: 'duration' },
    { key: 'size', label: '体积', num: true, sortField: 'size' },
    { key: 'quality', label: '音质分', num: true, sortField: 'quality' },
    { key: 'mtime', label: '修改时间', num: true, sortField: 'mtime' },
    { key: 'path', label: '所在目录' },
  ];
  const DEFAULT_COLS = ['cover', 'name', 'artist', 'album', 'duration', 'size'];
  const COLS_KEY = 'msh.libCols';

  let colSet = null;                    // Set<string>，null = 还没加载

  function loadCols() {
    if (colSet) return colSet;
    let saved = null;
    try {
      const raw = (typeof Prefs !== 'undefined')
    ? JSON.stringify(Prefs.get('libCols', null)) : localStorage.getItem(COLS_KEY);
      if (raw) saved = JSON.parse(raw);
    } catch (e) { saved = null; }
    const valid = new Set(COLUMNS.map((c) => c.key));
    let list = Array.isArray(saved) ? saved.filter((k) => valid.has(k)) : DEFAULT_COLS.slice();
    // always 列必须在内
    COLUMNS.forEach((c) => { if (c.always && !list.includes(c.key)) list.push(c.key); });
    if (!list.length) list = DEFAULT_COLS.slice();
    colSet = new Set(list);
    return colSet;
  }

  function saveCols() {
    try {
      // 按 COLUMNS 顺序存，避免勾选顺序影响显示顺序
      const list = COLUMNS.filter((c) => colSet.has(c.key)).map((c) => c.key);
      // 选的就是默认那套时干脆不存：这样"默认"永远等于代码里的 DEFAULT_COLS，
      // 以后改了默认值，没自定义过的用户会跟着更新，而不是被旧快照钉住。
      const isDefault = list.length === DEFAULT_COLS.length
        && DEFAULT_COLS.every((k) => list.includes(k));
      if (isDefault) localStorage.removeItem(COLS_KEY);
      else localStorage.setItem(COLS_KEY, JSON.stringify(list));
    } catch (e) { /* ignore */ }
  }

  const visibleCols = () => COLUMNS.filter((c) => colSet.has(c.key));

  /** 单行的 HTML。抽出来是为了"加载更多"时能只渲染新增的行。 */
  function rowHtml(t, q) {
    const cell = {
      cover: `<td class="lb-c">${UI.coverHtml(t)}</td>`,
      name: `<td>
          <div class="ell" title="${U.esc(t.name)}">${U.hl(t.name, q)}</div>
          <div class="muted" style="font-size:11px" title="${U.esc(t.path)}">${U.esc(U.parent(t.path))}</div>
        </td>`,
      title: `<td class="ell" title="${U.esc(t.title || '')}">${U.hl(t.title || '-', q)}</td>`,
      artist: `<td class="ell" title="${U.esc(t.artist || '')}">${U.hl(t.artist || '-', q)}</td>`,
      album: `<td class="ell" title="${U.esc(t.album || '')}">${U.hl(t.album || '-', q)}</td>`,      codec: `<td><span class="tag ${t.is_lossless ? 'll' : ''}">${U.esc(t.codec || '-')}</span>
            ${t.meta_status === 'error' ? `<span class="tag hi" title="${U.esc(t.meta_error || '')}">解析异常</span>` : ''}</td>`,
      bitrate: `<td class="num">${t.bitrate ? Math.round(t.bitrate / 1000) + 'k' : '-'}</td>`,
      sample_rate: `<td class="num">${t.sample_rate ? (t.sample_rate / 1000).toFixed(t.sample_rate % 1000 ? 1 : 0) + 'k' : '-'}</td>`,
      duration: `<td class="num">${U.fmtDur(t.duration)}</td>`,
      size: `<td class="num">${U.fmtBytes(t.size)}</td>`,
      quality: `<td class="num" title="音质综合分">${(t.quality || 0).toFixed(1)}</td>`,
      mtime: `<td class="num">${t.mtime ? U.fmtDate(t.mtime) : '-'}</td>`,
      path: `<td class="ell muted" style="font-size:11.5px" title="${U.esc(t.path)}">${U.esc(U.parent(t.path))}</td>`,
    };
    const cols = visibleCols().map((c) => cell[c.key] || '<td></td>').join('');
    return `<tr data-id="${t.id}" data-src="${U.esc(t.source_id)}" data-bound="1"
        data-play-id="${t.id}">
        ${cols}<td class="nowrap lb-act">
          <button class="btn icon sm" data-play="${t.id}" title="从这里开始播（当前列表进队列）">${U.icon('play')}</button>
          <button class="btn icon sm ghost" data-qadd="${t.id}" title="加到队列末尾">${U.icon('queue-add')}</button>
          <button class="btn icon sm ghost" data-info="${t.id}" title="详情">${U.icon('info')}</button>
        </td></tr>`;
  }

  /**
   * 从某一首开始播：把**当前列表**（含已加载的后续分页）填进队列。
   *
   * 为什么不是只播这一首：曲库是分页的，只播一首的话"下一首"立刻到头，
   * 播放器就是个摆设。把列表整体入队，"下一首/上一首"才真的有内容。
   * 队列里存的是完整曲目对象，所以**翻页也不会把队列冲掉**。
   */
  function playFrom(t) {
    // ★ 点哪首放**哪首**。
    //
    //   以前这里把"当前列表"整批塞进队列，于是用户点了第 50 首，
    //   播放列表里出现它**前面的 49 首** —— 反馈里的"点一首歌，
    //   把这之前的音乐全加进播放列表"就是这个。
    //
    //   想整列表入队请用工具栏的「全部入队」按钮（或专辑视图的「播放」）。
    App.playTrack(t, { label: '曲库' });
  }

  /** 给一行绑上交互（点开详情、双击试听、行内按钮）。 */
  function bindRow(tr) {
    const box = el.querySelector('#lb-tbl');
    tr.onclick = (e) => {
      if (e.target.closest('button')) return;
      box.querySelectorAll('tr').forEach((r) => r.classList.remove('sel'));
      tr.classList.add('sel');
      detailId = Number(tr.dataset.id);
      openDetail();
    };
    tr.ondblclick = () => {
      const t = (data && data.tracks || []).find((x) => x.id === Number(tr.dataset.id));
      // 双击 = 从这首开始，把**当前列表**都放进队列。
      // 以前只播这一首，于是"下一首"按钮形同虚设（队列里就一首）。
      if (t) playFrom(t);
    };
    const pb = tr.querySelector('[data-play]');
    if (pb) pb.onclick = (e) => {
      e.stopPropagation();
      const t = (data && data.tracks || []).find((x) => x.id === Number(pb.dataset.play));
      if (t) playFrom(t);
    };
    const qb = tr.querySelector('[data-qadd]');
    if (qb) qb.onclick = (e) => {
      e.stopPropagation();
      const t = (data && data.tracks || []).find((x) => x.id === Number(qb.dataset.qadd));
      if (!t) return;
      const n = App.queue.add([t], '手动加入');
      UI.toast(n ? '已加到队列末尾' : '这首已经在队列里了',
        n ? 'ok' : 'info', 2600);
      if (App._paintPlayerModes) App._paintPlayerModes();
    };
    const ib = tr.querySelector('[data-info]');
    if (ib) ib.onclick = (e) => {
      e.stopPropagation();
      detailId = Number(ib.dataset.info);
      openDetail();
    };
  }

  /** 列显示设置面板。 */
  function columnDialog() {
    loadCols();
    const ctl = UI.modal({
      title: '显示哪些信息',
      size: 'sm',
      body: `<div class="muted" style="font-size:12px;margin-bottom:8px">
          默认只留最常看的几列；列太多横着看很累，按需打开就好。
          选择会记住，下次打开还是这样。</div>
        <div class="te-grid">
          ${COLUMNS.map((c) => `
            <label class="te-row ${c.always ? 'on' : ''}" style="cursor:pointer">
              <span class="te-pick"><input type="checkbox" data-col="${c.key}"
                ${colSet.has(c.key) ? 'checked' : ''} ${c.always ? 'disabled' : ''}></span>
              <span class="te-label" style="text-align:left;width:auto;flex:1">
                ${U.esc(c.label)}</span>
              ${c.always ? '<span class="muted" style="font-size:11px">固定</span>' : ''}
            </label>`).join('')}
        </div>`,
      footer: `<button class="btn ghost" id="col-reset">恢复默认</button>
               <div class="grow"></div>
               <button class="btn primary" id="col-ok">完成</button>`,
    });
    const apply = () => {
      const list = [];
      ctl.body.querySelectorAll('[data-col]').forEach((cb) => {
        if (cb.checked) list.push(cb.dataset.col);
      });
      COLUMNS.forEach((c) => { if (c.always && !list.includes(c.key)) list.push(c.key); });
      colSet = new Set(list);
      saveCols();
      paint();
    };
    ctl.body.querySelectorAll('[data-col]').forEach((cb) => {
      cb.onchange = apply;
    });
    ctl.footer.querySelector('#col-reset').onclick = () => {
      try { Prefs.del('libCols'); } catch (e) { /* ignore */ }
      colSet = null;
      loadCols();
      // 必须把新状态**刷回复选框**：apply() 是从 DOM 的 checked 读值的，
      // 光改内存里的 colSet，复选框还是旧状态，apply() 就会把旧值又存回去
      //（实测：点"恢复默认"后表格看着对了，localStorage 里却还是自定义那套）。
      ctl.body.querySelectorAll('[data-col]').forEach((cb) => {
        cb.checked = colSet.has(cb.dataset.col) || cb.disabled;
      });
      apply();
    };
    ctl.footer.querySelector('#col-ok').onclick = () => ctl.close();
  }

  /** 把当前筛选条件拼成请求参数（`offset` 用于"加载更多"）。 */
  function buildParams(offset) {
    const f = State.libFilters;
    const params = {
      q: f.q || '', prefix: f.prefix || '', sort: f.sort || 'path', order: f.order || 'asc',
      limit: pageSize, offset: offset || 0,
    };
    // 有关键词且开着模糊时走后端的模糊打分（容忍错别字/缩写/词序）
    if (f.fuzzy !== false && (f.q || '').trim()) params.fuzzy = 1;
    if (f.lossless) params.lossless = true;
    if (f.has_cover) params.has_cover = true;
    if (f.min_bitrate) params.min_bitrate = f.min_bitrate;
    if (f.codec) params.codec = f.codec;
    return params;
  }

  /** 整表重载（条件变了、换源了、重置了都走这里）。 */
  async function reload() {
    const sid = State.activeSource;
    el = el || document.getElementById('view-library');
    if (!sid) {
      el.querySelector('#lb-tbl').innerHTML =
        `<div class="empty"><div class="big">${U.icon('music', 'ico')}</div><div>先添加并扫描一个数据源</div></div>`;
      return;
    }
    // ★ 上一次查询还没回来时**不能直接丢掉**这次请求。
    //   踩过：搜索框是 300ms 防抖，慢源上连续改两个筛选条件时，
    //   第二次 `reload()` 会被 `if (loading) return` 静默吃掉 ——
    //   页面停在上一组筛选结果，用户以为筛选坏了。
    //   现在记一个 pending 标记，当前请求结束后自动再跑一次。
    if (loading) { pendingReload = true; return; }
    loading = true;
    try {
      data = await API.library(sid, buildParams(0));
      State.tracks = data.tracks;
      State.playlist = data.tracks.map((t) => t.id);
      paint();
    } catch (e) {
      el.querySelector('#lb-tbl').innerHTML = `<div class="pad" style="color:#ff9daa">${U.esc(e.message)}</div>`;
    } finally {
      loading = false;
      // 期间有被丢掉的请求 → 补跑一次，用户看到的就是最后一次筛选的结果
      if (pendingReload) {
        pendingReload = false;
        setTimeout(() => { reload().catch(() => {}); }, 0);
      }
    }
  }

  /**
   * 追加下一页。
   *
   * 以前「加载更多」是 `pageSize += 500` 再整表重载：300 → 800 → 1300 条
   * 全部重新拉取、重新渲染，越点越慢，滚动位置也会跳掉。
   * 现在按 offset 只取新增的那一页，只追加新行，
   * 已渲染的行一个都不动。
   */
  async function loadMore() {
    if (!data || loading) return;
    if (data.returned >= data.total) return;
    const sid = State.activeSource;
    const btn = el.querySelector('#lb-more');
    const wasCount = data.tracks.length;
    loading = true;
    if (btn) { btn.disabled = true; btn.textContent = '加载中…'; }
    try {
      const next = await API.library(sid, buildParams(wasCount));
      const add = next.tracks || [];
      if (!add.length) {
        data.returned = data.total;        // 后端说没有了，收起按钮
      } else {
        data.tracks = data.tracks.concat(add);
        data.returned = data.tracks.length;
        // 统计卡仍用第一次那批的聚合值（同一套筛选条件，数值本来就一样）
        // 专辑视图没有"追加行"这回事（分组会变），整块重画更简单也更正确
        if (viewMode === 'album') paint();
        else appendRows(add);
        State.tracks = data.tracks;
        State.playlist = data.tracks.map((t) => t.id);
      }
      updateCount();
    } catch (e) {
      UI.toast('加载更多失败：' + U.esc(e.message), 'err', 7000);
    } finally {
      loading = false;
      if (btn) { btn.disabled = false; btn.textContent = '加载更多'; }
    }
  }

  /** 只把新增的行插进表格，不动已有行。 */
  function appendRows(add) {
    const box = el.querySelector('#lb-tbl');
    const tbody = box.querySelector('tbody');
    if (!tbody) return;
    const q = State.libFilters.q || '';
    tbody.insertAdjacentHTML('beforeend', add.map((t) => rowHtml(t, q)).join(''));
    // 新插入的行要单独绑事件（之前整表 innerHTML 重建时是统一绑的）
    tbody.querySelectorAll('tr[data-id]:not([data-bound])').forEach((tr) => {
      tr.dataset.bound = '1';
      bindRow(tr);
    });
  }

  /** 更新"已显示 / 总数"与加载更多按钮的状态。 */
  function updateCount() {
    if (!data) return;
    const info = el.querySelector('#lb-info');
    if (info) {
      info.textContent = `${data.tracks.length} / ${data.total} 条`
        + (State.libFilters.prefix ? ` · 目录 ${State.libFilters.prefix}` : '');
    }
    const more = el.querySelector('#lb-more');
    if (!more) return;
    const remain = Math.max(0, (data.total || 0) - (data.returned || 0));
    more.classList.toggle('hidden', remain <= 0);
    if (remain > 0) {
      more.textContent = `加载更多（还有 ${remain} 条）`;
    }
  }

  function paint() {
    if (!data) return;
    const agg = data.aggregate || {};
    const all = data.aggregate_all || {};
    const scope = data.scope || 'all';
    // 这些数字到底在统计"什么范围"，必须写在卡片上：
    // 选了目录就是该目录（含子目录），搜了关键词就是搜索结果，
    // 否则用户会以为体积/码率这些总是不变、像是坏了。
    const scopeNote = scope === 'folder' ? '当前目录'
      : scope === 'filtered' ? '当前筛选结果' : '整个数据源';
    const scopeSub = scope === 'all' ? ''
      : ` · ${scopeNote}（整库 ${all.count || 0} 首 / ${U.fmtBytes(all.bytes || 0)}）`;
    // 无损曲目常常是 VBR/均值偏低，光看"平均码率"容易觉得数字不对，
    // 所以把无损占比一起标出来，让这个平均值有解释。
    const losslessShare = agg.count ? Math.round((agg.lossless || 0) / agg.count * 100) : 0;
    el.querySelector('#lb-stats').innerHTML =
      UI.stat('曲目总数', agg.count != null ? agg.count : data.total, scopeSub) +
      UI.stat('当前结果', data.returned + (data.total > data.returned ? ` / ${data.total}` : '')) +
      UI.stat('总体积', U.fmtBytes(agg.bytes), scopeNote) +
      UI.stat('平均码率', U.fmtBitrate(agg.avg_bitrate), `无损占 ${losslessShare}%`) +
      UI.stat('无损曲目', `${agg.lossless || 0}`, `占 ${losslessShare}%`) +
      UI.stat('总时长', U.fmtDurLong(agg.total_duration), scopeNote);
    updateCount();

    const box = el.querySelector('#lb-tbl');
    if (!data.tracks.length) {
      box.innerHTML = `<div class="empty"><div class="big">${U.icon('search', 'ico')}</div><div>未匹配到的曲目</div>
        <div class="muted">如果是空库，请回到「数据源」页面点扫描</div></div>`;
      return;
    }
    // 专辑视图：像网页播放器那样按专辑分组（封面 + 专辑名 + 曲目列表 + 整张播放）
    if (viewMode === 'album') { paintAlbums(box); return; }
    loadCols();
    const q = State.libFilters.q || '';
    const cols = visibleCols();
    const head = cols.map((c) => {
      // 能排序的列加 sortable，点表头就按这个字段排
      const cls = c.num ? 'num' : (c.key === 'cover' ? 'lb-c' : '');
      const sort = c.sortField ? 'sortable' : '';
      const on = (State.libFilters.sort || 'path') === (c.sortField || '')
        || (c.sortField === 'name' && (State.libFilters.sort || 'path') === 'path');
      return `<th class="${cls} ${sort} ${on ? 'sorted' : ''}"
        ${c.sortField ? `data-sortfield="${c.sortField}"` : ''}
        style="cursor:${c.sortField ? 'pointer' : 'default'}">${U.esc(c.label)}</th>`;
    }).join('');
    box.innerHTML = `<table class="data lb-table">
      <thead><tr>${head}<th class="lb-act"></th></tr></thead>
      <tbody>${data.tracks.map((t) => rowHtml(t, q)).join('')}</tbody></table>`;

    box.querySelectorAll('tr[data-id]').forEach(bindRow);
    box.querySelectorAll('th[data-sortfield]').forEach((th) => {
      th.onclick = () => {
        const f = th.dataset.sortfield;
        if (State.libFilters.sort === f) {
          State.libFilters.order = State.libFilters.order === 'desc' ? 'asc' : 'desc';
        } else {
          State.libFilters.sort = f;
          State.libFilters.order = 'asc';
        }
        const sel = el.querySelector('#lb-sort');
        if (sel && [...sel.options].some((o) => o.value === State.libFilters.sort)) {
          sel.value = State.libFilters.sort;
        }
        const d = el.querySelector('#lb-desc');
        if (d) d.textContent = State.libFilters.order === 'desc' ? '↓ 降序' : '↑ 升序';
        reload();
      };
    });
  }

  // ------------------------------------------------------------ 专辑视图

  /**
   * 按专辑分组展示（"更像网页播放器"）。
   *
   * 分组的依据是 `album` 标签；没有专辑标签的按所在目录归组 ——
   * 很多本地曲库是"一个文件夹一张专辑"但没写标签，
   * 只按标签分组会让它们全挤进"未知专辑"。
   */
  function groupByAlbum(tracks) {
    const map = new Map();
    tracks.forEach((t) => {
      const album = (t.album || '').trim();
      const folder = U.parent(t.path) || '';
      const key = album || (folder ? '（未标专辑）' + folder : '（未标专辑）');
      const label = album || (folder.split('/').pop() || '未知专辑');
      if (!map.has(key)) {
        map.set(key, { key, label, album, folder, tracks: [], bytes: 0 });
      }
      const g = map.get(key);
      g.tracks.push(t);
      g.bytes += (t.size || 0);
    });
    // 曲目多的排前面：大专辑更可能是用户想找的
    return Array.from(map.values()).sort((a, b) => b.tracks.length - a.tracks.length);
  }

  function paintAlbums(box) {
    const q = State.libFilters.q || '';
    const groups = groupByAlbum(data.tracks);
    box.innerHTML = `<div class="alb-grid">${groups.map((g, gi) => {
      // 封面取第一首有封面的
      const withCover = g.tracks.find((t) => t.cover_url || t.has_cover);
      const cover = withCover
        ? (withCover.cover_url || `/api/cover/${withCover.id}?size=180`)
        : null;
      const artist = g.tracks[0].artist || '';
      const lossless = g.tracks.filter((t) => (t.quality || 0) >= 0.9).length;
      return `<section class="alb-card" data-album="${U.esc(g.key)}">
        <header class="alb-hd">
          <div class="alb-cover">${cover
            ? `<img src="${cover}" alt="" loading="lazy">`
            : U.icon('music', 'ico lg')}</div>
          <div class="alb-meta">
            <div class="alb-title" title="${U.esc(g.album || g.folder)}">${
              U.hl(g.album || g.label, q)}</div>
            <div class="alb-sub">${U.esc(artist || '未知作者')}${
              g.album && g.folder ? ` · ${U.esc(g.folder.split('/').pop())}` : ''}</div>
            <div class="alb-sub muted">${g.tracks.length} 首 · ${U.fmtBytes(g.bytes)}${
              lossless ? ` · 无损 ${lossless}` : ''}</div>
          </div>
          <div class="alb-act">
            <button class="btn sm primary" data-albplay="${gi}"
              title="整张专辑进队列，从第一首开始">${U.icon('play')}<span>播放</span></button>
            <button class="btn sm ghost" data-albq="${gi}"
              title="只加到队列末尾，不打断当前播放">${U.icon('queue-add')}</button>
            <button class="btn sm ghost" data-albtoggle="${gi}"
              title="展开 / 收起曲目">曲目</button>
          </div>
        </header>
        <div class="alb-tracks hidden" data-albtracks="${gi}">
          ${g.tracks.map((t, ti) => `
            <div class="alb-tr ${Number(t.id) === Number(State.curTrackId) ? 'playing' : ''}"
                 data-albtrack="${gi}:${ti}" data-play-id="${t.id}">
              <span class="alb-n">${ti + 1}</span>
              <span class="ell" title="${U.esc(t.name)}">${
                U.hl(t.title || t.name, q)}</span>
              ${(t.quality || 0) >= 0.9 ? '<span class="tag hi">无损</span>' : ''}
              <span class="muted" style="font-size:11.5px">${U.fmtDur(t.duration)}</span>
              <button class="btn icon sm ghost" data-albone="${gi}:${ti}"
                title="只播这一首">${U.icon('play')}</button>
            </div>`).join('')}
        </div>
      </section>`;
    }).join('')}</div>`;

    const playAlb = (gi, fromStart = true, only = -1) => {
      const g = groups[gi];
      if (!g) return;
      if (only >= 0) { playFrom(g.tracks[only]); return; }
      App.playList(g.tracks, g.tracks[0].id, '专辑');
      UI.toast(`已播放「${g.album || g.label}」共 ${g.tracks.length} 首`, 'ok', 3600);
    };
    box.querySelectorAll('[data-albplay]').forEach((b) => {
      b.onclick = (e) => { e.stopPropagation(); playAlb(Number(b.dataset.albplay)); };
    });
    box.querySelectorAll('[data-albq]').forEach((b) => {
      b.onclick = (e) => {
        e.stopPropagation();
        const g = groups[Number(b.dataset.albq)];
        const n = App.queue.add(g.tracks, '专辑');
        UI.toast(n ? `已把 ${n} 首加到队列末尾` : '这些曲目已经在队列里了',
          n ? 'ok' : 'info', 3200);
        if (App._paintPlayerModes) App._paintPlayerModes();
      };
    });
    box.querySelectorAll('[data-albtoggle]').forEach((b) => {
      b.onclick = (e) => {
        e.stopPropagation();
        const t = box.querySelector(`[data-albtracks="${b.dataset.albtoggle}"]`);
        if (t) t.classList.toggle('hidden');
      };
    });
    box.querySelectorAll('[data-albone]').forEach((b) => {
      b.onclick = (e) => {
        e.stopPropagation();
        const [gi, ti] = b.dataset.albone.split(':').map(Number);
        playAlb(gi, true, ti);
      };
    });
    box.querySelectorAll('[data-albtrack]').forEach((el2) => {
      el2.onclick = (e) => {
        if (e.target.closest('button')) return;
        const [gi, ti] = el2.dataset.albtrack.split(':').map(Number);
        const g = groups[gi];
        if (!g) return;
        // 点某一首：把整张专辑入队并从这首开始 —— 这样"下一首"还有得放
        App.playList(g.tracks, g.tracks[ti].id, '专辑');
      };
    });
  }

  // ------------------------------------------------------------ 目录范围

  /**
   * 选一个目录，把曲库范围限定到它（含子目录）。
   *
   * 侧栏折叠后，这是"按目录浏览"的替代入口 —— 以前这个能力只存在于
   * 侧栏目录树里，一收起就等于功能没了（汉堡键"联动性差"的根因之一）。
   */
  async function pickFolder() {
    const sid = State.activeSource;
    if (!sid) return UI.toast('请先选择一个数据源', 'warn');
    const start = State.libFilters.prefix || '';
    const p = await FolderPicker.open({ source: sid, start, title: '限定曲库范围到这个目录' });
    if (p === null || p === undefined) return;
    setScope(p);
  }

  // ------------------------------------------------------------ 偏好记忆

  /**
   * 把曲库的搜索词 / 排序 / 筛选 / 每批条数记下来。
   *
   * 以前这些都只在内存里，刷新一次就全没了 —— 用户每次都得重设一遍。
   * 记的是**界面偏好**，不涉及任何业务数据。
   */
  function saveLibPrefs() {
    try {
      Prefs.set('libFilters', {
        q: State.libFilters.q || '',
        sort: State.libFilters.sort || 'path',
        order: State.libFilters.order || 'asc',
        fuzzy: State.libFilters.fuzzy !== false,
        lossless: State.libFilters.lossless || '',
        has_cover: State.libFilters.has_cover || '',
        min_bitrate: State.libFilters.min_bitrate || '',
        codec: State.libFilters.codec || '',
        prefix: State.libFilters.prefix || '',
      });
      Prefs.set('libPageSize', pageSize);
    } catch (e) { /* 记不住不影响使用 */ }
  }

  /** 恢复上次的曲库偏好（首次使用就是默认值）。 */
  function loadLibPrefs() {
    try {
      const f = Prefs.get('libFilters', null);
      if (f && typeof f === 'object') {
        State.libFilters = Object.assign({}, State.libFilters, f);
      }
      const ps = Prefs.get('libPageSize', 0);
      if (ps && ps > 0) pageSize = ps;
    } catch (e) { /* 用默认值 */ }
  }

  /** 只刷新"目录范围"提示条（不重载数据）。 */
  function setScopeChip() {
    const chip = el.querySelector('#lb-scope');
    if (!chip) return;
    const p = State.libFilters.prefix || '';
    chip.classList.toggle('hidden', !p);
    const t = chip.querySelector('[data-scope-path]');
    if (t) t.textContent = p;
    chip.title = p;
  }

  /** 设定曲库的目录范围（空 = 整个数据源）。 */
  function setScope(prefix) {
    State.libFilters.prefix = prefix || '';
    pageSize = 300;
    // 范围是个"隐藏筛选"，得让用户看得见，否则会以为曲库变少了
    setScopeChip();
    reload();
  }

  // ------------------------------------------------------------ 详情
  async function openDetail() {
    if (!detailId) return;
    let t;
    try { t = await API.track(detailId); } catch (e) { return UI.toast(e.message, 'err'); }
    const s = App.sourceById(t.source_id);
    const body = `
      <div class="row" style="align-items:flex-start;gap:16px">
        <div style="flex:0 0 auto">
          ${t.has_cover
            ? `<img class="cover lg" src="/api/cover/${t.id}?size=420" alt="">`
            : `<div class="cover lg cover ph">${U.icon('music', 'ico huge')}</div>`}
          <button class="btn sm block" style="margin-top:8px" id="dt-play">${U.icon('play')}<span>播放</span></button>
      <button class="btn sm ghost block" style="margin-top:6px" id="dt-qadd"
        title="只把这一首加到队列末尾">${U.icon('queue-add')}<span>加入队列</span></button>
        </div>
        <div style="flex:1;min-width:0">
          <h3 style="margin:0 0 4px">${U.esc(t.title || t.name)}</h3>
          <div class="muted" style="margin-bottom:10px">${U.esc(t.artist || '未知作者')}${t.album ? ' · ' + U.esc(t.album) : ''}</div>
          <div class="kv">
            <div class="k">数据源</div><div>${U.esc(s ? s.name : t.source_id)} <span class="badge">${U.esc(s ? s.type : '')}</span></div>
            <div class="k">文件名</div><div class="mono">${U.esc(t.name)}</div>
            <div class="k">完整路径</div><div class="mono" style="word-break:break-all">${U.esc(t.path)}</div>
            <div class="k">格式</div><div>${U.esc(t.codec || '-')} ${t.is_lossless ? '<span class="tag ll">无损</span>' : '<span class="tag">有损</span>'}</div>
            <div class="k">码率</div><div>${U.fmtBitrate(t.bitrate)}</div>
            <div class="k">采样率 / 声道</div><div>${U.fmtRate(t.sample_rate)} / ${t.channels || '-'} 声道 ${t.bits_per_sample ? '· ' + t.bits_per_sample + 'bit' : ''}</div>
            <div class="k">时长</div><div>${U.fmtDur(t.duration)}（${t.duration ? t.duration.toFixed(2) + 's' : '-'}）</div>
            <div class="k">体积</div><div>${U.fmtBytes(t.size)}</div>
            <div class="k">音质分</div><div>${(t.quality || 0).toFixed(2)}</div>
            <div class="k">音轨号</div><div>${U.esc(t.track_no || '-')}</div>
            <div class="k">年份 / 流派</div><div>${U.esc(t.year || '-')} / ${U.esc(t.genre || '-')}</div>
            <div class="k">修改时间</div><div>${U.fmtDate(t.mtime)}</div>
            <div class="k">索引时间</div><div>${U.fmtDate(t.scanned_at)}</div>
            <div class="k">抽样哈希</div><div class="mono" style="font-size:11px;word-break:break-all">${U.esc(t.hash_quick || '未计算')}</div>
            ${t.meta_error ? `<div class="k">解析提示</div><div style="color:#ffd982">${U.esc(t.meta_error)}</div>` : ''}
          </div>
        </div>
      </div>`;
    const ctl = UI.modal({
      title: '曲目详情',
      size: 'lg',
      body,
      footer: `<span class="muted grow">支持 Range 流式读取，可直接拖动播放进度</span>
               <button class="btn ghost" data-act="copy">复制路径</button>
               <button class="btn ghost" data-act="refresh">重新解析标签</button>
               <button class="btn" data-act="tags">编辑标签</button>
               <button class="btn primary" data-act="files"
                 title="切到「文件浏览」并定位到这个文件，可在那里移动 / 集合 / 加入候选区">
                 ${U.icon('folder')}<span>在文件浏览中打开</span></button>
               <button class="btn ghost" data-act="close">关闭</button>`,
    });
    ctl.body.querySelector('#dt-play').onclick = () => playFrom(t);
    const qadd = ctl.body.querySelector('#dt-qadd');
    if (qadd) qadd.onclick = () => {
      const n = App.queue.add([t], '手动加入');
      UI.toast(n ? '已加到队列末尾' : '这首已经在队列里了', n ? 'ok' : 'info', 2600);
      if (App._paintPlayerModes) App._paintPlayerModes();
    };
    ctl.footer.querySelector('[data-act=close]').onclick = () => ctl.close();
    ctl.footer.querySelector('[data-act=tags]').onclick = () => {
      ctl.close();
      TagEditor.open({ source: t.source_id, paths: [t.path] });
    };
    // 跳到文件浏览并定位。文件浏览那边会选中该行并滚动到可见位置，
    // 接着就能直接用它的移动/集合/候选区/编辑标签等操作。
    ctl.footer.querySelector('[data-act=files]').onclick = async () => {
      ctl.close();
      App.setView('files');
      // 等视图 mount 完再去定位（setView 是同步切换、视图内部异步加载）
      await new Promise((r) => setTimeout(r, 60));
      const fv = App.viewApi ? App.viewApi('files') : null;
      const fn = fv && fv.revealFile;
      if (typeof fn !== 'function') {
        return UI.toast('文件浏览尚未就绪，请手动切到该页面', 'warn');
      }
      const ok = await fn(t.source_id, t.path);
      if (!ok) {
        UI.toast('已切到所在目录，但没找到这个文件（可能刚被移动或还没扫描）',
          'warn', 7000);
      }
    };
    ctl.footer.querySelector('[data-act=copy]').onclick = () => {
      navigator.clipboard.writeText(t.path).then(() => UI.toast('路径已复制', 'ok'));
    };
    ctl.footer.querySelector('[data-act=refresh]').onclick = async () => {
      try {
        await API.refreshTrack(t.id);
        UI.toast('标签已重新解析', 'ok');
        ctl.close();
        reload();
      } catch (e) { UI.toast('解析失败：' + U.esc(e.message), 'err'); }
    };
  }

  // ------------------------------------------------------------ 导出
  function exportCsv() {
    if (!data || !data.tracks.length) return UI.toast('没有可导出的数据', 'warn');
    const cols = ['name', 'title', 'artist', 'album', 'codec', 'bitrate', 'sample_rate',
      'duration', 'size', 'quality', 'path'];
    const head = cols.join(',');
    const lines = data.tracks.map((t) => cols.map((c) => {
      const v = t[c] === null || t[c] === undefined ? '' : String(t[c]);
      return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
    }).join(','));
    const blob = new Blob(['\ufeff' + head + '\n' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `library-${State.activeSource}.csv`;
    a.click();
    UI.toast(`已导出 ${data.tracks.length} 条`, 'ok');
  }

  App.registerView('library', { mount, render, reload, pickFolder, setScope });
})();
