/* ============================================================
   视图：文件浏览（像资源管理器一样按真实目录看文件）

   为什么单独做一个页面：曲库浏览是按"歌"看的，而整理音乐时人是按"文件夹"
   思考的——哪个文件夹塞了几百首、哪首放错了地方。所以这里：
     · 左侧目录树，每个文件夹标出曲目数（含子目录），可按数量排序
     · 右侧列表带封面与基本信息，多选后可"移动"到别的目录
     · 支持按同一作者 / 专辑 / 格式快速集合，批量处理
   ============================================================ */
(() => {
  let el = null;
  let cur = '';                 // 当前目录（绝对内部路径）
  let listData = null;          // /api/files/list 的结果
  let folderData = null;        // /api/files/folders 的结果
  let checked = new Set();      // 已勾选的 did（源::路径）
  let sortKey = 'name';
  let sortOrder = 'asc';
  // 排序偏好记下来：文件浏览是高频页面，每次回来都要重排很烦
  (() => {
    try {
      const s = Prefs.get('filesSort', null);
      if (s && s.key) { sortKey = s.key; sortOrder = s.order || 'asc'; }
    } catch (e) { /* 用默认 */ }
  })();

  function saveFilesSort() {
    try { Prefs.set('filesSort', { key: sortKey, order: sortOrder }); } catch (e) {}
  }
  let expanded = new Set();     // 展开的目录（rel 路径）
  let treePicked = new Set();   // 目录树里多选中的目录（rel 路径）
  let treeFiles = [];           // 树勾选联动出来的文件（当前"选中结果"视图）
  // （原来这里还有个 `treeFileCount`，5 处赋值、0 处读取 —— 死变量，已删）
  let treeViewOn = false;       // 文件列表是否正在显示"目录树选中结果"
  // 用户在树结果里**手动取消**的文件（did）。
  //   没有它的话：勾了 AlbumA → 取消其中一首 → 再勾 AlbumB，
  //   syncTreeToFiles 会重新"全部勾上"，用户刚取消的那首又回来了。
  let treeExcluded = new Set();
  let treeLoading = false;

  function mount(root) { el = root; }

  const sourceId = () => State.activeSource
    || (State.sources[0] && State.sources[0].id) || '';
  const did = (p) => `${sourceId()}::${p}`;

  function render() {
    const srcs = State.sources || [];
    if (!srcs.length) {
      el.innerHTML = `<div class="empty"><div class="big">${U.icon('folder', 'ico')}</div>
        <div>先添加数据源并扫描</div></div>`;
      return;
    }
    if (!State.activeSource) State.activeSource = srcs[0].id;
    el.innerHTML = `
      <div class="toolbar">
        <b>文件浏览</b>
        <select id="fb-src" style="width:auto;min-width:190px">
          ${srcs.map((s) => `<option value="${s.id}" ${State.activeSource === s.id ? 'selected' : ''}>
            ${U.esc(s.name)}（${s.indexed || 0}）</option>`).join('')}
        </select>
        <button class="btn sm" id="fb-up" title="上级目录">${U.icon('prev')}<span>上级</span></button>
        <button class="btn sm" id="fb-refresh">${U.icon('refresh')}<span>刷新</span></button>
        <span class="sep-v"></span>
        <button class="btn sm" id="fb-newdir" data-exclusive
                title="新建目录">${U.icon('plus')}<span>新建目录</span></button>
        <button class="btn sm" id="fb-dirop" data-exclusive
                title="当前目录操作">${U.icon('folder')}<span>当前目录操作</span></button>
        <button class="btn sm" id="fb-organize" data-exclusive
                title="按专辑把散落的歌归类：先选范围，再看方案，最后执行">
          ${U.icon('settings')}<span>智能整理</span></button>
        <button class="btn sm" id="fb-analyze"
                title="搜一首歌，看看它该放在库里的哪个文件夹">
          ${U.icon('search')}<span>查找并分析去向</span></button>
        <button class="btn sm" id="fb-upload" data-exclusive
                title="上传散装文件，按现有音乐库结构自动分配文件夹">
          ${U.icon('upload')}<span>上传入库</span></button>
        <button class="btn sm" id="fb-dup"
                title="扫描重复曲目：内容相同或高度相似的分成一组，可勾选要保留的">
          ${U.icon('compare')}<span>查重</span></button>
        <button class="btn sm ghost" id="fb-recycle" data-exclusive
                title="查看回收站：可以还原回原位置，或彻底删除">
          ${U.icon('trash')}<span>回收站</span></button>
        <span class="sep-v"></span>
        <div class="fb-search">
          <input type="text" id="fb-q" placeholder="🔍 搜文件 / 文件夹（支持错别字、拼音缩写）…">
          <button class="btn sm ghost hidden" id="fb-qclear">清除</button>
        </div>
        <span class="sep-v"></span>
        <label class="inline">文件夹排序
          <select id="fb-dsort" style="width:auto">
            <option value="count" selected>按文件数</option>
            <option value="name">按名称</option>
            <option value="mtime">按时间</option>
          </select></label>
        <div class="grow"></div>
        <span class="disk-chip hidden" id="fb-disk"></span>
        <span class="muted" id="fb-info"></span>
      </div>
      <div class="fb-breadcrumb" id="fb-crumb"></div>
      <div class="fb-split">
        <aside class="fb-tree-wrap">
          <aside class="fb-tree" id="fb-tree"></aside>
          <div class="fb-tree-bar hidden" id="fb-tree-bar">
            <span class="muted" id="fb-tree-bar-info"></span>
            <div class="grow"></div>
            <button class="btn sm ghost" id="fb-tb-none"
              title="清空目录勾选，回到普通文件列表">清除</button>
            <button class="btn sm ghost" id="fb-tb-tofiles"
              title="把选中目录里的文件加入候选区，之后可统一定去向">加入候选区</button>
            <button class="btn sm" id="fb-tb-organize"
              title="只对选中的这些目录生成整理方案">整理这些</button>
          </div>
        </aside>
        <div class="fb-main">
          <div class="fb-selbar" id="fb-selbar"></div>
          <div class="tbl-wrap fb-list" id="fb-list"></div>
        </div>
      </div>
      <div id="fb-stage"></div>`;

    // 候选区（跨目录暂存）：挂载一次，之后由 Staging 自己重绘
    if (typeof Staging !== 'undefined') {
      Staging.attach(el.querySelector('#fb-stage'));
      Staging.onChange(() => paintStagedMarks());
    }
    // 候选区里的「执行移动」按钮由本视图实现（它知道怎么调 Executor）
    document.addEventListener('staging:move', () => moveStaged());

    el.querySelector('#fb-src').onchange = (e) => {
      State.activeSource = e.target.value;
      // 换源时必须把缓存清干净：留着上一个源的 folderData/listData，
      // 重绘时就会把别的源的目录树/列表画出来（"串台"的根因之一）。
      folderData = null;
      listData = null;
      cur = ''; checked.clear(); expanded.clear();
      if (App.renderSidebar) App.renderSidebar();
      load();
    };
    el.querySelector('#fb-up').onclick = () => { if (listData && listData.parent !== undefined) go(listData.parent || ''); };
    el.querySelector('#fb-refresh').onclick = () => load();
    el.querySelector('#fb-newdir').onclick = () => createFolder(cur);
    el.querySelector('#fb-dirop').onclick = (e) => {
      const r = e.currentTarget.getBoundingClientRect();
      const items = currentDirMenuItems();
      if (items.length) UI.contextMenu(r.left, r.bottom + 4, items);
    };
    el.querySelector('#fb-organize').onclick = () => Organizer.albumWizard({ source: sourceId() });
    el.querySelector('#fb-analyze').onclick = () => Organizer.analyze(sourceId());
    el.querySelector('#fb-upload').onclick = () => Uploader.open({ source: sourceId() });
    // 查重就在文件浏览里直接开：发现重复后往往要回到文件列表去处理，
    // 让用户先跳到「查重」标签页再切回来是多余的往返。
    const _tb = el.querySelector('#fb-recycle');
    if (_tb) _tb.onclick = () => openTrash();
    el.querySelector('#fb-dup').onclick = () => openDedup();
    el.querySelector('#fb-tb-none').onclick = () => {
      treePicked.clear();
      paintTree();
      syncTreeToFiles();      // 清空勾选 → 退出"选中结果"视图并恢复目录列表
    };
    // 把选中目录里的文件加进候选区（跨目录挑歌的快捷入口）
    el.querySelector('#fb-tb-tofiles').onclick = async () => {
      if (!treeFiles.length) return UI.toast('所选目录里没有文件', 'warn');
      const n = Staging.add(treeFiles.map((t) => ({
        did: did(t.path), path: t.path, name: t.name,
        title: t.title, artist: t.artist, album: t.album,
        size: t.size, id: t.id, cover_url: t.cover_url,
        srcFolder: U.parent(t.path),
      })));
      UI.toast(`已加入候选区 ${n} 个文件`, 'ok', 5000);
    };
    // 用选中的目录作为整理范围：把它们的**父级**之外的目录都忽略掉。
    // 直接在向导里预勾"忽略范围"比让用户自己去高级设置里一个个找快得多。
    el.querySelector('#fb-tb-organize').onclick = () => {
      const picked = Array.from(treePicked);
      if (!picked.length) return;
      Organizer.albumWizard({ source: sourceId(), scopes: picked });
    };
    mountDropImport();
    el.querySelector('#fb-dsort').onchange = () => paintTree();
    // 搜索框：防抖后调后端模糊搜索（文件和文件夹一起）
    const qBox = el.querySelector('#fb-q');
    let qt = null;
    qBox.oninput = () => {
      clearTimeout(qt);
      const v = qBox.value.trim();
      qt = setTimeout(() => runSearch(v), 260);
    };
    qBox.onkeydown = (e) => {
      if (e.key === 'Escape') { qBox.value = ''; runSearch(''); }
      if (e.key === 'Enter') { clearTimeout(qt); runSearch(qBox.value.trim()); }
    };
    el.querySelector('#fb-qclear').onclick = () => {
      qBox.value = '';
      runSearch('');
      qBox.focus();
    };
    // 只有"缓存数据确实属于当前源"时才复用，否则重新加载
    const cachedOk = folderData && folderData.source === sourceId()
      && listData && listData.source === sourceId();
    if (!cachedOk) { folderData = null; listData = null; load(); }
    else { paintTree(); paintList(); }
  }

  /* 请求序号：只有最后一次发起的加载才允许写状态。
     否则快速连点/切换数据源时，先发后到的响应会把新数据覆盖掉——
     表现出来就是"列表显示的是另一个源，还切不回来"。 */
  let loadSeq = 0;

  async function load() {
    const sid = sourceId();
    if (!sid) return;
    const seq = ++loadSeq;
    const wantPath = cur;
    const box = el.querySelector('#fb-list');
    box.innerHTML = '<div class="pad muted">读取目录…</div>';
    try {
      const [fl, ls] = await Promise.all([
        API.fileFolders(sid),
        API.fileList(sid, wantPath, sortKey, sortOrder),
      ]);
      if (seq !== loadSeq) return;              // 已有更新的请求，丢弃这次结果
      // 双保险：万一服务端换了源，也绝不把两个源的数据拼在一起
      if (fl.source !== sid || ls.source !== sid) return;
      folderData = fl;
      listData = ls;
      cur = ls.path;
      if (!expanded.has(relOf(cur))) expanded.add(relOf(cur));
      paintTree();
      // 树里勾着目录时，列表显示的是"选中结果"而不是当前目录 ——
      // 否则用户刚勾完就被导航冲掉，联动等于白做。
      if (treeViewOn) paintTreeFiles(); else paintList();
    } catch (e) {
      if (seq !== loadSeq) return;
      box.innerHTML = `<div class="pad" style="color:var(--danger)">${U.esc(e.message)}</div>`;
    }
  }

  function relOf(abs) {
    if (!listData || !listData.root) return '';
    const r = listData.root.replace(/\/+$/, '');
    return abs && abs.startsWith(r + '/') ? abs.slice(r.length + 1) : '';
  }

  async function go(absPath) {
    cur = absPath || '';
    checked.clear();
    // 换了目录就让侧栏高亮跟上（否则两边显示的目录不一致）
    App.syncTree && App.syncTree(cur);
    await load();
  }

  /**
   * 定位到某个文件：切到它所在目录、把它选中并滚动到可见位置。
   *
   * 给"曲库浏览里看到一首歌 → 想直接去文件浏览操作它"这条路径用。
   * 文件可能不在索引里（比如刚扫描过又改了名），那种情况只切目录并提示。
   */
  async function revealFile(sid, path) {
    if (!sid || !path) return false;
    const folder = String(path).replace(/\/[^/]*$/, '');
    // 换源：曲库浏览当前源可能跟文件浏览的不一样
    if (State.activeSource !== sid) {
      State.activeSource = sid;
      folderData = null;
      listData = null;
      expanded.clear();
      if (App.renderSidebar) App.renderSidebar();
      if (App.renderTree) await App.renderTree();
    }
    await go(folder);
    // 载入后按路径找那一行（用 path 而不是 name，避免同名文件误选）
    const box = el.querySelector('#fb-list');
    const rows = Array.from(box.querySelectorAll('tr[data-path]'));
    const hit = rows.find((tr) => tr.dataset.path === path);
    if (!hit) {
      // 有些视图只存了 name/did，退一步按文件名找
      const base = String(path).split('/').pop();
      const alt = rows.find((tr) => (tr.dataset.name || '') === base);
      if (!alt) return false;
      return highlightRow(alt, base);
    }
    return highlightRow(hit, hit.dataset.name || '');
  }

  function highlightRow(tr, name) {
    const box = el.querySelector('#fb-list');
    box.querySelectorAll('tr').forEach((r) => r.classList.remove('found'));
    tr.classList.add('found');
    try { tr.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch (e) { /* ignore */ }
    const didKey = tr.dataset.did;
    if (didKey) checked.add(didKey);
    paintSelbar();
    UI.toast(`已定位到「${name}」，可直接移动 / 编辑标签 / 加入候选区`, 'ok', 6000);
    return true;
  }

  /* ------------------------------------------------------------ 搜索 */

  let searchSeq = 0;
  let searchMode = false;

  async function runSearch(q) {
    const sid = sourceId();
    const clearBtn = el.querySelector('#fb-qclear');
    clearBtn.classList.toggle('hidden', !q);
    if (!q) {
      searchMode = false;
      await load();
      return;
    }
    searchMode = true;
    const mine = ++searchSeq;
    const box = el.querySelector('#fb-list');
    box.innerHTML = '<div class="pad muted">搜索中…</div>';
    try {
      // 文件和文件夹一起搜，一边一半：找歌和找目录都是常见诉求
      const [files, folders] = await Promise.all([
        API.search(sid, q, { kind: 'track', limit: 300 }),
        API.search(sid, q, { kind: 'folder', limit: 60 }),
      ]);
      if (mine !== searchSeq) return;
      paintSearch(q, files, folders);
    } catch (e) {
      if (mine !== searchSeq) return;
      box.innerHTML = `<div class="pad" style="color:var(--danger)">${U.esc(e.message)}</div>`;
    }
  }

  function paintSearch(q, files, folders) {
    const box = el.querySelector('#fb-list');
    el.querySelector('#fb-crumb').innerHTML =
      `<span class="muted">搜索结果：</span><b>${U.esc(q)}</b>
       <span class="muted">· 曲目 ${files.track_total} 个 · 文件夹 ${folders.folder_total} 个</span>`;
    const dirRows = (folders.folders || []).map((f) => `
      <tr class="fb-dir" data-path="${U.esc(f.path)}" data-rel="${U.esc(f.rel)}"
          data-name="${U.esc(f.name)}">
        <td class="fb-c"><svg class="ico"><use href="#i-folder"></use></svg></td>
        <td class="ell"><b>${U.esc(f.name)}</b>
          <span class="muted" style="font-size:11.5px">${U.esc(f.rel)}</span></td>
        <td class="num">${f.count} 首</td>
        <td class="num">${U.fmtBytes(f.bytes)}</td>
        <td class="num">-</td><td class="num">-</td>
        <td class="num fb-dir-act">
          <button class="btn icon sm ghost" data-dir-menu title="更多操作">⋯</button></td>
      </tr>`).join('');
    const fileRows = (files.tracks || []).map((t) => {
      const id = did(t.path);
      const on = checked.has(id);
      const why = (t.hit || []).join('/') || '路径';
      return `
      <tr class="fb-file ${on ? 'sel' : ''}" data-did="${U.esc(id)}"
          data-path="${U.esc(t.path)}" data-name="${U.esc(t.name || '')}">
        <td class="fb-c"><input type="checkbox" data-check="${U.esc(id)}" ${on ? 'checked' : ''}></td>
        <td class="fb-name">
          ${UI.coverHtml(t, 'cover sm')}
          <div style="min-width:0">
            <div class="ell" title="${U.esc(t.path)}"><b>${U.esc(t.name)}</b></div>
            <div class="muted ell" style="font-size:11.5px">
              ${U.esc(t.artist || '')}${t.album ? ' · ' + U.esc(t.album) : ''}
              · <span class="tag">命中${U.esc(why)}</span>
            </div>
          </div>
        </td>
        <td class="num">${U.fmtBytes(t.size)}</td>
        <td class="num">${U.fmtDur(t.duration)}</td>
        <td class="num">${t.quality != null ? (t.quality || 0).toFixed(1) : '-'}</td>
        <td class="num">${U.fmtDate(t.mtime)}</td>
        <td class="num fb-act">
          <button class="btn icon sm" data-fplay="${t.id || ''}" title="试听"
            ${t.id ? '' : 'disabled'}>${U.icon('play')}</button></td>
      </tr>`;
    }).join('');
    const empty = !dirRows && !fileRows;
    box.innerHTML = `<table class="data fb-table">
      <thead><tr>
        <th class="fb-c"></th><th>名称</th><th class="num">大小</th>
        <th class="num">时长</th><th class="num">音质分</th>
        <th class="num">修改时间</th><th></th>
      </tr></thead>
      <tbody>${dirRows}${fileRows}
        ${empty ? `<tr><td colspan="7" class="pad muted">
          没找到匹配「${U.esc(q)}」的文件或文件夹。模糊匹配认错别字和拼音缩写，
          差异过大时可能无法匹配，建议改用更短的关键词。</td></tr>` : ''}
      </tbody></table>`;
    box.querySelectorAll('tr.fb-dir').forEach((tr) => {
      const dir = { path: tr.dataset.path, rel: tr.dataset.rel, name: tr.dataset.name };
      tr.onclick = (e) => {
        if (e.target.closest('[data-dir-menu]')) return;
        el.querySelector('#fb-q').value = '';
        searchMode = false;
        go(dir.path);
      };
      UI.attachMenu(tr, () => folderMenuItems(dir));
      const mb = tr.querySelector('[data-dir-menu]');
      if (mb) mb.onclick = (e) => {
        e.stopPropagation();
        const r = mb.getBoundingClientRect();
        UI.contextMenu(r.left, r.bottom + 4, folderMenuItems(dir));
      };
    });
    box.querySelectorAll('[data-check]').forEach((cb) => {
      cb.onclick = (e) => e.stopPropagation();
      cb.onchange = () => {
        const id = cb.dataset.check;
        if (cb.checked) checked.add(id); else checked.delete(id);
        const tr = cb.closest('tr');
        if (tr) tr.classList.toggle('sel', cb.checked);
        refreshTreeChecks();       // 目录树的半选状态要跟着变
        paintSelbarFrom(files.tracks || []);
      };
    });
    box.querySelectorAll('tr.fb-file').forEach((tr) => {
      tr.onclick = (e) => {
        if (e.target.closest('button') || e.target.type === 'checkbox') return;
        const id = tr.dataset.did;
        if (checked.has(id)) checked.delete(id); else checked.add(id);
        paintSearch(q, files, folders);
        refreshTreeChecks();
        paintSelbarFrom(files.tracks || []);
      };
    });
    box.querySelectorAll('[data-fplay]').forEach((b) => {
      b.onclick = (e) => {
        e.stopPropagation();
        const t = (files.tracks || []).find((x) => x.id === Number(b.dataset.fplay));
        if (t) App.playTrack(t, { label: U.qualityLabel(t) });
      };
    });
    el.querySelector('#fb-info').textContent =
      `搜索「${q}」：${files.track_total} 个曲目 · ${folders.folder_total} 个文件夹`;
    paintSelbarFrom(files.tracks || []);
  }

  /** 搜索结果专用的选中条：条目来自各个目录，不能按当前目录反查。 */
  /**
   * 画选中条。
   *
   * `tracks` 是"当前可见的这批"（普通目录列表或搜索/树选择结果），
   * 选中条上的批量操作都只作用在**这批里被勾上的**那些。
   * `onSelectAll` 允许调用方接管"重绘"（普通列表走搜索重绘，
   * 树选择结果只需就地重画）。
   */
  /**
   * 只刷新"选中条"里的**数字与按钮状态**，不重建整张表、不重跑搜索。
   *
   * 为什么需要：`paintSelbarFrom` 会重建整条 bar 的 `innerHTML`，
   * 而调用方常常紧接着又要重画列表 —— 实测勾一行要 65 ms，
   * 点全选要 120 ms，用户看到的就是"点了半天状态才变"。
   *
   * 这里只改文本节点和 disabled 属性，是 O(1) 级别的更新。
   */
  function repaintSelbarOnly(bar, tracks, onSelectAll) {
    if (!bar) return;
    const picked = tracks.filter((t) => checked.has(did(t.path)));
    const n = picked.length;
    const total = tracks.length;
    const bytes = picked.reduce((a, t) => a + (t.size || 0), 0);
    const allOn = total > 0 && n === total;
    const someOn = n > 0 && n < total;
    const allBox = bar.querySelector('#fb-all');
    if (allBox) {
      allBox.checked = allOn;
      allBox.indeterminate = someOn;
      const span = allBox.parentElement.querySelector('span');
      if (span) {
        span.textContent = allOn ? `取消全选（${total}）`
          : someOn ? `已选 ${n} / ${total}` : `全选（${total}）`;
      }
    }
    const counters = bar.querySelectorAll('[data-selnum]');
    counters.forEach((s) => {
      s.innerHTML = `已选 <b>${n}</b> 个${n ? ' · ' + U.fmtBytes(bytes) : ''}`;
    });
    bar.querySelectorAll('[data-needs-sel]').forEach((b) => {
      b.disabled = !n;
    });
    const one = bar.querySelector('[data-needs-one]');
    if (one) one.disabled = n !== 1;
  }

  function paintSelbarFrom(tracks, onSelectAll) {
    const bar = el.querySelector('#fb-selbar');
    if (!bar) return;
    const picked = tracks.filter((t) => checked.has(did(t.path)));
    const n = picked.length;
    const total = tracks.length;
    const bytes = picked.reduce((a, t) => a + (t.size || 0), 0);
    const allOn = total > 0 && n === total;
    const someOn = n > 0 && n < total;
    const stagedDid = new Set(
      (typeof Staging !== 'undefined') ? Staging.items.map((i) => i.did) : []);
    const stagedHere = picked.filter((t) => stagedDid.has(did(t.path))).length;
    const fresh = n - stagedHere;
    const redraw = () => {
      if (typeof onSelectAll === 'function') onSelectAll();
      else runSearch(el.querySelector('#fb-q').value.trim());
    };
    bar.innerHTML = `
      <label class="check" style="margin:0">
        <input type="checkbox" id="fb-all" ${allOn ? 'checked' : ''}>
        <span>${allOn ? `取消全选（${total}）`
          : someOn ? `已选 ${n} / ${total}` : `全选（${total}）`}</span>
      </label>
      <span class="sep-v"></span>
      <span class="muted" data-selnum>已选 <b>${n}</b> 个${n ? ' · ' + U.fmtBytes(bytes) : ''}${
        stagedHere ? ` · 其中 ${stagedHere} 个已在候选区` : ''}</span>
      <span class="sep-v"></span>
      <span class="muted" style="font-size:12.5px">快速集合：</span>
      <button class="btn sm ghost" id="fb-by-artist" data-needs-sel ${n ? '' : 'disabled'}>同作者</button>
      <button class="btn sm ghost" id="fb-by-album" data-needs-sel ${n ? '' : 'disabled'}>同专辑</button>
      <button class="btn sm ghost" id="fb-by-folder" data-needs-sel ${n ? '' : 'disabled'}
              title="把选中文件所在的整个文件夹都算进来">整个文件夹</button>
      <div class="grow"></div>
      <button class="btn sm ghost" id="fb-analyze-one" data-needs-one ${n === 1 ? '' : 'disabled'}
              title="在库里现有的文件夹之间找这首歌该去的地方，并说明理由">
        ${U.icon('search')}<span>分析去向</span></button>
      <button class="btn sm" id="fb-stage-add" data-needs-sel ${fresh > 0 ? '' : 'disabled'}
              title="加进候选区；已经在候选区里的会自动跳过">
        ${U.icon('plus')}<span>加入候选区</span>${fresh > 0 ? `<b>(${fresh})</b>` : ''}</button>
      <button class="btn sm" id="fb-tagedit" data-needs-sel ${n ? '' : 'disabled'}
              title="改标题/作者/专辑等标签，可批量；写入前自动备份">
        ${U.icon('edit')}<span>编辑标签</span></button>
      <button class="btn sm primary" id="fb-collect" data-needs-sel data-exclusive ${n ? '' : 'disabled'}>
        ${U.icon('plus')}<span>集合成文件夹…</span></button>
      <button class="btn sm" id="fb-xfer" data-needs-sel data-exclusive ${n ? '' : 'disabled'}
        title="传到**另一个**数据源（复制→校验→再删源；失败源不会丢）">
        ${U.icon('db')}<span>跨库传输…</span></button>
      <button class="btn sm primary" id="fb-move" data-needs-sel data-exclusive ${n ? '' : 'disabled'}>
        ${U.icon('folder')}<span>移动到…</span></button>
      <button class="btn sm danger" id="fb-trash" data-needs-sel data-exclusive ${n ? '' : 'disabled'}>${U.icon('trash')}<span>移入回收站</span></button>`;
    // ★ 两个选中条（普通列表 / 目录树结果）**都要**有跨库传输入口。
    //   原来只有普通列表那个有「移动到…」，树结果视图里连移动都没有
    //   —— 用户在树里选完一批文件，能做的操作比普通列表少一截。
    const xfer1 = bar.querySelector('#fb-xfer');
    if (xfer1) xfer1.onclick = () => transferDialog(picked);
    const allBox = bar.querySelector('#fb-all');
    if (allBox) {
      allBox.indeterminate = someOn;          // ← 半选：部分选中时一眼能看出
      allBox.onchange = () => {
        const want = allBox.checked;
        // 批量改的是**全部文件**（含只渲染了前 800 行的那部分），
        // 所以不能只翻 DOM 上的复选框，必须改 `checked` 集合。
        if (want) treeExcluded.clear();
        tracks.forEach((t) => {
          const k = did(t.path);
          if (want) checked.add(k); else checked.delete(k);
        });
        if (!want && treeViewOn) {
          // 取消全选 = 连"选了哪些目录"一起取消，否则再勾别的目录会连带
          treePicked.clear();
          treeExcluded.clear();
          paintTree();
          syncTreeToFiles();
          return;
        }
        // ★ 就地翻可见的复选框，**不要**重建整张表。
        //   重建 = 重新 `innerHTML` 800 行，实测 120 ms 且会丢滚动位置，
        //   用户感受到的就是"点了全选，状态半天不动"。
        el.querySelectorAll('#fb-list input[data-check]').forEach((cb) => {
          cb.checked = want && !cb.disabled;
          const tr = cb.closest('tr');
          if (tr) tr.classList.toggle('sel', cb.checked);
        });
        applyDirIndexNow();                   // 索引重建（一次，O(文件数)）
        refreshTreeChecks();
        repaintSelbarOnly(bar, tracks, onSelectAll);
      };
    }
    bar.querySelector('#fb-stage-add').onclick = () => {
      const add = picked.filter((t) => !stagedDid.has(did(t.path)));
      if (!add.length) return UI.toast('选中的文件都已经在候选区里了', 'warn', 4000);
      Staging.add(add);
      UI.toast(`已加入候选区 ${add.length} 个`
        + (stagedHere ? `（${stagedHere} 个已在候选区，跳过）` : ''), 'ok', 5000);
      redraw();
    };
    bar.querySelector('#fb-by-folder').onclick = () => collectByFolder(picked);
    bar.querySelector('#fb-move').onclick = () => moveDialog(picked);
    bar.querySelector('#fb-by-artist').onclick = () => collectBy('artist', picked);
    bar.querySelector('#fb-by-album').onclick = () => collectBy('album', picked);
    bar.querySelector('#fb-collect').onclick = () => collectDialog(picked);
    bar.querySelector('#fb-trash').onclick = () => trashDialog(picked);
    bar.querySelector('#fb-analyze-one').onclick = () => {
      if (picked.length === 1) Organizer.analyze(sourceId(), { path: picked[0].path });
    };
    bar.querySelector('#fb-tagedit').onclick = () => {
      TagEditor.open({ source: sourceId(), paths: picked.map((t) => t.path) });
    };
  }

  /* ------------------------------------------------------------ 集合成文件夹 */

  /** 把选中的曲目集合到一个（可新建的）文件夹里。 */
  async function collectDialog(sel) {
    if (!sel.length) return;
    const sid = sourceId();
    const total = sel.reduce((a, t) => a + (t.size || 0), 0);
    // 搜索模式下条目散落在各个目录，默认放到数据源根目录更直观
    let parentAbs = (searchMode && listData) ? listData.root : cur;
    const ctl = UI.modal({
      title: `集合 ${sel.length} 个文件`,
      size: 'md',
      body: `
        <div class="filtnote" style="margin-bottom:12px">
          把这 <b>${sel.length}</b> 个文件（${U.fmtBytes(total)}）移动到一个文件夹里。
          文件夹不存在就新建。<b>这是移动，不删任何内容。</b>
        </div>
        <div class="field">
          <label>新文件夹放在哪里？</label>
          <div class="mv-dest-box">
            <div class="mono mv-dest-path" id="cl-parent">${U.esc(parentAbs)}</div>
            <button class="btn sm" id="cl-pick">${U.icon('search')}<span>浏览 / 搜索目录…</span></button>
          </div>
        </div>
        <div class="field">
          <label>新文件夹名称</label>
          <input type="text" id="cl-name" placeholder="例如 我的精选、Shiroi-Ice 合集…">
          <div class="hint">留空 = 不新建文件夹，直接移动进上面那个目录本身。</div>
        </div>
        <div id="cl-report"></div>`,
      footer: `<span class="muted grow" id="cl-sum"></span>
               <button class="btn ghost" data-act="no">取消</button>
               <button class="btn primary" data-act="yes">${U.icon('check')}<span>开始集合</span></button>`,
    });
    const parentBox = ctl.body.querySelector('#cl-parent');
    const nameBox = ctl.body.querySelector('#cl-name');
    const report = ctl.body.querySelector('#cl-report');
    const sumBox = ctl.footer.querySelector('#cl-sum');
    let conflicts = 0;

    const refresh = async () => {
      const nm = nameBox.value.trim();
      sumBox.innerHTML = `目标：<span class="mono">${
        U.esc((parentAbs.split('/').pop() || '') + (nm ? '/' + nm : ''))}</span>`;
      try {
        const r = await API.collectPreview({
          source: sid, parent: parentAbs, name: nm, paths: sel.map((t) => t.path),
        });
        conflicts = (r.conflicts || []).length;
        report.innerHTML = `
          <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(110px,1fr))">
            ${UI.stat('将移动', r.count)}
            ${UI.stat('总体积', r.human_bytes)}
            ${UI.stat('同名冲突', conflicts)}
          </div>
          ${conflicts ? `<div class="muted" style="font-size:12px;margin-top:6px">
            目标里已有 ${conflicts} 个同名文件，会自动改名避免覆盖。</div>` : ''}
          ${(r.missing || []).length ? `<div class="muted" style="font-size:12px;margin-top:6px">
            ${r.missing.length} 个文件在磁盘上找不到了，会被跳过。</div>` : ''}`;
      } catch (e) {
        report.innerHTML = `<div style="color:var(--danger);font-size:12px">${U.esc(e.message)}</div>`;
      }
    };
    refresh();
    nameBox.oninput = U.debounce(refresh, 350);
    ctl.body.querySelector('#cl-pick').onclick = async () => {
      const picked = await FolderPicker.open({ source: sid, start: parentAbs,
                                               title: '选择新文件夹放在哪里' });
      if (picked === null) return;
      parentAbs = picked;
      parentBox.textContent = parentAbs;
      refresh();
    };
    ctl.footer.querySelector('[data-act=no]').onclick = () => ctl.close();
    ctl.footer.querySelector('[data-act=yes]').onclick = async () => {
      const nm = nameBox.value.trim();
      const policy = conflicts ? 'rename' : 'skip';
      const ok = await UI.confirm(
        nm ? `新建文件夹 <b>${U.esc(nm)}</b>，把 ${sel.length} 个文件移进去？`
           : `把 ${sel.length} 个文件移动到 <span class="mono">${U.esc(parentAbs)}</span>？`,
        { title: '集合确认', okText: '开始集合' });
      if (!ok) return;
      ctl.close();
      const wait = UI.modal({
        title: '正在集合', size: 'md',
        body: '<div class="muted">正在移动文件…（文件多时请稍候）</div>',
        footer: '<button class="btn ghost" disabled>关闭</button>',
      });
      try {
        // 集合成文件夹 = 真的移动文件 → 走独占入口
        const r = await App.runExclusive('collect', () => API.collectApply({
          source: sid, parent: parentAbs, name: nm,
          paths: sel.map((t) => t.path), execute: true, confirm: true, policy,
        }));
        wait.body.innerHTML = `
          <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(110px,1fr))">
            ${UI.stat('已移动', r.moved || 0)}
            ${UI.stat('跳过', (r.summary || {}).skipped || 0)}
            ${UI.stat('失败', r.failed || 0)}
          </div>
          <div class="muted" style="margin-top:8px;font-size:12px">
            目标：<span class="mono">${U.esc(r.dest || '')}</span></div>`;
        wait.footer.querySelector('button').disabled = false;
        wait.footer.querySelector('button').onclick = () => wait.close();
        UI.toast(`已集合 ${r.moved || 0} 个文件到「${nm || '目标目录'}」`,
          r.failed ? 'warn' : 'ok', 6000);
        checked.clear();
        el.querySelector('#fb-q').value = '';
        searchMode = false;
        await go(r.dest || parentAbs);
      } catch (e) {
        wait.body.innerHTML = `<div style="color:var(--danger)">${U.esc(e.message)}</div>`;
        wait.footer.querySelector('button').disabled = false;
        wait.footer.querySelector('button').onclick = () => wait.close();
      }
    };
  }

  /* ------------------------------------------------------------ 目录树 */
  function childFolders(rel) {
    const all = (folderData && folderData.folders) || [];
    const pre = rel ? rel + '/' : '';
    return all.filter((f) => {
      if (rel && !f.rel.startsWith(pre)) return false;
      const rest = f.rel.slice(pre.length);
      return rest && !rest.includes('/');
    });
  }

  /**
   * 目录的"勾选覆盖情况"。
   *
   * 三种状态，和文件管理器一致：
   *   `none`  没有任何子目录被勾
   *   `all`   自己 + **所有**子目录都被勾了
   *   `some`  只勾了一部分 → 父节点要显示**半选**（indeterminate）
   */
  /**
   * 目录的覆盖状态 —— **按文件层实际勾选**算（树视图下唯一真相）。
   *
   * 为什么必须这样：`treePicked`（哪些目录被勾）和 `checked`（哪些文件被勾）
   * 是两套状态。`paintTree` 原来只看 `treePicked`，而
   * `refreshTreeChecks` 看文件；于是：
   *   · 在右侧点「取消全选」→ 文件全不勾，树看着也空了
   *   · 再勾另一个目录 → `treePicked` 里**旧的目录还在** →
   *     旧目录的文件被一起勾回来（用户反馈 1）
   *   · 任何一次重绘（展开/排序/刷新）都会把树倒回 `treePicked` 的旧样子
   *     （用户反馈 2：右侧全选/部分选与树不同步）
   * 现在树视图下一切以文件为准，`treePicked` 只在"没有任何树结果"时兜底。
   */
  function coverFromList(rel, files, strictRoot) {
    let total = 0, on = 0;
    for (const f of files) {
      const r = relOf(f.path);
      if (rel && !(r === rel || r.startsWith(rel + '/'))) continue;
      total += 1;
      if (checked.has(did(f.path))) on += 1;
    }
    // 树视图的列表里没有这个目录的文件 → 它就是"没选中"。
    // 不要再回退 `treePicked`：那是另一套状态，回退就会把已经取消的目录
    // 又显示成勾选（用户反馈："取消后勾别的文件夹，旧的又一起勾上"）。
    if (!total) return 'none';
    if (rel === '' && strictRoot) {
      // ★ 根节点单独判断。
      //
      //   踩过的坑（用户反馈："左侧选了某个文件夹后，上面的根目录也会被勾选"）：
      //   根节点的 rel 是空串，上面那个过滤条件对空串不起作用，于是它统计的是
      //   **当前树结果里的全部文件** —— 只要右侧全勾，根节点就显示"全选"，
      //   看起来像"整个库都被选中了"（其实只选了一个子目录）。
      //
      //   现在：只有用户**显式勾了根目录**、且文件确实全勾，才算"全选"；
      //   选了子目录只显示"半选"；一个没勾才是"未选"。
      if (on === 0) return 'none';
      return (treePicked.has('') && on === total) ? 'all' : 'some';
    }
    return on === 0 ? 'none' : (on === total ? 'all' : 'some');
  }

  function coverFromFiles(rel) {
    return coverFromList(rel, treeFiles, true);
  }

  /** 只按"用户显式勾过哪些目录"算（普通目录视图里用）。 */
  function treeCoverPicked(rel) {
    const self = treePicked.has(rel);
    const kids = childFolders(rel);
    if (!kids.length) return self ? 'all' : 'none';
    const allKids = kids.every((k) => treeCover(k.rel) === 'all');
    if (self && allKids) return 'all';
    if (self || kids.some((k) => treeCoverPicked(k.rel) !== 'none')) return 'some';
    return 'none';
  }

  /**
   * 目录树的覆盖状态（唯一入口）。
   *
   * 三种情况：
   *   1. 树结果视图（勾了目录，右侧显示汇总）→ 按 `treeFiles` 里**当前选中**的文件算；
   *   2. 普通目录视图 → 当前目录按**本层文件**算（这样「全选本目录」能反映到树上），
   *      它的祖先显示半选，其他目录仍按显式勾选算；
   *   3. 其余 → 按 `treePicked`。
   */
  function treeCover(rel) {
    if (treeViewOn) return coverFromList(rel, treeFiles, true);
    const fs = (listData && listData.files) || [];
    const curRel = (listData && listData.rel) || '';
    if (rel === curRel) return coverFromList(rel, fs, false);
    // 当前目录的**祖先**（含根节点 rel==''）：本层有勾选就算半选
    if (rel === '' || curRel.startsWith(rel + '/')) {
      return fs.some((f) => checked.has(did(f.path))) ? 'some' : 'none';
    }
    return treeCoverPicked(rel);
  }

  /**
   * 把一个目录（连同**所有**子目录）整体勾上或取消。
   *
   * 为什么必须级联 —— 这是踩出来的：
   *
   * 父节点处于**半选**（indeterminate）时，浏览器点击后把
   * `checked` 置为 `true`（半选语义上是"部分"，所以先补齐）。
   * 这时 `treeCover(parent)==='all'`，子节点被全部勾上。
   * 而取消时如果只 `treePicked.delete(parent)`，**子节点还留在集合里**，
   * `treeCover(parent)` 立刻又变回 `'some'`（半选），
   * 于是怎么点都清不掉 —— 表现就是"取消勾选文件夹，文件仍然全选"。
   *
   * 级联之后父子状态永远自洽：取消父 = 连子一起取消。
   */
  function treeSetAll(rel, on) {
    if (on) treePicked.add(rel); else treePicked.delete(rel);
    childFolders(rel).forEach((k) => treeSetAll(k.rel, on));
    // 显式勾上某个目录 = "这里整体都要"，把它范围内的手动取消记录清掉；
    // 否则取消过的那几首永远也回不来（用户会觉得"勾了没用"）。
    if (on && treeExcluded.size) {
      const pre = rel ? rel + '/' : '';
      for (const k of [...treeExcluded]) {
        if (!pre) { treeExcluded.delete(k); continue; }
        const f = treeFiles.find((x) => did(x.path) === k);
        if (!f) continue;
        const r = relOf(f.path);
        if (r === rel || r.startsWith(pre)) treeExcluded.delete(k);
      }
    }
  }

  /**
   * 只刷新目录树里各复选框的勾选 / 半选状态，**不重建 DOM**。
   *
   * 为什么不能直接调 `paintTree()`：它会重画整棵树并调 `paintTreeBar()`，
   * 而 `paintTreeBar()` 又会触发 `syncTreeToFiles()` —— 循环。
   * 这里只改属性，开销小也不会递归。
   *
   * ★ 关键：判断依据是**文件层的实际勾选**，不是"这个目录被点过"。
   *
   * 踩过：原来只按 `treePicked`（目录被点过没有）算，于是
   * "勾了目录 → 再取消其中 3 个文件"之后，目录框**还是全勾**，
   * 和下面列表对不上 —— 用户看到的就是"复选框状态不对"。
   * 现在按真实勾选反推：
   *   全部选中 → 全勾    选中一部分 → 半选    一个没选 → 空
   */
  // ---------------------------------------------------------------- 目录覆盖索引
  //
  // `refreshTreeChecks()` 原来是 O(目录节点数 × 文件数)，而且把 `relOf(f.path)`
  // 这种带字符串切分的调用放在**内层循环**里。在真实库（101 个目录节点、
  // 2301 个文件）上实测：**勾一个复选框要 65 ms**，点「全选」要 120 ms
  // —— 用户感受到的就是"勾了之后状态半天不更新"。
  //
  // 现在把所有文件的"从属关系"一次性摊平：
  //   `dirTotal[rel]`  = 这个目录（含子目录）下有几个文件
  //   `dirOn[rel]`     = 其中几个已勾选
  //   `didDir[did]`    = 这个文件属于哪些目录（祖先链，用来增量更新）
  // 之后单行切换只需沿着祖先链 +1/-1，不用再扫全表。
  let dirTotal = new Map();
  let dirOn = new Map();
  let didDir = new Map();

  /** 重建目录覆盖索引。文件集合或勾选集合**结构**变化时调用。 */
  function rebuildDirIndex() {
    dirTotal = new Map();
    dirOn = new Map();
    didDir = new Map();
    if (!(treeViewOn && treeFiles.length)) return;
    for (const f of treeFiles) {
      const r = relOf(f.path);
      const k = did(f.path);
      const dirs = ancestorsOf(r);
      didDir.set(k, dirs);
      const isOn = checked.has(k);
      for (const d of dirs) {
        dirTotal.set(d, (dirTotal.get(d) || 0) + 1);
        if (isOn) dirOn.set(d, (dirOn.get(d) || 0) + 1);
      }
    }
  }

  /** 某个 rel 的祖先链（含自己，以及根 ''）。 */
  function ancestorsOf(rel) {
    const out = [''];
    if (!rel) return out;
    const segs = String(rel).split('/').filter(Boolean);
    let cur = '';
    for (const s of segs) {
      cur = cur ? cur + '/' + s : s;
      out.push(cur);
    }
    return out;
  }

  /** 单个文件的勾选状态变化 → 只更新它祖先链上的计数。 */
  function bumpDirIndex(k, on) {
    const dirs = didDir.get(k);
    if (!dirs) return false;
    for (const d of dirs) {
      dirOn.set(d, Math.max(0, (dirOn.get(d) || 0) + (on ? 1 : -1)));
    }
    return true;
  }

  /** 批量改勾选（全选/全不选）后重建索引，比逐条 bump 快。 */
  function applyDirIndexNow() {
    rebuildDirIndex();
  }

  function refreshTreeChecks() {
    const box = el.querySelector('#fb-tree');
    if (!box) return;
    // ★ 和 `paintTree` 用**同一个**判断函数（`treeCover`）。
    //   以前这里读 dirTotal/dirOn 索引、`paintTree` 读 `treePicked`，
    //   两套算法必然算出不同结果 —— 这就是"树和右侧不同步"的根源。
    //   现在两边都走 treeCover：树视图下按文件算，平时按目录勾选算。
    const nodes = box.querySelectorAll('.fb-node');
    for (const n of nodes) {
      const cb = n.querySelector('.fb-pick');
      if (!cb) continue;
      const rel = n.dataset.rel || '';
      const cover = treeCover(rel);
      cb.checked = cover === 'all';
      cb.indeterminate = cover === 'some';
      cb.dataset.cover = cover;
      n.classList.toggle('picked', cover !== 'none');
    }
    paintTreeBar();
  }

  function paintTree() {
    const box = el.querySelector('#fb-tree');
    if (!folderData) { box.innerHTML = ''; return; }
    const sortBy = el.querySelector('#fb-dsort').value;
    const rows = [];
    const walk = (rel, depth) => {
      let kids = childFolders(rel);
      if (sortBy === 'count') kids = kids.slice().sort((a, b) => b.count_all - a.count_all);
      else if (sortBy === 'name') kids = kids.slice().sort((a, b) => a.rel.localeCompare(b.rel));
      else if (sortBy === 'mtime') kids = kids.slice().sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
      kids.forEach((f) => {
        const isOpen = expanded.has(f.rel);
        const isCur = listData && listData.rel === f.rel;
        const cover = treeCover(f.rel);
        const isPicked = cover !== 'none';
        const pad = 8 + depth * 14;
        const hasKids = childFolders(f.rel).length > 0;
        rows.push(`<div class="fb-node ${isCur ? 'active' : ''} ${isPicked ? 'picked' : ''}"
             data-rel="${U.esc(f.rel)}" style="padding-left:${pad}px">
            <span class="tw" title="${hasKids ? '展开 / 收起' : ''}">${hasKids ? (isOpen ? '▾' : '▸') : ''}</span>
            <input type="checkbox" class="fb-pick" data-pick="${U.esc(f.rel)}"
                   data-cover="${cover}"
                   ${cover === 'all' ? 'checked' : ''}
                   title="${cover === 'some' ? '部分子目录已勾选' : '勾选这个目录（可多选）'}">
            <svg class="ico">${''}<use href="#i-folder"></use></svg>
            <span class="n" title="${U.esc(f.rel)}">${U.esc(f.rel.split('/').pop())}</span>
            <span class="c">${f.count_all}</span>
          </div>`);
        if (isOpen) walk(f.rel, depth + 1);
      });
    };
    rows.push(`<div class="fb-node root ${listData && !listData.rel ? 'active' : ''}" data-rel="">
        <span class="tw"></span>
        <input type="checkbox" class="fb-pick" data-pick=""
               title="勾选根目录（等于全选整库）">
        <svg class="ico"><use href="#i-db"></use></svg>
        <span class="n">（根目录）</span>
        <span class="c">${folderData.total_tracks}</span></div>`);
    walk('', 0);
    box.innerHTML = rows.join('');
    // 半选必须用 DOM 属性设（HTML 里没有这个属性）
    box.querySelectorAll('input.fb-pick[data-cover="some"]').forEach((cb) => {
      cb.indeterminate = true;
      cb.checked = false;
    });
    box.querySelectorAll('.fb-node').forEach((n) => {
      const rel = n.dataset.rel;
      const cb = n.querySelector('.fb-pick');
      // 勾选框：普通点击即可切换（不需要按 Ctrl）——
      // 多选本来就该点勾选框，这是所有人的直觉
      if (cb) {
        cb.onclick = (e) => e.stopPropagation();
        cb.onchange = () => {
          // 级联：勾父节点就把它下面所有子目录一起勾上；
          // 取消同理。不级联的话父节点会永远停在半选，怎么点都清不掉。
          treeSetAll(rel, cb.checked);
          n.classList.toggle('picked', cb.checked);
          // 勾目录 = 选中里面的文件（联动），这是用户期望的语义
          syncTreeToFiles();
        };
      }
      n.onclick = (e) => {
        // 展开箭头
        if (e.target.classList.contains('tw')) {
          if (expanded.has(rel)) expanded.delete(rel); else expanded.add(rel);
          paintTree();
          return;
        }
        if (e.target.classList.contains('fb-pick')) return;
        // Ctrl / Cmd / Shift 点击整行也能多选（保留快捷键）；
        // 这里同样要级联，否则整行操作和复选框行为会不一致
        if (e.ctrlKey || e.metaKey || e.shiftKey) {
          e.preventDefault();
          const next = !treePicked.has(rel);
          treeSetAll(rel, next);
          if (cb) cb.checked = next;
          n.classList.toggle('picked', next);
          syncTreeToFiles();
          return;
        }
        go(rel ? folderData.root.replace(/\/+$/, '') + '/' + rel : folderData.root);
      };
    });
    const active = box.querySelector('.fb-node.active');
    if (active) active.scrollIntoView({ block: 'nearest' });
    paintTreeBar();
  }

  /** 目录树多选后的操作条：把选中的目录作为整理范围等。 */
  function paintTreeBar() {
    const bar = el.querySelector('#fb-tree-bar');
    if (!bar) return;
    const box = el.querySelector('#fb-tree-bar-info');
    // ★ 显示**最小的那批目录**，而不是 `treePicked` 的全部。
    //
    // `treePicked` 里会带上所有祖先（因为"某个子孙里有文件被选中"），
    // 直接数它的话，勾一个 Shiroi-ice 会显示"已选 2 个目录"
    // （它自己 + 被牵连进来的父级），数字对不上用户的认知。
    // 这里只数"父级没有被选中"的那些 —— 也就是列表里真正被勾上的。
    const picked = [];
    treePicked.forEach((rel) => {
      const up = rel.includes('/') ? rel.slice(0, rel.lastIndexOf('/')) : '';
      if (up && treePicked.has(up)) return;      // 父级已选中，不重复计
      picked.push(rel);
    });
    bar.classList.toggle('hidden', picked.length === 0);
    if (!picked.length) {
      // （treeFileCount 已删：死变量）
      treeFiles = [];
      if (box) { box.textContent = ''; box.title = ''; }
      return;
    }
    if (box) {
      const n = picked.length === 1 ? picked[0] : `已选 ${picked.length} 个目录`;
      const extra = treeFiles.length ? ` · 含 ${treeFiles.length} 个文件` : '';
      box.textContent = n + extra;
      box.title = picked.join('\n');
    }
  }

  /**
   * 把目录树的勾选**联动到文件列表**。
   *
   * 这是之前最迷惑的地方：在树上勾了文件夹，文件列表里什么都没变，
   * 让人以为勾选没生效。现在勾目录 = 选中它（含子目录）里的全部文件，
   * 文件列表会**切到"选中结果"视图**把勾上的文件都列出来 ——
   * 因为跨目录的文件不可能同时显示在同一个目录视图里，
   * 必须有一个地方让用户看见"到底选中了什么"。
   */
  async function syncTreeToFiles() {
    const picked = Array.from(treePicked);
    if (!picked.length) {
      // （treeFileCount 已删：死变量）
      treeFiles = [];
      // ★ 必须把勾选清掉再回普通列表 —— 否则刚在树里全选的那批文件
      //   会把普通目录列表也"传染"成全选（用户看到的就是
      //   "取消勾选文件夹后文件仍然全选"）。
      checked.clear();
      treeExcluded.clear();
      if (treeViewOn) {
        treeViewOn = false;
        await load();
      } else {
        paintList();          // 不是树视图也要重画，把残留的勾选状态刷掉
      }
      refreshTreeChecks();    // 树上的框也要跟着变回未勾选
      paintTreeBar();
      return;
    }
    const sid = sourceId();
    const root = folderData ? folderData.root : '';
    const abs = picked.map((r) => (r ? root.replace(/\/+$/, '') + '/' + r : root));
    treeLoading = true;
    paintTreeBar();
    try {
      const r = await API.filesUnder({ source: sid, paths: abs });
      treeFiles = r.files || [];
      // （treeFileCount 已删：死变量）
      // ★ 只勾"用户没手动取消过"的；已经消失的文件从排除表里清掉。
      //   以前是无条件 `checked.clear()` 再全部勾上——用户取消的那首
      //   会在下一次勾选目录时**复活**，看起来就是"勾选状态乱跳"。
      const keep = new Set(treeFiles.map((f) => did(f.path)));
      treeExcluded = new Set([...treeExcluded].filter((k) => keep.has(k)));
      checked.clear();
      treeFiles.forEach((f) => {
        const k = did(f.path);
        if (!treeExcluded.has(k)) checked.add(k);
      });
      treeViewOn = true;
      // ★ 顺序很重要：`paintTreeFiles()` 里才会 `rebuildDirIndex()` 重建
      //   dirTotal/dirOn。原来先 `refreshTreeChecks()`，读的是**上一轮**的
      //   旧索引 —— 于是刚勾的目录显示"未选"、刚取消的目录还显示"全选"，
      //   而且每次都要等下一次刷新才对。这就是"树和右侧不同步"的真凶。
      paintTreeBar();
      paintTreeFiles();          // ← 先画列表（这里重建索引）
      refreshTreeChecks();       // ← 再按新索引刷树
    } catch (e) {
      treeFiles = [];
      // （treeFileCount 已删：死变量）
      treeViewOn = false;
      UI.toast('读取所选目录的文件失败：' + e.message, 'err', 7000);
    } finally {
      treeLoading = false;
      paintTreeBar();
    }
  }
  /** 画出"目录树选中结果"的文件列表（替代普通目录列表，直到取消勾选）。 */
  function paintTreeFiles() {
    const box = el.querySelector('#fb-list');
    if (!box || !treeViewOn) return;
    // 文件集合或勾选集合变了 → 重建目录覆盖索引（一次 O(文件数)），
    // 之后单行勾选只沿祖先链加减，不再重扫。（这就是"勾了状态不更新"的解药）
    rebuildDirIndex();
    // 候选区里的文件要显示成"已在候选区"并且不可再勾（和普通列表一致）
    const stagedDid = new Map();
    if (typeof Staging !== 'undefined') {
      Staging.items.forEach((i) => stagedDid.set(i.did, {
        color: Staging.colorOf(i.dst),
        label: i.dstLabel || (i.dst ? i.dst.split('/').pop() : '待定'),
      }));
    }
    // 已经在候选区的不算"待加入"，从 checked 里剔掉，数字才对得上
    let removed = false;
    stagedDid.forEach((_v, k) => { if (checked.delete(k)) removed = true; });
    if (removed) rebuildDirIndex();     // 索引要跟着 checked 的实际内容走
    // ★ 注意：`paintSelbarFrom` 会重建整条选中条并给 `#fb-all` 绑事件；
    //   而下面还会重建 `#fb-list` 的整张表。全选处理函数只翻 `#fb-list`
    //   里的复选框、不再重画列表，所以这个顺序是安全的。
    paintSelbarFrom(treeFiles, () => paintTreeFiles());
    if (!treeFiles.length) {
      box.innerHTML = `<div class="empty" style="padding:26px">
        <div class="big">${U.icon('folder', 'ico')}</div>
        <div>所选的目录里没有已索引的音频文件</div>
        <div style="margin-top:10px">
          <button class="btn sm ghost" id="fb-tree-clear">取消勾选，看当前目录</button>
        </div>
        <div class="muted" style="font-size:12px;margin-top:6px">
          如果刚放进来还没扫描，先回「数据源」扫描一次</div></div>`;
      const _clr = box.querySelector('#fb-tree-clear');
      if (_clr) {
        _clr.onclick = () => {
          treePicked.clear();
          checked.clear();
          treeExcluded.clear();
          paintTree();
          syncTreeToFiles();      // 无勾选 → 退出树结果视图并恢复目录列表
        };
      }
      return;
    }
    box.innerHTML = `
      <div class="fb-tree-result">
        以下是从 <b>${treePicked.size}</b> 个目录里选出的
        <b>${treeFiles.length}</b> 个文件（含子目录，来自索引）。
        上面的批量操作都会作用在它们身上；取消目录勾选即退出此视图。
      </div>
      <table class="data fb-table"><thead><tr>
        <th class="fb-c"></th><th>名称</th><th>所在目录</th>
        <th class="right">大小</th><th class="right">时长</th><th></th>
      </tr></thead><tbody>
      ${treeFiles.slice(0, 800).map((t) => {
        const id = did(t.path);
        // 必须按 `checked` 决定勾选状态 —— 写死 `checked` 的话，
        // 每次重绘都会把用户刚取消的勾选又勾回来（"全不选"点了没反应）。
        const on = checked.has(id);
        const st = stagedDid.get(id);
        return `<tr class="fb-file ${on ? 'sel' : ''} ${st ? 'staged' : ''}"
              data-did="${U.esc(id)}"
              data-path="${U.esc(t.path)}" data-name="${U.esc(t.name || '')}"
              ${st ? `style="--c:${st.color};background:color-mix(in srgb, ${st.color} 9%, transparent)"` : ''}>
          <td class="fb-c"><input type="checkbox" data-check="${U.esc(id)}"
                ${on ? 'checked' : ''} ${st ? 'disabled title="这个文件已经在候选区里了"' : ''}></td>
          <td class="fb-name">${UI.coverHtml(t, 'cover sm')}
            <span class="ell" title="${U.esc(t.path)}">${U.esc(t.name)}</span>${
              st ? `<span class="tag staged-tag" style="--c:${st.color};border-color:${st.color};color:${st.color};margin-left:6px">已在候选区 → ${U.esc(st.label)}</span>` : ''}</td>
          <td class="ell muted" style="font-size:11.5px;max-width:260px"
              title="${U.esc(t.path)}">${U.esc(U.parent(t.path))}</td>
          <td class="num">${U.fmtBytes(t.size)}</td>
          <td class="num">${U.fmtDur(t.duration)}</td>
          <td class="num nowrap">
            <button class="btn icon sm" data-play="${U.esc(t.path)}"
              title="试听这一首">${U.icon('play')}</button>
            <button class="btn icon sm ghost" data-qadd="${U.esc(t.path)}"
              title="加入播放列表（队列末尾，不打断当前播放）">${U.icon('queue-add')}</button></td>
        </tr>`;
      }).join('')}
      </tbody></table>
      ${treeFiles.length > 800
        ? `<div class="muted" style="font-size:12px;margin-top:6px">
             仅显示前 800 个，实际共 ${treeFiles.length} 个</div>` : ''}`;
    // 复选框 / 试听 / 行点击（和普通列表保持一致的行为）
    box.querySelectorAll('input[data-check]').forEach((cb) => {
      cb.onchange = () => {
        const k = cb.dataset.check;
        // 勾上 = 移出排除表；取消 = 记进排除表（跨目录重算时保留）
        if (cb.checked) { checked.add(k); treeExcluded.delete(k); }
        else { checked.delete(k); treeExcluded.add(k); }
        const tr = cb.closest('tr');
        if (tr) tr.classList.toggle('sel', cb.checked);
        // ★ 增量更新目录覆盖计数：只沿**这个文件的祖先链**加减，
        //   不再重扫整棵树 × 整份文件表（原来勾一行要 65 ms）。
        //   索引还没建过（比如刚从普通列表切过来）就退回全量重建。
        if (!bumpDirIndex(k, cb.checked)) applyDirIndexNow();
        refreshTreeChecks();
        // ★ 一个文件都不剩 = 目录勾选也该没了。
        //   不清的话：右侧看着"全不选"，`treePicked` 里还留着旧目录，
        //   再勾别的目录会把它一起带回来。
        if (treeViewOn && checked.size === 0) {
          treePicked.clear();
          treeExcluded.clear();
          paintTree();
          syncTreeToFiles();        // 无勾选 → 退出树结果视图
          return;
        }
        // 只刷新数字与按钮状态，不重建整张表
        repaintSelbarOnly(el.querySelector('#fb-selbar'), treeFiles);
      };
    });
    box.querySelectorAll('tr.fb-file').forEach((tr) => {
      tr.onclick = (e) => {
        if (e.target.closest('button') || e.target.closest('input')) return;
        const cb = tr.querySelector('input[data-check]');
        if (!cb) return;
        cb.checked = !cb.checked;
        cb.onchange();
      };
    });
    box.querySelectorAll('[data-play]').forEach((b) => {
      b.onclick = (e) => {
        e.stopPropagation();
        const f = treeFiles.find((x) => x.path === b.dataset.play);
        if (f) App.playTrack(f);
      };
    });
    box.querySelectorAll('[data-qadd]').forEach((b) => {
      b.onclick = (e) => {
        e.stopPropagation();
        const f = treeFiles.find((x) => x.path === b.dataset.qadd);
        if (!f) return;
        const n = App.queue.add([f], '手动加入');
        UI.toast(n ? '已加入播放列表' : '这首已经在播放列表里了',
          n ? 'ok' : 'info', 2600);
        if (App._paintPlayerModes) App._paintPlayerModes();
      };
    });
  }

  /* ------------------------------------------------------- 回收站 */

  /**
   * 回收站：列出 / 还原 / 彻底删除。
   *
   * 为什么加这一块：界面一直写着"删除 = 移入回收站，可恢复"，但应用里
   * 从来没有还原入口 —— 用户得自己去资源管理器翻 `.music-sync-trash\日期\`，
   * 还得记得它原来在哪。这里把承诺补上。
   *
   * 还原默认**回到原位置**（删除时记在 `_origin.json` 里），默认**跳过同名**，
   * 绝不覆盖。彻底删除要输入短语二次确认。
   */
  async function openTrash() {
    const sid = State.activeSource;
    if (!sid) return UI.toast('先选一个数据源', 'warn', 3000);
    let data;
    try {
      data = await API.trash(sid);
    } catch (e) {
      return UI.toast('读回收站失败：' + e.message, 'err', 7000);
    }
    const items = data.items || [];
    if (!items.length) return UI.toast('回收站是空的', 'info', 3000);
    const rows = items.map((it, i) => `
      <tr>
        <td><input type="checkbox" data-ti="${i}"></td>
        <td class="ell" title="${U.esc(it.rel)}">${U.esc(it.name)}</td>
        <td class="ell muted" style="font-size:11.5px"
            title="${U.esc(it.origin || '')}">${it.origin
              ? U.esc(it.origin)
              : '<span class="muted">（未记录，还原时放回库根目录）</span>'}</td>
        <td class="num">${it.is_dir ? '目录' : U.fmtBytes(it.size)}</td>
        <td class="num">${it.mtime ? U.fmtDate(it.mtime) : '-'}</td>
      </tr>`).join('');
    const ctl = UI.modal({
      title: `回收站（${items.length} 项 · ${U.fmtBytes(data.bytes || 0)}）`,
      size: 'lg',
      body: `
        <div class="muted" style="margin-bottom:8px">
          位置：<code>${U.esc(data.root || '')}</code>　
          还原默认回到原位置，<b>绝不覆盖</b>同名文件。
        </div>
        <div class="row wrap" style="gap:10px;margin-bottom:8px;align-items:center">
          <label class="check"><input type="checkbox" id="tr-all"> 全选</label>
          <span class="sep-v"></span>
          <label>同名时
            <select id="tr-policy" style="width:auto">
              <option value="skip">跳过</option>
              <option value="rename">自动改名</option>
            </select>
          </label>
          <div class="grow"></div>
          <button class="btn sm primary" id="tr-restore">还原选中</button>
          <button class="btn sm" id="tr-purge">彻底删除…</button>
        </div>
        <div class="tbl-wrap" style="max-height:48vh;overflow:auto">
          <table class="tbl"><thead><tr>
            <th></th><th>名称</th><th>原位置</th>
            <th class="num">大小</th><th class="num">删除时间</th>
          </tr></thead><tbody>${rows}</tbody></table>
        </div>`,
      footer: '<button class="btn" id="tr-close">关闭</button>',
    });
    const pick = () => [...ctl.body.querySelectorAll('input[data-ti]:checked')]
      .map((cb) => items[Number(cb.dataset.ti)]);
    ctl.body.querySelector('#tr-all').onchange = (e) => {
      ctl.body.querySelectorAll('input[data-ti]').forEach((cb) => {
        cb.checked = e.target.checked;
      });
    };
    ctl.footer.querySelector('#tr-close').onclick = () => ctl.close();
    ctl.body.querySelector('#tr-restore').onclick = async () => {
      const sel = pick();
      if (!sel.length) return UI.toast('先勾选要还原的项', 'warn', 3000);
      const policy = ctl.body.querySelector('#tr-policy').value;
      ctl.close();
      try {
        const r = await API.trashRestore({
          source: sid, items: sel.map((x) => ({ rel: x.rel })), policy,
        });
        const done = (r.results || []).filter((x) => x.state === 'done').length;
        const rest = (r.results || []).length - done;
        UI.toast(`已还原 ${done} 项${rest ? `，${rest} 项未还原（同名或出错）` : ''}`,
          done ? 'ok' : 'warn', 6000);
        reload();
      } catch (e) {
        UI.toast('还原失败：' + e.message, 'err', 8000);
      }
    };
    ctl.body.querySelector('#tr-purge').onclick = async () => {
      const sel = pick();
      if (!sel.length) return UI.toast('先勾选要彻底删除的项', 'warn', 3000);
      ctl.close();
      const yes = await UI.confirmHard({
        title: '彻底删除回收站里的文件？',
        phrase: '永久删除',
        message: `这 ${sel.length} 项会从磁盘上<b>永久删除</b>，不进回收站，无法恢复。`,
        details: sel.map((x) => x.rel),
        okText: '永久删除',
      });
      if (!yes) return;
      try {
        const r = await API.trashPurge({
          source: sid, items: sel.map((x) => ({ rel: x.rel })), confirm: true,
        });
        UI.toast(`已彻底删除 ${r.done} 项`, 'ok', 4000);
        openTrash();
      } catch (e) {
        UI.toast('删除失败：' + e.message, 'err', 8000);
      }
    };
  }

  /* ------------------------------------------------------------ 列表 */
  function crumb() {
    const box = el.querySelector('#fb-crumb');
    if (!listData) { box.innerHTML = ''; return; }
    const root = listData.root;
    const parts = (listData.rel || '').split('/').filter(Boolean);
    let acc = root;
    const bits = [`<a data-p="">${U.icon('db')} 根目录</a>`];
    parts.forEach((p) => {
      acc += '/' + p;
      bits.push(`<span class="sep">›</span><a data-p="${U.esc(acc)}">${U.esc(p)}</a>`);
    });
    box.innerHTML = bits.join('');
    box.querySelectorAll('a').forEach((a) => {
      a.onclick = () => go(a.dataset.p);
    });
  }

  function paintList() {
    crumb();
    const box = el.querySelector('#fb-list');
    const d = listData;
    if (!d) return;
    const cols = [
      ['name', '名称'], ['size', '大小'], ['duration', '时长'],
      ['mtime', '修改时间'], ['quality', '音质分'],
    ];
    const arrow = (k) => (sortKey === k ? (sortOrder === 'asc' ? ' ▲' : ' ▼') : '');
    const dirRows = d.dirs.map((x) => `
      <tr class="fb-dir" data-path="${U.esc(x.path)}" data-rel="${U.esc(x.rel || x.name)}"
          data-name="${U.esc(x.name)}">
        <td class="fb-c"><svg class="ico">${''}<use href="#i-folder"></use></svg></td>
        <td class="ell"><b>${U.esc(x.name)}</b>
          ${x.indexed ? '' : '<span class="tag" title="该目录还没扫描过，数字只数了一层">未建索引</span>'}</td>
        <td class="num">${x.count} 首${x.count !== x.count_here ? `<small class="muted">（本层 ${x.count_here}）</small>` : ''}</td>
        <td class="num">${x.bytes ? U.fmtBytes(x.bytes) : '-'}</td>
        <td class="num">-</td>
        <td class="num">${x.mtime ? U.fmtDate(x.mtime) : '-'}</td>
        <td class="num fb-dir-act">
          <button class="btn icon sm ghost" data-dir-menu title="更多操作（重命名 / 移动 / 删除）"
            >⋯</button>
        </td>
      </tr>`).join('');

    const fileRows = d.files.map((t) => {
      const id = did(t.path);
      const on = checked.has(id);
      return `
      <tr class="fb-file ${on ? 'sel' : ''}" data-did="${U.esc(id)}"
          data-path="${U.esc(t.path)}" data-name="${U.esc(t.name || '')}">
        <td class="fb-c"><input type="checkbox" data-check="${U.esc(id)}" ${on ? 'checked' : ''}></td>
        <td class="fb-name">
          ${UI.coverHtml(t, 'cover sm')}
          <div style="min-width:0">
            <div class="ell" title="${U.esc(t.path)}"><b>${U.esc(t.name)}</b></div>
            <div class="muted ell" style="font-size:11.5px">
              ${U.esc(t.title || '')}${t.artist ? ' · ' + U.esc(t.artist) : ''}
              ${t.indexed ? '' : ' · <span class="tag">未索引</span>'}
            </div>
          </div>
        </td>
        <td class="num">${U.fmtBytes(t.size)}</td>
        <td class="num">${U.fmtDur(t.duration)}</td>
        <td class="num">${U.fmtDate(t.mtime)}</td>
        <td class="num">${t.quality != null ? (t.quality || 0).toFixed(1) : '-'}</td>
        <td class="fb-act">
          <button class="btn icon sm" data-fplay="${t.id || ''}" title="试听"
            ${t.id ? '' : 'disabled'}>${U.icon('play')}</button>
        </td>
      </tr>`;
    }).join('');

    // ★ 本层没有文件、但子目录里有歌时必须提示，否则用户看到的是
    //   "文件夹内没有文件"，然后要自己发现"勾左边的目录"才能看到歌。
    //   （用户反馈：没全选文件夹时显示没有文件，全选后文件才冒出来。）
    const _sub = Number(d.dir_audio_total || 0);
    const _direct = (d.files || []).length;
    const _ndirs = (d.dirs || []).length;
    const subHint = (_ndirs && _sub > 0 && _sub > _direct)
      ? `<div class="fb-subhint">
           ${U.icon('folder')}
           <div class="fb-sh-txt">
             <b>${_direct ? `本层 ${_direct} 首` : '这个文件夹本层没有音频文件'}</b>
             <span>${_direct
               ? `子目录里还有 <span class="fb-sh-num">${_sub}</span> 首`
               : `音频都在下面的 <span class="fb-sh-num">${_ndirs}</span> 个子目录里，共 <span class="fb-sh-num">${_sub}</span> 首`}</span>
           </div>
           <button class="btn sm primary" id="fb-include-sub"
                   title="把本目录连同所有子目录一起显示（等于在左边勾选这个目录）">
             ${U.icon('list')}<span>包含子目录查看</span></button>
         </div>` : '';
    box.innerHTML = subHint + `<table class="data fb-table">
      <thead><tr>
        <th class="fb-c"></th>
        ${cols.map(([k, label]) => `<th class="sortable ${k === 'name' ? '' : 'num'}"
           data-sort="${k}">${label}${arrow(k)}</th>`).join('')}
        <th></th>
      </tr></thead>
      <tbody>${dirRows}${fileRows ||
        (d.dirs.length ? '' : '<tr><td colspan="7" class="pad muted">这个文件夹里没有音频文件</td></tr>')}</tbody>
    </table>`;

    const _inc = box.querySelector('#fb-include-sub');
    if (_inc) {
      _inc.onclick = () => {
        const rel = relOf(cur);
        treeSetAll(rel, true);         // 勾上（含级联子目录）
        paintTree();
        syncTreeToFiles();
        UI.toast('已切换为"包含子目录"视图', 'ok', 3500);
      };
    }
    box.querySelectorAll('th[data-sort]').forEach((th) => {
      th.onclick = () => {
        const k = th.dataset.sort;
        if (sortKey === k) sortOrder = sortOrder === 'asc' ? 'desc' : 'asc';
        else { sortKey = k; sortOrder = k === 'size' || k === 'mtime' || k === 'duration' ? 'desc' : 'asc'; }
        saveFilesSort();
        load();
      };
    });
    box.querySelectorAll('tr.fb-dir').forEach((tr) => {
      const dir = { path: tr.dataset.path, rel: tr.dataset.rel, name: tr.dataset.name };
      tr.onclick = (e) => {
        if (e.target.closest('[data-dir-menu]')) return;
        go(dir.path);
      };
      // 右键 = 完整菜单；行尾的 ⋯ = 同一个菜单（触摸屏没有右键，必须有这个入口）
      UI.attachMenu(tr, () => folderMenuItems(dir));
      const mb = tr.querySelector('[data-dir-menu]');
      if (mb) {
        mb.onclick = (e) => {
          e.stopPropagation();
          const r = mb.getBoundingClientRect();
          UI.contextMenu(r.left, r.bottom + 4, folderMenuItems(dir));
        };
      }
    });
    // 列表空白处右键：对"当前目录"操作
    box.oncontextmenu = (e) => {
      if (e.target.closest('tr')) return;
      e.preventDefault();
      const items = currentDirMenuItems();
      if (items.length) UI.contextMenu(e.clientX, e.clientY, items);
    };
    box.querySelectorAll('[data-check]').forEach((cb) => {
      cb.onclick = (e) => e.stopPropagation();
      cb.onchange = () => {
        const id = cb.dataset.check;
        if (cb.checked) checked.add(id); else checked.delete(id);
        const tr = cb.closest('tr');
        if (tr) tr.classList.toggle('sel', cb.checked);
        refreshTreeChecks();      // 树的半选/全选要跟着本层勾选变
        paintSelbar();
      };
    });
    box.querySelectorAll('tr.fb-file').forEach((tr) => {
      tr.onclick = (e) => {
        if (e.target.closest('button') || e.target.type === 'checkbox') return;
        const id = tr.dataset.did;
        if (checked.has(id)) checked.delete(id); else checked.add(id);
        paintList();
        refreshTreeChecks();
        paintSelbar();
      };
    });
    box.querySelectorAll('[data-fplay]').forEach((b) => {
      b.onclick = (e) => {
        e.stopPropagation();
        const tid = Number(b.dataset.fplay);
        const t = d.files.find((x) => x.id === tid);
        if (t) App.playTrack(t, { label: U.qualityLabel(t) });
      };
    });

    el.querySelector('#fb-info').textContent =
      `${d.dirs.length} 个文件夹 · ${d.file_count} 个音频文件` +
      (d.dirs.length ? ` · 子目录合计 ${d.dir_audio_total} 首` : '');
    paintDisk(d.disk);
    paintSelbar();
    paintStagedMarks();
  }

  /**
   * 磁盘余量条。
   *
   * 为什么要有：往里放文件之前，用户得知道"这块盘还剩多少" ——
   * 否则复制到一半才发现塞不下，白等一场。放在工具栏里，
   * 切目录时跟着变（不同目录可能在不同盘上）。
   */
  function paintDisk(dk) {
    const box = el.querySelector('#fb-disk');
    if (!box) return;
    if (!dk || !dk.total) { box.innerHTML = ''; box.classList.add('hidden'); return; }
    box.classList.remove('hidden');
    const free = dk.free || 0;
    const pct = dk.percent == null ? 0 : dk.percent;
    // 余量紧张时变色：<5% 或 <5GB 都算"该注意了"
    const tight = pct >= 95 || free < 5 * 1024 ** 3;
    const warn = !tight && (pct >= 88 || free < 20 * 1024 ** 3);
    box.innerHTML = `
      <span class="dk-bar ${tight ? 'tight' : warn ? 'warn' : ''}"
            title="${U.esc(dk.dir || '')}&#10;已用 ${U.fmtBytes(dk.used)} / ${U.fmtBytes(dk.total)}">
        <i style="width:${pct}%"></i></span>
      <span class="dk-txt">剩余 <b>${U.fmtBytes(free)}</b></span>
      ${dk.writable ? '' : '<span class="badge err">只读</span>'}
      <button class="btn sm ghost" id="fb-rotoggle"
        title="${dk.writable
          ? '把当前数据源设为只读：之后任何写入/删除都会被拒绝（预防误操作）'
          : '取消只读：恢复可写'}">
        ${U.icon(dk.writable ? 'lock' : 'unlock')}
        <span>${dk.writable ? '设为只读' : '取消只读'}</span></button>`;
    // 只读开关就放在文件视图里，不用翻到「数据源」编辑框去找。
    // 切换后**立即生效**（后端 update_source 会 reset 数据源实例）。
    const rt = box.querySelector('#fb-rotoggle');
    if (rt) {
      rt.onclick = async () => {
        const sid = sourceId();
        // ★ 目标值必须按**最新的**只读状态算，不能用这条磁盘条上的
        //   `dk.writable` —— 那个值是上次 load() 时抓的快照。
        //   踩过：在别处（数据源页 / 另一个标签页）改过只读之后，
        //   这里的按钮文案和动作就都反了，点下去等于没改
        //   （弹窗还写着"取消只读"，可实际想做的却是"设为只读"）。
        rt.disabled = true;
        let curReadOnly = !dk.writable;
        try {
          const fresh = await API.sources();
          const list = fresh.sources || fresh || [];
          const me = list.find((x) => x.id === sid);
          if (me && typeof me.read_only === 'boolean') curReadOnly = me.read_only;
        } catch (e) { /* 拿不到就用快照值 */ }
        const want = !curReadOnly;
        rt.disabled = false;
        const okd = await UI.confirm(
          want
            ? `把当前数据源设为<b>只读</b>？<br><span class="muted">
               之后它的所有写入、删除、改标签都会被拒绝。
               已索引的内容和播放都不受影响，随时可以取消。</span>`
            : `取消<b>只读</b>，恢复这个数据源的可写？<br>
               <span class="muted">恢复后它就能被移动 / 改名 / 改标签。</span>`,
          { title: '只读开关', okText: want ? '设为只读' : '取消只读' });
        if (!okd) return;
        rt.disabled = true;
        try {
          await App.runExclusive('settings', () => API.updateSource(sid, {
            read_only: want,
          }));
          UI.toast(want ? '已设为只读，写入会被拒绝' : '已恢复可写', 'ok', 5000);
          await App.loadSources();
          App.renderSidebar();
          // reload 会重画磁盘条 → 按钮文案跟着最新状态走
          await reload();
        } catch (e) {
          UI.toast('切换失败：' + U.esc(e && e.message ? e.message : e),
            'err', 7000);
          rt.disabled = false;
        }
      };
    }
  }

  /**
   * 选中条：显示已选数量 + 快速集合 + 批量操作。
   *
   * 全选按钮是**三态**的，和文件管理器一致：
   *   一个都没选 → 「全选本目录」
   *   选了部分   → 框显示**半选**（indeterminate），文案「已选 12 / 271」
   *   全选       → 框打勾，文案「取消全选」
   *
   * 之前没有半选概念：只选了 3 个和全选 271 个，按钮都显示成"全选本目录"，
   * 完全看不出当前是什么状态（用户说的"全选和部分选的显示是一样的"）。
   */
  function paintSelbar() {
    const bar = el.querySelector('#fb-selbar');
    if (!bar) return;
    const files = (listData && listData.files) || [];
    const picked = files.filter((t) => checked.has(did(t.path)));
    const n = picked.length;
    const total = files.length;
    const bytes = picked.reduce((a, t) => a + (t.size || 0), 0);
    const allOn = total > 0 && n === total;
    const someOn = n > 0 && n < total;
    const staged = (typeof Staging !== 'undefined') ? Staging.items.length : 0;
    const stagedDid = new Set(
      (typeof Staging !== 'undefined') ? Staging.items.map((i) => i.did) : []);
    // 已进候选区的文件单独计数：它们不该被重复加入
    const stagedHere = picked.filter((t) => stagedDid.has(did(t.path))).length;
    bar.innerHTML = `
      <label class="check" id="fb-all-wrap" style="margin:0">
        <input type="checkbox" id="fb-all" ${allOn ? 'checked' : ''}>
        <span id="fb-all-text">${
          allOn ? `取消全选（${total}）`
            : someOn ? `已选 ${n} / ${total}` : `全选本目录（${total}）`}</span>
      </label>
      <span class="sep-v"></span>
      <span class="muted">已选 <b>${n}</b> 个${n ? ' · ' + U.fmtBytes(bytes) : ''}${
        stagedHere ? ` · 其中 ${stagedHere} 个已在候选区` : ''}</span>
      <span class="sep-v"></span>
      <span class="muted" style="font-size:12.5px">快速集合：</span>
      <button class="btn sm ghost" id="fb-by-artist" ${n ? '' : 'disabled'}>同作者</button>
      <button class="btn sm ghost" id="fb-by-album" ${n ? '' : 'disabled'}>同专辑</button>
      <button class="btn sm ghost" id="fb-by-folder" ${n ? '' : 'disabled'}>整个文件夹</button>
      <div class="grow"></div>
      <button class="btn sm" id="fb-stage-add" ${n - stagedHere > 0 ? '' : 'disabled'}
              title="加进候选区，换目录也不会丢；已经在候选区里的会自动跳过">
        ${U.icon('plus')}<span>加入候选区</span>${
          n - stagedHere > 0 ? `<b>(${n - stagedHere})</b>` : ''}</button>
      <button class="btn sm" id="fb-tagedit" ${n ? '' : 'disabled'}
              title="改标题/作者/专辑等标签，可批量；写入前自动备份">
        ${U.icon('edit')}<span>编辑标签</span></button>
      <button class="btn sm primary" id="fb-collect" ${n ? '' : 'disabled'}
              title="把选中的歌收进一个新文件夹（可先看预演）">
        ${U.icon('plus')}<span>集合成文件夹…</span></button>
      <button class="btn sm danger" id="fb-trash" ${n ? '' : 'disabled'}>${U.icon('trash')}<span>移入回收站</span></button>
      <button class="btn sm primary" id="fb-move" data-exclusive ${n ? '' : 'disabled'}>${U.icon('folder')}<span>移动到…</span></button>
      <button class="btn sm" id="fb-xfer" ${n ? '' : 'disabled'}
        title="传到**另一个**数据源（复制→校验→再删源；失败源不会丢）">
        ${U.icon('db')}<span>跨库传输…</span></button>`;

    const allBox = bar.querySelector('#fb-all');
    if (allBox) {
      allBox.indeterminate = someOn;         // ← 半选
      allBox.onchange = () => {
        if (allBox.checked) files.forEach((t) => checked.add(did(t.path)));
        else checked.clear();
        refreshTreeChecks();
        paintList();
      };
    }
    const sel = () => files.filter((t) => checked.has(did(t.path)));
    bar.querySelector('#fb-by-artist').onclick = () => collectBy('artist', sel());
    bar.querySelector('#fb-by-album').onclick = () => collectBy('album', sel());
    bar.querySelector('#fb-by-folder').onclick = () => collectByFolder(sel());
    bar.querySelector('#fb-move').onclick = () => moveDialog(sel());
    const xferBtn = bar.querySelector('#fb-xfer');
    if (xferBtn) xferBtn.onclick = () => transferDialog(sel());
    bar.querySelector('#fb-collect').onclick = () => collectDialog(sel());
    bar.querySelector('#fb-trash').onclick = () => trashDialog(sel());
    bar.querySelector('#fb-stage-add').onclick = () => {
      // 已经在候选区里的不再重复添加（Staging 内部也会去重，但这里先过滤，
      // 好让 toast 的数字和按钮上的数字对得上）
      const fresh = sel().filter((t) => !stagedDid.has(did(t.path)));
      const skipped = sel().length - fresh.length;
      if (!fresh.length) {
        return UI.toast('选中的文件都已经在候选区里了', 'warn', 4000);
      }
      Staging.add(fresh);
      checked.clear();
      paintList();
      UI.toast(`已加入候选区 ${fresh.length} 个`
        + (skipped ? `（${skipped} 个已在候选区，跳过）` : ''), 'ok', 5000);
    };
    bar.querySelector('#fb-tagedit').onclick = () => {
      TagEditor.open({ source: sourceId(), paths: sel().map((t) => t.path) });
    };
  }

  /**
   * 把当前列表里"已在候选区"的行标上它的去向颜色。
   *
   * 这是"相同去向高亮同色"的入口：列表里一眼能看出哪几个文件会一起去同一个
   * 地方，不用挨个点开看。
   */
  function paintStagedMarks() {
    if (typeof Staging === 'undefined') return;
    const box = el.querySelector('#fb-list');
    if (!box) return;
    const map = new Map();
    Staging.items.forEach((i) => map.set(i.did, i));
    box.querySelectorAll('tr.fb-file').forEach((tr) => {
      const it = map.get(tr.dataset.did);
      const cb = tr.querySelector('input[data-check]');
      if (!it) {
        tr.classList.remove('staged');
        tr.style.removeProperty('--c');
        tr.style.removeProperty('background');
        tr.style.removeProperty('opacity');
        if (cb) {
          cb.disabled = false;
          cb.title = '';
        }
        const b = tr.querySelector('[data-stage-badge]');
        if (b) b.remove();
        return;
      }
      const c = Staging.colorOf(it.dst);
      tr.classList.add('staged');
      tr.style.setProperty('--c', c);
      tr.style.background = `color-mix(in srgb, ${c} 9%, transparent)`;
      // 已经在候选区里的文件：勾选框**禁用**并把勾去掉。
      // 之前只加了个小标签，勾选框照样能勾、能"再次加入"，
      // 看着像没生效（用户说的"选过的文件在上方仍然可选"）。
      if (cb) {
        cb.disabled = true;
        cb.checked = false;
        tr.classList.remove('sel');
        cb.title = '这个文件已经在候选区里了';
      }
      let badge = tr.querySelector('[data-stage-badge]');
      if (!badge) {
        const nameCell = tr.querySelector('.fb-name');
        if (nameCell) {
          badge = document.createElement('span');
          badge.dataset.stageBadge = '1';
          nameCell.appendChild(badge);
        }
      }
      if (badge) {
        badge.className = 'tag staged-tag';
        badge.style.cssText = `--c:${c};border-color:${c};color:${c};margin-left:6px`;
        badge.textContent = it.dst
          ? `已在候选区 → ${it.dstLabel || it.dst.split('/').pop() || it.dst}`
          : '已在候选区 · 待定';
      }
    });
    // 已进候选区的文件要从 `checked` 里剔除，否则"已选 N 个"会把它算进去，
    // 后面的批量移动/删除也会重复作用在它身上。
    // 只在**真的删掉了东西**时才刷新选中条 —— 否则
    // paintSelbar → paintList → paintStagedMarks 会互相触发、无限重绘。
    let dirty = false;
    map.forEach((_it, didKey) => {
      if (checked.delete(didKey)) dirty = true;
    });
    if (dirty && !paintStagedMarks._busy) {
      paintStagedMarks._busy = true;
      try { paintSelbar(); } finally { paintStagedMarks._busy = false; }
    }
  }

  /** 候选区里已就绪的文件按去向分组，交给批量移动执行。 */
  async function moveStaged() {
    if (typeof Staging === 'undefined') return;
    const groups = Staging.groupByDst();
    if (!groups.size) return UI.toast('候选区里还没有分配去向的文件', 'warn');
    // 去向数量不设上限：颜色只有 7 种，超了会循环复用，
    // 但每一列都独立显示、各带自己的目录路径，不会看混。
    const total = Staging.ready().length;
    const rows = Array.from(groups.entries()).map(([dst, list]) => {
      const c = Staging.colorOf(dst);
      return `<tr><td><span class="stg-swatch" style="background:${c}"></span></td>
        <td class="ell" style="max-width:230px">${U.esc(dst)}</td>
        <td class="num">${list.length}</td>
        <td class="num">${U.fmtBytes(list.reduce((a, i) => a + (i.size || 0), 0))}</td></tr>`;
    }).join('');
    const ok = await UI.confirm(
      `把候选区里 <b>${total}</b> 个文件按 ${groups.size} 个去向搬过去？<br>
       <span class="muted">同名冲突按"跳过"处理，绝不覆盖已有文件；只移动、不删除。</span>
       <div class="tbl-wrap" style="max-height:30vh;margin-top:8px">
         <table class="data"><thead><tr><th></th><th>目标目录</th>
           <th class="num">文件数</th><th class="num">体积</th></tr></thead>
           <tbody>${rows}</tbody></table></div>`,
      { title: '批量移动', okText: '开始移动' });
    if (!ok) return;

    const wait = UI.modal({
      title: '正在移动', size: 'md',
      body: `<div class="muted">共 ${total} 个文件、${groups.size} 个去向…</div>`,
      footer: '<button class="btn ghost" disabled>关闭</button>',
    });
    let moved = 0; let skipped = 0; let failed = 0;
    const problems = [];
    try {
      // 逐个去向执行：去向之间互不相干，串行更稳（也避免同名互相干扰）
      for (const [dst, list] of groups.entries()) {
        // 候选区的「执行移动」是真实写操作 → 走独占入口
        const r = await App.runExclusive('organize', () => API.organizeApply({
          source: (list[0].did || '').split('::')[0] || sourceId(),
          execute: true, confirm: true, policy: 'skip',
          plan: {
            moves: list.map((it) => ({
              path: it.path, name: it.name, dst_dir: dst,
              dst_path: `${dst}/${it.name}`, size: it.size || 0,
            })),
            summary: { total: list.length },
          },
        }));
        const s = r.summary || {};
        moved += r.moved || 0;
        skipped += r.skipped || 0;
        failed += r.failed || 0;
        (r.results || []).filter((x) => x.state === 'error')
          .slice(0, 20).forEach((x) => problems.push(x));
      }
      wait.body.innerHTML = `
        <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(110px,1fr))">
          ${UI.stat('已移动', moved)}
          ${UI.stat('跳过', skipped)}
          ${UI.stat('失败', failed)}
          ${UI.stat('去向数', groups.size)}
        </div>
        ${problems.length ? `<div class="tbl-wrap" style="max-height:200px;margin-top:10px">
          <table class="data"><thead><tr><th>文件</th><th>问题</th></tr></thead><tbody>
          ${problems.map((x) => `<tr><td class="mono ell">${U.esc(x.src || '')}</td>
            <td><span class="badge err">${U.esc(x.detail || x.state)}</span></td></tr>`).join('')}
          </tbody></table></div>` : ''}`;
      wait.footer.querySelector('button').disabled = false;
      wait.footer.querySelector('button').onclick = () => wait.close();
      UI.toast(`已移动 ${moved} 个文件` + (skipped ? `，跳过 ${skipped}` : ''),
        failed ? 'warn' : 'ok', 7000);
      Staging.clear();
      await load();
      App.loadSources && App.loadSources();
      App.loadSystem && App.loadSystem();
    } catch (e) {
      wait.body.innerHTML = `<div style="color:var(--danger)">${U.esc(e.message)}</div>`;
      wait.footer.querySelector('button').disabled = false;
      wait.footer.querySelector('button').onclick = () => wait.close();
    }
  }

  /** 按"同一作者/专辑"把库里的歌快速集合过来（不只是当前目录）。 */
  async function collectBy(field, sel) {
    if (!sel.length) return;
    const values = Array.from(new Set(sel.map((t) => t[field]).filter(Boolean)));
    if (!values.length) return UI.toast(`选中的文件没有${field === 'artist' ? '作者' : '专辑'}信息`, 'warn');
    let picked = values;
    if (values.length > 1) {
      const ctl = UI.modal({
        title: `按${field === 'artist' ? '作者' : '专辑'}集合`,
        size: 'sm',
        body: `<div class="muted" style="margin-bottom:8px">选中项包含多个值，挑一个：</div>
          ${values.map((v, i) => `<label class="check" style="display:flex;margin:4px 0">
            <input type="radio" name="cbv" value="${U.esc(v)}" ${i === 0 ? 'checked' : ''}> ${U.esc(v)}</label>`).join('')}`,
        footer: `<button class="btn ghost" data-act="no">取消</button>
                 <button class="btn primary" data-act="yes">集合</button>`,
      });
      const ok = await new Promise((res) => {
        ctl.footer.querySelector('[data-act=no]').onclick = () => { ctl.close(); res(false); };
        ctl.footer.querySelector('[data-act=yes]').onclick = () => {
          const v = ctl.body.querySelector('input[name=cbv]:checked');
          picked = v ? [v.value] : values;
          ctl.close(); res(true);
        };
      });
      if (!ok) return;
    }
    const sid = sourceId();
    const prefix = listData && listData.rel ? listData.rel : (listData ? relOf(cur) : '');
    const r = await API.fileSelect({ source: sid, prefix, by: field, value: picked[0] });
    if (!r.count) return UI.toast('未匹配到的曲目', 'warn');
    r.items.forEach((it) => checked.add(it.did));
    refreshTreeChecks();
    paintList();
    UI.toast(`已集合 ${r.count} 首（${field === 'artist' ? '作者' : '专辑'}：${picked[0]}）`, 'ok', 5000);
  }

  /** 把当前目录（含子目录）的所有曲目集合进来 */
  async function collectByFolder(sel) {
    const t = sel[0];
    if (!t) return;
    const dir = t.parent || cur;
    const sid = sourceId();
    const prefix = relOf(dir);
    const r = await API.fileSelect({ source: sid, prefix, by: null });
    r.items.forEach((it) => checked.add(it.did));
    paintList();
    UI.toast(`已集合整个文件夹 ${r.count} 首`, 'ok', 4500);
  }

  /* ------------------------------------------------------------ 跨库传输 */
  /**
   * 把选中的文件传到**另一个数据源**。
   *
   * 语义是「移动」而不是「复制」：成功之后源那份会进回收站。
   * 后端按 **复制 → 校验 → 再删源** 的顺序做，所以任何一步失败
   * 源都不会消失（详见 app/core/transfer.py）。
   *
   * 为什么单独一个入口而不是塞进「移动到…」：同库移动是**原子改名**
   * （快、无中间状态），跨库是**复制 + 校验 + 删源**（慢、要校验、
   * 失败处理完全不同）。混在一个对话框里，用户根本分不清自己按下去
   * 会发生哪一种。
   */
  async function transferDialog(sel) {
    if (!sel.length) return;
    const sid = sourceId();
    const me = State.sources.find((s) => s.id === sid) || {};
    const others = (State.sources || []).filter((s) => s.id !== sid);
    if (!others.length) {
      return UI.toast('只有一个数据源，跨库传输需要至少两个（可以再加一个 U 盘或另一块盘）',
        'warn', 7000);
    }
    const total = sel.reduce((a, t) => a + (t.size || 0), 0);
    // 可写且不是当前源的目标排前面，只读的置灰（但仍然列出来，
    // 让用户知道"那个源存在、只是不能写"）
    const targets = others.slice().sort((a, b) => {
      const ra = a.read_only ? 1 : 0, rb = b.read_only ? 1 : 0;
      return ra - rb;
    });
    let dstSid = (targets.find((s) => !s.read_only) || targets[0]).id;
    let dstDir = '';

    const optHtml = targets.map((s) => `
      <option value="${U.esc(s.id)}" ${s.id === dstSid ? 'selected' : ''}
        ${s.read_only ? 'data-ro="1"' : ''}>${U.esc(s.name)}（${U.esc(s.id)}）${
          s.read_only ? ' · 只读' : ''}${s.indexed ? ` · ${s.indexed} 首` : ''}</option>`)
      .join('');

    const ctl = UI.modal({
      title: `跨库传输 ${sel.length} 个文件`,
      size: 'lg',
      body: `
        <div class="filtnote" style="margin-bottom:12px">
          把 <b>${sel.length}</b> 个文件（${U.fmtBytes(total)}）从
          <b>${U.esc(me.name || sid)}</b> 传到另一个数据源。
          <br><span class="muted">顺序是：<b>先复制 → 校验通过 → 再删源</b>。
          任何一步失败源都不会消失；删掉的源会进回收站，可以再拿回来。</span>
        </div>
        <div class="field"><label>目标数据源</label>
          <select id="xf-src">${optHtml}</select>
          <div class="hint" id="xf-ro-hint"></div></div>
        <div class="mv-dest-row">
          <div class="mv-dest-label">目标目录</div>
          <div class="mv-dest-box">
            <div class="mono mv-dest-path" id="xf-dest">（目标源根目录）</div>
            <button class="btn sm" id="xf-pick">${U.icon('search')}<span>浏览 / 搜索目录…</span></button>
          </div>
          <div class="muted mv-dest-hint">
            可以不选 —— 不选就放到目标数据源的根目录。
            文件名保持不变（跨库搬运不该顺手改名）。
          </div>
        </div>
        <div class="row wrap" style="margin-top:12px">
          <label class="check"><span class="muted">同名冲突</span>
            <select id="xf-policy" style="width:auto">
              <option value="skip" selected>跳过（目标已有同名文件就不动它）</option>
              <option value="rename">自动改名（加 (2)）</option>
              <option value="overwrite">覆盖目标</option>
            </select></label>
          <label class="check"><input type="checkbox" id="xf-hash">
            额外比对内容哈希（更慢；默认只比大小）</label>
        </div>
        <div id="xf-pre" class="muted" style="margin-top:10px"></div>`,
      footer: `<button class="btn ghost" data-act="dry">${U.icon('eye')}<span>预检</span></button>
               <div class="grow"></div>
               <button class="btn ghost" data-act="no">取消</button>
               <button class="btn primary" data-act="yes">
                 ${U.icon('check')}<span>开始传输</span></button>`,
    });

    const srcSel = ctl.body.querySelector('#xf-src');
    const destEl = ctl.body.querySelector('#xf-dest');
    const roHint = ctl.body.querySelector('#xf-ro-hint');
    const preBox = ctl.body.querySelector('#xf-pre');
    const yesBtn = ctl.footer.querySelector('[data-act=yes]');

    const syncTarget = () => {
      dstSid = srcSel.value;
      const s = targets.find((x) => x.id === dstSid) || {};
      const ro = !!s.read_only;
      yesBtn.disabled = ro;
      roHint.innerHTML = ro
        ? '<span style="color:var(--danger)">这个数据源是只读的，不能作为传输目标。'
          + '要传过去请先在「数据源」里取消它的只读勾选。</span>'
        : '';
      const el = destEl;
      el.textContent = dstDir || '（目标源根目录）';
    };
    srcSel.onchange = syncTarget;
    syncTarget();

    ctl.body.querySelector('#xf-pick').onclick = async () => {
      const r = await FolderPicker.open({
        source: dstSid, start: dstDir,
        title: `选择目标目录（在 ${dstSid} 里）`,
      });
      if (r === null) return;
      dstDir = r || '';
      destEl.textContent = dstDir || '（目标源根目录）';
    };

    const payloadOf = (execute) => ({
      from: sid, to: dstSid, items: sel.map((t) => t.path),
      dst_dir: dstDir,
      policy: ctl.body.querySelector('#xf-policy').value,
      verify: true,
      verify_hash: ctl.body.querySelector('#xf-hash').checked,
      execute, confirm: execute,
    });

    const runPre = async () => {
      preBox.innerHTML = '<span class="muted">正在预检…</span>';
      try {
        const r = await API.fileTransfer(payloadOf(false));
        const items = r.items || [];
        const conf = r.would_conflict || 0;
        const miss = r.missing || 0;
        const bytes = items.reduce((a, x) => a + (x.size || 0), 0);
        preBox.innerHTML = `
          <div class="card" style="padding:8px 10px">
            预检通过：<b>${items.length}</b> 个文件 · ${U.fmtBytes(bytes)}
            ${conf ? ` · <span style="color:var(--warn)">${conf} 个目标已存在同名</span>` : ''}
            ${miss ? ` · <span style="color:var(--danger)">${miss} 个源文件已找不到</span>` : ''}
            <div class="muted" style="font-size:11.5px;margin-top:4px">
              目标：${U.esc((targets.find((x) => x.id === dstSid) || {}).name || dstSid)}
              ${dstDir ? ' / ' + U.esc(dstDir) : '（根目录）'}
            </div>
          </div>`;
        return r;
      } catch (e) {
        preBox.innerHTML = `<div style="color:var(--danger)">${U.esc(e.message)}</div>`;
        return null;
      }
    };

    ctl.footer.querySelector('[data-act=dry]').onclick = () => runPre();
    ctl.footer.querySelector('[data-act=no]').onclick = () => ctl.close();

    yesBtn.onclick = async () => {
      const policy = ctl.body.querySelector('#xf-policy').value;
      const dstName = (targets.find((x) => x.id === dstSid) || {}).name || dstSid;
      const okd = await UI.confirm(
        `确认把这 <b>${sel.length}</b> 个文件（${U.fmtBytes(total)}）从
         <b>${U.esc(me.name || sid)}</b> 传输到 <b>${U.esc(dstName)}</b>${
           dstDir ? ` 的 <span class="mono">${U.esc(dstDir)}</span>` : ' 的根目录'}？<br>
         <span class="muted">顺序：先复制 → 校验 → 再删源。同名冲突策略：${
           { skip: '跳过', rename: '自动改名', overwrite: '覆盖' }[policy]}。
         删掉的源会进回收站，可以再拿回来。</span>`,
        { title: '跨库传输确认', okText: '开始传输', danger: true });
      if (!okd) return;
      yesBtn.disabled = true;
      const old = yesBtn.innerHTML;
      yesBtn.innerHTML = '<span>传输中…</span>';
      try {
        const r = await App.runExclusive('transfer', () => API.fileTransfer(payloadOf(true)));
        ctl.close();
        // 结果里 blocked / error 要单独说清楚，不能只报"成功 N 个"
        const bad = (r.results || []).filter((x) => x.state !== 'done');
        UI.toast(`传输完成：成功 ${r.moved || 0} 个`
          + (r.conflict ? ` · 跳过 ${r.conflict}` : '')
          + (r.blocked ? ` · 被拒 ${r.blocked}` : '')
          + (r.failed ? ` · 失败 ${r.failed}` : '')
          + ` · ${U.fmtBytes(r.bytes || 0)}`,
          bad.length ? 'warn' : 'ok', 9000);
        if (bad.length) {
          const lines = bad.slice(0, 20).map((x) =>
            `<div class="mono" style="font-size:11.5px">${U.esc(U.base(x.src_path))}`
            + ` — ${U.esc(x.detail || x.state)}</div>`).join('');
          UI.modal({
            title: `有 ${bad.length} 个文件没能传输`,
            size: 'md',
            body: `<div class="muted" style="margin-bottom:8px">
                下面这些**源文件都还在**，没有被删掉。处理完可以再传一次。</div>${lines}`,
            footer: '<button class="btn primary" data-act="ok">知道了</button>',
            onMount(c) {
              c.footer.querySelector('[data-act=ok]').onclick = () => c.close();
            },
          });
        }
        await App.loadSources();
        App.renderSidebar();
        await reload();
      } catch (e) {
        UI.toast('传输失败：' + U.esc(e.message), 'err', 9000);
        yesBtn.disabled = false;
        yesBtn.innerHTML = old;
      }
    };
  }

  /* ------------------------------------------------------------ 移动 */
  async function moveDialog(sel) {
    if (!sel.length) return;
    const sid = sourceId();
    const total = sel.reduce((a, t) => a + (t.size || 0), 0);
    let dest = cur;

    const ctl = UI.modal({
      title: `移动 ${sel.length} 个文件`,
      size: 'lg',
      body: `
        <div class="filtnote" style="margin-bottom:12px">
          将把 <b>${sel.length}</b> 个文件（${U.fmtBytes(total)}）移动到下面选中的目录。
          移动后可随时再移回来；同名文件按你选的策略处理。
        </div>
        <div class="mv-dest-row">
          <div class="mv-dest-label">目标目录</div>
          <div class="mv-dest-box">
            <div class="mono mv-dest-path" id="mv-dest">${U.esc(cur)}</div>
            <button class="btn sm" id="mv-pick">${U.icon('search')}<span>浏览 / 搜索目录…</span></button>
            <button class="btn sm ghost" id="mv-new">新建子目录…</button>
          </div>
          <div class="muted mv-dest-hint">
            目标目录下的子目录数：<b id="mv-subcount">-</b>。
            点「浏览 / 搜索目录…」可以直接搜名字跳过去，不用一层层翻。
          </div>
        </div>
        <div class="row" style="margin-top:12px">
          <span class="muted">同名冲突</span>
          <select id="mv-policy" style="width:auto">
            <option value="skip" selected>跳过（目标已有同名文件就不动它）</option>
            <option value="rename">自动改名（加 (2)）</option>
            <option value="overwrite">覆盖目标</option>
          </select>
        </div>`,
      footer: `<button class="btn ghost" data-act="dry">${U.icon('eye')}<span>预演</span></button>
               <div class="grow"></div>
               <button class="btn ghost" data-act="no">取消</button>
               <button class="btn primary" data-act="yes">${U.icon('check')}<span>执行移动</span></button>`,
    });

    const destInput = ctl.body.querySelector('#mv-dest');
    const subcount = ctl.body.querySelector('#mv-subcount');

    const showDest = () => { destInput.textContent = dest || '(未选择)'; };
    showDest();

    // 显示目标目录里有多少子目录，让人心里有数
    (async () => {
      try {
        const d = await API.fileList(sid, dest, 'count', 'desc');
        subcount.textContent = `${d.dirs.length} 个`;
      } catch (e) { subcount.textContent = '-'; }
    })();

    ctl.body.querySelector('#mv-pick').onclick = async () => {
      const picked = await FolderPicker.open({
        source: sid,
        start: dest,
        title: `选择目标目录（移动 ${sel.length} 个文件）`,
      });
      if (picked === null) return;
      dest = picked;
      showDest();
      try {
        const d = await API.fileList(sid, dest, 'count', 'desc');
        subcount.textContent = `${d.dirs.length} 个`;
      } catch (e) { subcount.textContent = '-'; }
    };
    ctl.body.querySelector('#mv-new').onclick = async () => {
      const name = await UI.prompt('新建子目录', `在 ${dest} 下新建`, '');
      if (!name) return;
      dest = dest.replace(/\/+$/, '') + '/' + name;
      showDest();
      subcount.textContent = '0 个（新建，执行移动时会自动创建）';
    };

    const buildSteps = () => sel.map((t) => ({
      src: sid, src_path: t.path,
      dst_path: (dest.replace(/\/+$/, '') || '') + '/' + t.name,
    }));

    ctl.footer.querySelector('[data-act=no]').onclick = () => ctl.close();
    ctl.footer.querySelector('[data-act=dry]').onclick = async () => {
      try {
        const r = await API.fileMove({ steps: buildSteps(), execute: false });
        UI.toast(`预演：将移动 ${r.summary.planned || 0} 个，跳过 ${r.summary.skipped || 0} 个，`
          + `错误 ${r.summary.error || 0} 个`, 'info', 8000);
      } catch (e) { UI.toast('预演失败：' + U.esc(e.message), 'err'); }
    };
    ctl.footer.querySelector('[data-act=yes]').onclick = async () => {
      const policy = ctl.body.querySelector('#mv-policy').value;
      const steps = buildSteps();
      if (dest === cur) return UI.toast('目标目录和当前目录相同', 'warn');
      const ok = await UI.confirm(
        `确认把 <b>${steps.length}</b> 个文件移动到<br><span class="mono">${U.esc(dest)}</span>？<br>
         <span class="muted">同名冲突策略：${
           { skip: '跳过', rename: '自动改名', overwrite: '覆盖' }[policy]}。
         复制是"先写临时文件再改名"，中断不会留半截文件。</span>`,
        { title: '移动确认', okText: '开始移动' });
      if (!ok) return;
      ctl.close();
      await runMove(steps, policy);
    };
  }

  async function runMove(steps, policy) {
    const ctl = UI.modal({
      title: '移动文件',
      size: 'md',
      body: '<div class="muted">正在移动…</div>',
      footer: '<button class="btn ghost" data-act="close" disabled>关闭</button>',
    });
    const box = ctl.body;
    try {
      const r = await API.fileMove({ steps, execute: true, confirm: true, policy });
      const bad = r.results.filter((x) => x.state === 'error');
      box.innerHTML = `
        <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(110px,1fr))">
          ${UI.stat('成功', r.summary.done || 0)}
          ${UI.stat('跳过', r.summary.skipped || 0)}
          ${UI.stat('失败', bad.length)}
          ${UI.stat('移动体积', U.fmtBytes(r.summary.bytes_moved || 0))}
        </div>
        <div class="tbl-wrap" style="max-height:300px;margin-top:10px">
          <table class="data"><thead><tr><th>文件</th><th>结果</th></tr></thead><tbody>
          ${r.results.slice(0, 300).map((x) => `<tr>
            <td class="mono ell" title="${U.esc(x.src || '')}">${U.esc((x.src || '').split('/').pop())}</td>
            <td>${x.state === 'done'
              ? `<span class="badge ok">已移动</span><span class="muted mono"> → ${U.esc(x.target || '')}</span>`
              : `<span class="badge ${x.state === 'skipped' ? 'warn' : 'err'}">${U.esc(x.detail || x.state)}</span>`}</td>
          </tr>`).join('')}</tbody></table></div>`;
      ctl.footer.querySelector('[data-act=close]').disabled = false;
      if (r.summary.done) UI.toast(`已移动 ${r.summary.done} 个文件`, 'ok', 6000);
      if (bad.length) UI.toast(`${bad.length} 个文件移动失败，请看详情`, 'err', 8000);
      checked.clear();
      await load();
      App.loadSystem && App.loadSystem();
    } catch (e) {
      box.innerHTML = `<div style="color:var(--danger)">${U.esc(e.message)}</div>`;
      ctl.footer.querySelector('[data-act=close]').disabled = false;
    }
    ctl.footer.querySelector('[data-act=close]').onclick = () => ctl.close();
  }

  /* ------------------------------------------------------------ 删除（复用既有执行器） */
  async function trashDialog(sel) {
    if (!sel.length) return;
    const sid = sourceId();
    const bytes = sel.reduce((a, t) => a + (t.size || 0), 0);
    const ok = await UI.confirm(
      `把 <b>${sel.length}</b> 个文件（${U.fmtBytes(bytes)}）移入回收站？<br>
       <span class="muted">删除 = 移动到 <span class="mono">.music-sync-trash/日期/</span>，可随时恢复。</span>`,
      { title: '移入回收站', okText: '确认移入', danger: true });
    if (!ok) return;
    // ★ 同一路径只发一次：列表勾选 + 目录树勾选可能选中同一个文件，
    //   发两条会让第二条报"不存在"，界面显示"失败 N"（其实已经删掉了）。
    const seen = new Set();
    const steps = sel.filter((t) => {
      const k = t.path;
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    }).map((t) => ({
      op: 'trash', src: sid, src_path: t.path, dst: sid, dst_path: t.path,
      size: t.size || 0, risk: 'high', kind: 'manual',
    }));
    try {
      const r = await App.runExclusive('trash', () => API.execute(
        { steps, execute: true, confirm: true, policy: 'skip' }));
      checked.clear();
      refreshTreeChecks();
      paintSelbar();
      const _skipped = r.summary.skipped || 0;
      UI.toast(`已移入回收站 ${r.summary.done || 0} 个`
        + (_skipped ? `，跳过 ${_skipped} 个（已不存在）` : '')
        + (r.summary.error ? `，失败 ${r.summary.error} 个` : ''),
        r.summary.error ? 'warn' : 'ok', 6000);
      await load();
      // 侧栏/数据源的曲目数要跟着变（只 load() 不会刷新计数）
      try { await App.loadSources(); App.renderSidebar(); } catch (e) {}
    } catch (e) {
      // ★ 必须有 catch：`runExclusive` 在别的页面占着独占位时会**抛 409**
      //   （设计如此，不排队）。不接住的话就是未捕获的 rejection，
      //   界面停在半截状态，用户不知道删除到底成没成。
      UI.toast('移入回收站失败：' + (e && e.message ? e.message : e), 'err', 9000);
      paintSelbar();
    }
    App.loadSystem && App.loadSystem();
  }

  /** 供其它视图调用：弹出可搜索的目录选择器（移动目标） */
  function pickFolder(sid, start) {
    return FolderPicker.open({ source: sid, start: start || '' });
  }

  /** 供外部（侧栏点数据源）调用：清掉缓存并按当前源重新加载。 */
  function reload() {
    folderData = null;
    listData = null;
    cur = '';
    checked.clear();
    expanded.clear();
    if (!el || !el.querySelector('#fb-src')) return;
    const sel = el.querySelector('#fb-src');
    if (sel.value !== sourceId()) sel.value = sourceId();
    load();
  }

  /* ------------------------------------------------------------ 文件夹操作 */

  /** 右键菜单：对某个子目录能做的事 */
  function folderMenuItems(dir) {
    const sid = sourceId();
    return [
      { label: '打开', icon: 'folder', onPick: () => go(dir.path) },
      { label: '新建子目录…', icon: 'plus', onPick: () => createFolder(dir.path) },
      { sep: true },
      { label: '重命名…', icon: 'edit', onPick: () => renameFolder(dir) },
      { label: '移动到…', icon: 'link', onPick: () => moveFolder(dir) },
      { label: '清理空文件夹…', icon: 'bars', onPick: () => emptyFolderCleanup(dir.path) },
      { label: '智能整理这个文件夹…', icon: 'settings',
        onPick: () => Organizer.open({ source: sid, path: dir.path, name: dir.name }) },
      { sep: true },
      { label: '删除（移入回收站）', icon: 'trash', danger: true,
        onPick: () => deleteFolder(dir) },
    ];
  }

  /** 当前目录自己的菜单（在空白处右键） */
  function currentDirMenuItems() {
    if (!listData) return [];
    const rel = listData.rel || '';
    const isRoot = !rel;
    const items = [
      { label: '新建子目录…', icon: 'plus', onPick: () => createFolder(cur) },
      { label: '刷新', icon: 'refresh', onPick: () => load() },
    ];
    if (!isRoot) {
      const self = { path: cur, name: rel.split('/').pop(), rel };
      items.push({ sep: true });
      items.push({ label: '重命名当前目录…', icon: 'edit',
                   onPick: () => renameFolder(self) });
      items.push({ label: '移动当前目录到…', icon: 'link',
                   onPick: () => moveFolder(self) });
      items.push({ label: '清理当前目录下的空文件夹…', icon: 'bars',
                   onPick: () => emptyFolderCleanup(self.path) });
      items.push({ sep: true });
      items.push({ label: '删除当前目录（移入回收站）', icon: 'trash', danger: true,
                   onPick: () => deleteFolder(self) });
    }
    return items;
  }

  /* ------------------------------------------------------------ 一键清空空文件夹 */
  /**
   * 扫描并清理空文件夹。
   *
   * 取代原来的「解散文件夹」：那个功能要求为**每个文件**单独指定去向，
   * 为了清几个空壳目录要用户做十几步判断，操作极重。实际需求往往只是
   * "把扫出来的空目录壳删掉"，所以现在：
   *
   *   1. 扫描出范围内所有（递归意义上的）空文件夹；
   *   2. 列表默认**全选**，用户可以取消勾选任何想保留的；
   *   3. 一次删掉剩下的。
   *
   * 删除是**不可逆**的，但只删"确认为空"的目录 —— 后端会再验一次，
   * 非空的一律跳过并如实报告，避免勾错导致文件被连带删除。
   *
   * @param {string} [startPath] 扫描起点，省略则从当前目录
   */
  async function emptyFolderCleanup(startPath) {
    const sid = sourceId();
    const start = startPath || cur || '';
    let data;
    try {
      data = await API.folderEmptyScan(sid, start);
    } catch (e) {
      return UI.toast('扫描空文件夹失败：' + e.message, 'err', 8000);
    }
    const folders = data.folders || [];
    if (!folders.length) {
      return UI.toast(start
        ? `「${data.start || '当前目录'}」下没有空文件夹`
        : '整个数据源里没有空文件夹', 'ok', 5000);
    }
    // 默认全部勾选（也就是全部清理）
    const keep = new Set();

    const ctl = UI.modal({
      title: '清理空文件夹',
      size: 'lg',
      body: `
        <div class="filtnote" style="margin-bottom:10px">
          在 <b>${U.esc(data.start || '数据源根目录')}</b> 下找到
          <b>${folders.length}</b> 个空文件夹（里面没有音频文件，也没有非空子目录）。
          <br>默认全部清理；<b>取消勾选即可保留</b>。
        </div>
        <div class="row" style="margin-bottom:8px">
          <button class="btn sm ghost" id="ec-all">全选</button>
          <button class="btn sm ghost" id="ec-none">全不选</button>
          <button class="btn sm ghost" id="ec-shallow">只保留最外层</button>
          <div class="grow"></div>
          <span class="muted" id="ec-info"></span>
        </div>
        <div class="tbl-wrap" style="max-height:46vh">
          <table class="data"><thead><tr>
            <th style="width:34px"></th><th>目录</th>
            <th class="num">层级</th><th>所在位置</th>
          </tr></thead><tbody>
          ${folders.map((f) => `
            <tr data-p="${U.esc(f.path)}">
              <td><input type="checkbox" data-ec="${U.esc(f.path)}" checked></td>
              <td class="ell" style="max-width:300px" title="${U.esc(f.rel)}">
                ${U.esc(f.name)}</td>
              <td class="num">${f.depth}</td>
              <td class="ell muted" style="max-width:260px;font-size:11.5px"
                  title="${U.esc(f.parent)}">${U.esc(f.parent || '（根目录）')}</td>
            </tr>`).join('')}
          </tbody></table>
        </div>`,
      footer: `<span class="muted grow" id="ec-sum"></span>
               <button class="btn ghost" data-act="no">取消</button>
               <button class="btn danger solid" data-act="yes" id="ec-go"></button>`,
    });

    const sumBox = ctl.footer.querySelector('#ec-sum');
    const goBtn = ctl.footer.querySelector('#ec-go');
    const infoBox = ctl.body.querySelector('#ec-info');
    const boxes = () => Array.from(ctl.body.querySelectorAll('[data-ec]'));

    function paint() {
      const total = boxes().length;
      const on = boxes().filter((cb) => cb.checked).length;
      const off = total - on;
      sumBox.textContent = off
        ? `将清理 ${on} 个，保留 ${off} 个`
        : `将清理全部 ${on} 个空文件夹`;
      infoBox.textContent = off ? `已保留 ${off} 个` : '';
      goBtn.textContent = `清理 ${on} 个`;
      goBtn.disabled = on === 0;
      // 取消勾选的行淡一点，一眼看出哪些被保留了
      ctl.body.querySelectorAll('tr[data-p]').forEach((tr) => {
        const cb = tr.querySelector('[data-ec]');
        tr.style.opacity = cb && cb.checked ? '1' : '.45';
      });
    }

    boxes().forEach((cb) => { cb.onchange = paint; });
    ctl.body.querySelector('#ec-all').onclick = () => {
      boxes().forEach((cb) => { cb.checked = true; }); paint();
    };
    ctl.body.querySelector('#ec-none').onclick = () => {
      boxes().forEach((cb) => { cb.checked = false; }); paint();
    };
    // "只保留最外层"：把最浅的那一层去掉勾选，其余全清
    ctl.body.querySelector('#ec-shallow').onclick = () => {
      const minD = Math.min(...folders.map((f) => f.depth));
      boxes().forEach((cb) => {
        const f = folders.find((x) => x.path === cb.dataset.ec);
        cb.checked = !(f && f.depth === minD);
      });
      paint();
      UI.toast(`已保留最外层的 ${folders.filter((f) => f.depth === minD).length} 个`,
        'ok', 4000);
    };

    paint();

    ctl.footer.querySelector('[data-act=no]').onclick = () => ctl.close();
    goBtn.onclick = async () => {
      const pick = boxes().filter((cb) => cb.checked).map((cb) => cb.dataset.ec);
      if (!pick.length) return;
      const ok = await UI.confirm(
        `确定删除这 <b>${pick.length}</b> 个空文件夹？<br>
         <span class="muted">只删确认为空的目录，非空的一律跳过。
         此操作不可撤销（但里面本来就没有文件）。</span>`,
        { title: '清理空文件夹', okText: '删除', danger: true });
      if (!ok) return;
      goBtn.disabled = true;
      goBtn.textContent = '清理中…';
      try {
        // 删目录是写操作 → 独占
        const r = await App.runExclusive('cleanup', () => API.folderEmptyRemove({
          source: sid, paths: pick, confirm: true,
        }));
        ctl.close();
        const parts = [`已删除 ${r.removed_count} 个空文件夹`];
        if (r.skipped_count) parts.push(`跳过 ${r.skipped_count} 个`);
        UI.toast(parts.join(' · '), 'ok', 6000);
        if (r.skipped_count) {
          UI.modal({
            title: '部分目录已跳过',
            body: `<div class="muted" style="margin-bottom:8px">
                这些目录在删除前复查时发现并非空目录，已原样保留：</div>
              <div class="tbl-wrap" style="max-height:40vh"><table class="data">
                <thead><tr><th>目录</th><th>原因</th></tr></thead><tbody>
                ${r.skipped.map((s) => `<tr><td class="mono">${U.esc(s.path)}</td>
                  <td class="muted">${U.esc(s.reason)}</td></tr>`).join('')}
              </tbody></table></div>`,
            footer: '<button class="btn primary">知道了</button>',
          });
        }
        await load();          // 刷新目录树 / 文件列表
      } catch (e) {
        goBtn.disabled = false;
        paint();
        UI.toast('清理失败：' + e.message, 'err', 8000);
      }
    };
  }

  /**
   * 从文件浏览打开查重。
   *
   * 切到「查重」视图后**自动开始扫描** —— 用户点这个按钮的意图就是要查，
   * 再让他去点一次"开始扫描"是多余的。扫完可以直接切回文件浏览，
   * 视图状态会保留（App.setView 不销毁已 mount 的视图）。
   */
  function openDedup() {
    App.setView('duplicates');
    const api = App.viewApi && App.viewApi('duplicates');
    if (api && typeof api.run === 'function') {
      // 等一帧让视图渲染完，再自动跑
      setTimeout(() => {
        try { api.run(); } catch (e) { /* 让视图自己提示错误 */ }
      }, 60);
    }
  }

  /**
   * 拖拽导入：把文件从资源管理器拖进文件列表，直接导入**当前目录**。
   *
   * 用的是和上传入库同一套接口（`/api/upload` → `/api/upload/commit`），
   * 所以同样会走查重、同样只移动不删除。区别只是：
   *   · 目标目录由"当前所在目录"决定，不需要自动分配；
   *   · 拖进来的文件立即入库，不用再走三步向导。
   *
   * 落点始终是**当前浏览的目录**，并且会在拖拽时明确显示出来 ——
   * 拖拽最容易出的事故就是"松手之后不知道放哪了"。
   */
  function mountDropImport() {
    // `mount()` 只负责记下 el，列表容器是 `render()` 里 innerHTML 出来的。
    // 如果在这里直接 querySelector 会拿到 null、然后**静默 return**，
    // 拖拽功能就永远不生效（实测就是这样：overlay 根本没被创建）。
    // 所以改成把事件挂在容器元素 `el` 上、用事件委托判断落点，
    // 并且在第一次拖动时才真正建 overlay。
    let list = el.querySelector('#fb-list');
    let depth = 0;                      // dragenter/dragleave 会成对冒泡，用计数判断
    let overlay = null;

    const ensure = () => {
      list = el.querySelector('#fb-list');
      if (!list) return null;
      if (!overlay || !overlay.isConnected) {
        overlay = document.createElement('div');
        overlay.className = 'fb-drop-overlay hidden';
        list.appendChild(overlay);
      }
      return list;
    };

    const showOverlay = () => {
      const box = ensure();
      if (!box) return;
      const dest = (listData && listData.rel) || '（根目录）';
      overlay.innerHTML = `<div class="fb-drop-box">
        <div class="big">${U.icon('download', 'ico')}</div>
        <div>松手即导入到 <b>${U.esc(dest)}</b></div>
        <div class="muted">支持多选文件；同名冲突按"跳过"处理，绝不覆盖</div>
      </div>`;
      overlay.classList.remove('hidden');
    };
    const hideOverlay = () => {
      if (overlay) overlay.classList.add('hidden');
      depth = 0;
    };

    const hasFiles = (e) => {
      const dt = e.dataTransfer;
      if (!dt) return false;
      if (dt.types && Array.from(dt.types).includes('Files')) return true;
      return dt.types && Array.from(dt.types).includes('application/x-moz-file');
    };

    /** 判断事件是否发生在文件列表区域内（用委托，容器后来才存在也没关系）。 */
    const inList = (e) => {
      const box = el.querySelector('#fb-list');
      return !!(box && e.target && (e.target === box || box.contains(e.target)));
    };

    el.addEventListener('dragenter', (e) => {
      if (!el.querySelector('#fb-list')) return;
      if (!hasFiles(e) || !inList(e)) return;
      e.preventDefault();
      depth++;
      showOverlay();
    });
    el.addEventListener('dragover', (e) => {
      if (!hasFiles(e) || !inList(e)) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
    });
    el.addEventListener('dragleave', (e) => {
      if (!hasFiles(e)) return;
      depth--;
      if (depth <= 0) hideOverlay();
    });
    el.addEventListener('drop', async (e) => {
      if (!hasFiles(e) || !inList(e)) return;
      e.preventDefault();
      hideOverlay();
      const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
      if (!files.length) return;
      await importDropped(files);
    });
  }

  /** 把拖进来的文件上传并入库到当前目录。 */
  async function importDropped(files) {
    const sid = sourceId();
    const dest = cur || '';
    const destLabel = (listData && listData.rel) || '（根目录）';
    const audio = files.filter((f) => /\.(flac|mp3|m4a|aac|ogg|opus|wav|aiff?|alac)$/i.test(f.name));
    const ignored = files.length - audio.length;
    if (!audio.length) {
      return UI.toast('拖进来的文件里没有可识别的音频格式', 'warn', 6000);
    }
    const bytes = audio.reduce((a, f) => a + f.size, 0);
    const ok = await UI.confirm(
      `把这 <b>${audio.length}</b> 个文件导入到 <b>${U.esc(destLabel)}</b>？<br>
       <span class="muted">共 ${U.fmtBytes(bytes)}。
       ${ignored ? `另有 ${ignored} 个非音频文件会被忽略。<br>` : ''}
       同名文件按"跳过"处理，绝不覆盖已有内容。</span>`,
      { title: '拖拽导入', okText: '导入' });
    if (!ok) return;

    const prog = UI.modal({
      title: '导入中',
      body: `<div class="muted" id="di-info">正在上传…</div>
             <div class="pbar" style="margin-top:8px"><i id="di-bar" style="width:0%"></i></div>`,
      footer: '<button class="btn ghost" id="di-cancel" disabled>请稍候…</button>',
    });
    const info = prog.body.querySelector('#di-info');
    const bar = prog.body.querySelector('#di-bar');

    try {
      // 1) 上传到暂存区（分批，避免一次请求过大）
      //
      // ★ 批次号只能取 `/api/upload` **本次返回**的 `r.batch`。
      //   踩过：原来从 `r.files[0].batch` 取（记录里根本没这个字段 → undefined），
      //   再兜底成 `(await API.uploadBatches()).batches[0].id` ——
      //   那个列表是按更新时间倒序的**最新批次**，而且键叫 `batch` 不叫 `id`。
      //   于是多标签页/多次上传之后，兜底会拿到**别人的批次**，
      //   然后按本次的 dst 把它提交掉 —— 用户看到的是"我明明拖到 A 目录，
      //   怎么跑到别处去了"。这是会动错文件的问题，必须只认本次的批次号。
      const batchFiles = [];
      let batch = '';
      const CHUNK = 20;
      for (let i = 0; i < audio.length; i += CHUNK) {
        const part = audio.slice(i, i + CHUNK);
        const fd = new FormData();
        part.forEach((f) => fd.append('files', f, f.name));
        fd.append('source', sid);
        const r = await API.uploadFiles(fd);
        if (r.batch) batch = r.batch;      // 只认本次
        (r.files || []).forEach((x) => batchFiles.push(x));
        const pct = Math.round(Math.min(1, (i + part.length) / audio.length) * 90);
        bar.style.width = pct + '%';
        info.textContent = `已上传 ${Math.min(i + CHUNK, audio.length)} / ${audio.length}`;
      }
      if (!batch) throw new Error('上传后没拿到批次号');
      if (!batchFiles.length) {
        // 全部同名被跳过时也可能走到这 —— 说清楚，不要拿别的批次顶上
        throw new Error('这些文件都没有被收进暂存区（可能全部同名被跳过）');
      }

      // 2) 入库到当前目录：直接指定 moves，不走"自动分配"
      info.textContent = '正在入库…';
      bar.style.width = '95%';
      const moves = batchFiles.map((x) => ({ rel: x.rel, dst: dest }));
      const res = await API.uploadCommit({
        batch, source: sid, moves, confirm: true, policy: 'skip', scan: true,
      });
      bar.style.width = '100%';
      prog.close();
      const s = res.summary || {};
      const parts = [`导入 ${s.done || 0} 个`];
      if (s.skipped) parts.push(`跳过 ${s.skipped} 个（已存在）`);
      if (s.error) parts.push(`失败 ${s.error} 个`);
      UI.toast(parts.join(' · '), s.error ? 'warn' : 'ok', 7000);
      await load();
      await App.loadSources();
      App.renderSidebar();
    } catch (e) {
      prog.close();
      UI.toast('导入失败：' + e.message, 'err', 9000);
    }
  }

  async function createFolder(parentAbs) {
    const name = await UI.prompt('新建子目录', '目录名', '');
    if (!name) return;
    try {
      await API.folderCreate({ source: sourceId(), parent: parentAbs, name });
      UI.toast(`已创建「${name}」`, 'ok');
      expanded.add(relOf(parentAbs));
      await load();
    } catch (e) { UI.toast('创建失败：' + U.esc(e.message), 'err', 7000); }
  }

  async function renameFolder(dir) {
    const name = await UI.prompt('重命名目录', `把「${dir.name}」改成`, dir.name);
    if (!name || name === dir.name) return;
    try {
      const r = await API.folderRename({ source: sourceId(), path: dir.path, name });
      UI.toast(`已重命名为「${name}」`
        + (r.indexed ? `（同步索引 ${r.indexed} 首）` : ''), 'ok', 5000);
      // 如果正在看的就是被改名的目录，跟着走
      if (listData && (cur === dir.path || cur.startsWith(dir.path + '/'))) {
        cur = r.path + cur.slice(dir.path.length);
      }
      await load();
    } catch (e) { UI.toast('重命名失败：' + U.esc(e.message), 'err', 8000); }
  }

  async function moveFolder(dir) {
    const sid = sourceId();
    const target = await FolderPicker.open({
      source: sid,
      start: listData ? listData.root : '',
      title: `把「${dir.name}」移动到…`,
    });
    if (target === null) return;
    // 不能移到它自己或自己的子孙里，服务端也会拦，这里先给个友好提示
    if (target === dir.path || target.startsWith(dir.path + '/')) {
      return UI.toast('不能把目录移动到它自己的子目录里', 'warn', 6000);
    }
    const parent = target.replace(/\/+$/, '');
    try {
      const r = await API.folderRename({
        source: sid, path: dir.path, name: dir.name, new_parent: parent,
      });
      UI.toast(`已移动「${dir.name}」`
        + (r.indexed ? `（同步索引 ${r.indexed} 首）` : ''), 'ok', 5000);
      await load();
    } catch (e) { UI.toast('移动失败：' + U.esc(e.message), 'err', 8000); }
  }

  async function deleteFolder(dir) {
    const sid = sourceId();
    let info = null;
    try {
      info = await API.folderInfo(sid, dir.path);
    } catch (e) { /* 拿不到信息就退化成不显示细节 */ }
    const detail = info
      ? `里面共有 <b>${info.files}</b> 个文件（其中音频 ${info.audio} 个）、
         <b>${info.dirs}</b> 个子目录，合计 <b>${U.fmtBytes(info.bytes)}</b>。`
      : '';
    const ctl = UI.modal({
      title: `删除目录「${dir.name}」`,
      size: 'sm',
      body: `<div style="line-height:1.75;font-size:13px">
          ${detail}
          <div class="filtnote" style="margin-top:10px">
            默认<b>移入回收站</b>（<span class="mono">.music-sync-trash/日期/</span>），
            整个目录连同里面的文件一起搬过去，随时能恢复。
          </div>
          <label class="check" style="margin-top:12px">
            <input type="checkbox" id="df-hard"> 我要永久删除，不进回收站
          </label>
          <div class="muted" style="font-size:11.5px;margin-top:4px">
            永久删除不可撤销，请确认已经别处备份。
          </div>
        </div>`,
      footer: `<button class="btn ghost" data-act="no">取消</button>
               <button class="btn danger" data-act="yes">确认删除</button>`,
    });
    ctl.footer.querySelector('[data-act=no]').onclick = () => ctl.close();
    ctl.footer.querySelector('[data-act=yes]').onclick = async () => {
      const hard = ctl.body.querySelector('#df-hard').checked;
      if (hard) {
        // 永久删除**不可逆**，用"打字确认"而不是点一下 ——
        // 连着做几次操作时，普通确认框很容易被条件反射点过去。
        const ok = await UI.confirmHard({
          title: '永久删除文件夹',
          message: `即将<b>永久删除</b>「${U.esc(dir.name)}」及其中的全部文件。<br>
            <span class="muted">不会进回收站，<b>无法撤销</b>。</span>`,
          phrase: '永久删除',
          okText: '永久删除',
          details: [dir.path],
        });
        if (!ok) return;
      }
      try {
        const r = await API.folderDelete({
          source: sid, path: dir.path, hard, confirm_hard: hard,
        });
        ctl.close();
        UI.toast(hard ? `已永久删除「${dir.name}」`
                      : `已移入回收站：${r.trash || ''}`, hard ? 'warn' : 'ok', 7000);
        // 删的是当前目录（或它的祖先）就退到它的父目录
        if (cur === dir.path || cur.startsWith(dir.path + '/')) {
          cur = dir.path.replace(/\/[^/]*$/, '');
        }
        await load();
        App.loadSystem && App.loadSystem();
      } catch (e) { UI.toast('删除失败：' + U.esc(e.message), 'err', 8000); }
    };
  }

  App.registerView('files', {
    mount, render, load, go, pickFolder, reload, moveStaged, revealFile,
  });
})();
