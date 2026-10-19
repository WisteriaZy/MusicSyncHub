/* ============================================================
   视图：数据源（Windows 10 磁贴墙）
   ============================================================ */
(() => {
  let el = null;
  let types = [];

  // 有精确指针（鼠标）才用"悬停才显示操作按钮"的交互；
  // 触摸屏没有 hover，按钮必须常驻，否则等于点不到。
  const USE_HOVER_HINT = (() => {
    try { return window.matchMedia('(hover: hover) and (pointer: fine)').matches; }
    catch (e) { return true; }
  })();

  // ★ 类型显示名**从后端拿**（`/api/source-types` 的 label），不要在这里写死。
  //   原来这里只有 `local` 一项，其它类型就退化成显示原始 kind
  //   （列表里出现过光秃秃的 "sftp" / "webdav" / "smb" / "ftp"）。
  //   后端已经带了"（实验性）"后缀，前端直接用就行。
  function typeLabel(kind) {
    const t = types.find((x) => x.kind === kind);
    return (t && t.label) || kind;
  }

  /** 这个类型是不是实验性的（没对着真实服务器完整测过）。 */
  function isExperimental(kind) {
    const t = types.find((x) => x.kind === kind);
    return !!(t && t.experimental);
  }

  function mount(root) {
    el = root;
  }

  async function render() {
    // 系统信息（索引库大小、总曲目数）可能已经过期，超过 8 秒就先刷一次
    if (!State._sysTs || Date.now() - State._sysTs > 8000) {
      try { await App.loadSystem(); State._sysTs = Date.now(); } catch (e) { /* 忽略 */ }
    }
    const srcs = State.sources;
    const sys = State.system || {};
    const totalTracks = Object.values(sys.counts || {}).reduce((a, b) => a + b, 0);
    el.innerHTML = `
      <div>
        <h1 class="page-title">数据源</h1>
        <div class="page-sub">
          ${srcs.length} 个位置 · 已索引 ${totalTracks} 首 · 索引库 ${U.fmtBytes(sys.db_size || 0)}
          · 配置文件 <span class="mono">${U.esc((sys.data_dir || '') + '/config.toml')}</span>
        </div>
      </div>
      <div class="toolbar">
        <button class="btn primary" id="btn-add">${U.icon('plus')}<span>添加数据源</span></button>
        <button class="btn" id="btn-refresh">${U.icon('refresh')}<span>刷新</span></button>
        <span class="sep-v"></span>
        <span class="muted" style="font-size:12.5px">
          提示：把作为「源」的那一侧勾选「只读」，任何写操作都会被直接拒绝。
        </span>
      </div>
      <div class="tiles" id="tiles"></div>`;

    el.querySelector('#btn-add').onclick = () => openAdd();
    el.querySelector('#btn-refresh').onclick = async () => {
      await App.loadSources(); await App.loadSystem(); render();
    };

    const box = el.querySelector('#tiles');
    srcs.forEach((s) => box.appendChild(tile(s)));
    box.appendChild(addTile());
  }

  /* ------------------------------------------------------------ 磁贴 */
  function tile(s) {
    const color = U.tileColor(s.id);
    const target = s.root;
    const st = App.statusOf(s.id);

    const t = document.createElement('div');
    t.className = `tile ${color}`;
    t.dataset.sid = s.id;
    t.innerHTML = `
      <svg class="glyph"><use href="#i-${U.typeIcon(s.type)}"></use></svg>
      <div class="t-actions">
        <button class="btn icon sm" data-a="test" title="测试连接" aria-label="测试连接">${U.icon('plug')}</button>
        <button class="btn icon sm" data-a="scan" title="扫描建索引" aria-label="扫描建索引">${U.icon('refresh')}</button>
        <button class="btn icon sm" data-a="browse" title="浏览目录" aria-label="浏览目录">${U.icon('folder')}</button>
        <button class="btn icon sm" data-a="edit" title="编辑" aria-label="编辑">${U.icon('edit')}</button>
        <button class="btn icon sm dangerish" data-a="del" title="删除数据源" aria-label="删除数据源">${U.icon('trash')}</button>
      </div>
      <div class="t-head">
        <svg class="ico lg"><use href="#i-${U.typeIcon(s.type)}"></use></svg>
        <div class="t-title">
          <div class="t-name">${U.esc(s.name)}${
            isExperimental(s.type)
              ? '<span class="badge exp" title="这个协议类型没有对着真实服务器完整测试过">实验性</span>'
              : ''}</div>
          <div class="t-meta">
            ${U.esc(typeLabel(s.type))}
            ${s.read_only ? ' · 只读' : ''}
            ${s.enabled === false ? ' · 已停用' : ''}
          </div>
        </div>
        ${App.dotHtml(s.id, true)}
      </div>
      <div class="t-target" title="${U.esc(target || '')}">${U.esc(target || '(未设置路径)')}</div>
      <div class="t-status" data-status="${U.esc(s.id)}">${statusLine(s.id)}</div>
      <div class="t-foot">
        <div class="t-count">${s.indexed || 0}<small>首</small></div>
        <div class="t-meta t-scan" style="margin-left:auto;text-align:right">
          扫描 ${U.fmtAgo(s.last_scan)}
        </div>
      </div>
      <div class="t-out hidden" id="out-${s.id}"></div>`;

    // 触摸设备上 hover 出不来，按钮必须一直可见，否则点不到
    if (USE_HOVER_HINT === false) t.classList.add('t-actions-always');
    t.addEventListener('pointerenter', () => t.classList.add('t-actions-always'));
    t.addEventListener('pointerleave', () => {
      if (USE_HOVER_HINT === false) return;
      t.classList.remove('t-actions-always');
    });

    t.querySelectorAll('button[data-a]').forEach((b) => {
      b.onclick = (e) => { e.stopPropagation(); action(t, s, b.dataset.a, b); };
    });
    t.onclick = () => {
      App.selectSource(s.id);
    };
    return t;
  }

  /** 磁贴上的在线状态一行（带原因，不只是个点） */
  function statusLine(sid) {
    const st = App.statusOf(sid);
    if (!st) return '<span class="badge">检测中…</span>';
    if (st.ok) {
      const ms = st.latency != null ? ` · ${Math.round(st.latency * 1000)} ms` : '';
      return `<span class="badge ok">${U.icon('check')} 在线可读${ms}</span>`;
    }
    return `<span class="badge err" title="${U.esc(st.error || '')}">${U.icon('warn')} 离线：${U.esc(st.error || '无法访问')}</span>`;
  }

  /** 探活结果回来后只刷新状态区，不整页重建（避免闪一下） */
  function refreshStatus() {
    if (!el) return;
    (State.sources || []).forEach((s) => {
      const box = el.querySelector(`[data-status="${s.id}"]`);
      if (box) box.innerHTML = statusLine(s.id);
      const t = el.querySelector(`.tile[data-sid="${s.id}"] .dot`);
      if (t) t.outerHTML = App.dotHtml(s.id, true);
    });
  }

  function addTile() {
    const t = document.createElement('div');
    t.className = 'tile add';
    t.innerHTML = `${U.icon('plus', 'ico xl')}<div style="font-size:14px;font-weight:600">添加数据源</div>
      <div class="t-meta">添加本地音乐目录</div>`;
    t.onclick = () => openAdd();
    return t;
  }

  /* ------------------------------------------------------------ 磁贴动作 */
  async function action(tileEl, s, a, btn) {
    const out = tileEl.querySelector(`#out-${s.id}`);
    const show = (html) => { out.classList.remove('hidden'); out.innerHTML = html; };

    if (a === 'test') {
      btn.disabled = true;
      show(`<span class="spinner"></span> 正在连接…`);
      // 把结果写进全局状态表：侧栏小圆点、磁贴状态行、重测按钮都读它，
      // 这样"测试连接"和自动探活用的是同一份数据，不会各说各话。
      const setState = (ok, latency, error) => {
        State.srcStatus = State.srcStatus || {};
        State.srcStatus[s.id] = {
          id: s.id, ok: !!ok, error: error || '',
          latency: latency != null ? latency : null,
          checked_at: Date.now() / 1000,
        };
        State.srcStatusAt = Date.now();
      };
      try {
        const r = await API.testSource(s.id);
        if (r.ok) {
          setState(true, r.latency, '');
          show(`${U.icon('check')} 连接成功（${(r.latency * 1000).toFixed(0)} ms）<br>
                根目录 ${r.entries} 项：${r.dirs} 个文件夹 / ${r.audio} 个音频文件
                ${r.sample && r.sample.length ? `<br><span style="opacity:.75">${U.esc(r.sample.slice(0, 4).join('　'))}</span>` : ''}`);
        } else {
          setState(false, r.latency, r.error || '连接失败');
          show(`${U.icon('warn')} ${U.esc(r.error || '连接失败')}
                ${r.tip ? `<br><span style="opacity:.75">${U.esc(r.tip)}</span>` : ''}`);
        }
      } catch (e) {
        setState(false, null, e.message);
        show(`${U.icon('warn')} ${U.esc(e.message)}`);
      }
      btn.disabled = false;
      App.renderSidebar();
      refreshStatus();
      return;
    }
    if (a === 'scan') {
      const ok = await UI.confirm(
        `开始扫描 <b>${U.esc(s.name)}</b>？<br>
         <span class="muted">增量扫描：文件大小/修改时间没变就直接用索引缓存，不会重复解析。</span>`,
        { title: '扫描数据源', okText: '开始扫描' });
      if (ok) App.scanDialog(s.id);
      return;
    }
    if (a === 'browse') { browseDialog(s); return; }
    if (a === 'edit') { openAdd(s); return; }
    if (a === 'del') {
      const ok = await UI.confirm(
        `确定删除数据源 <b>${U.esc(s.name)}</b> 吗？<br>
         <span class="muted">只删除配置，不会动服务器上的文件。</span>`,
        { danger: true, okText: '删除', title: '删除数据源' });
      if (!ok) return;
      const purge = await UI.confirm('是否同时清空它的索引记录？',
        { okText: '清空索引', title: '清理索引' });
      await API.deleteSource(s.id, purge);
      UI.toast('已删除数据源', 'ok');
      await App.loadSources();
      if (State.view === 'sources') render(); else App.renderSidebar();
    }
  }

  /* ------------------------------------------------------------ 目录浏览 */
  async function browseDialog(s) {
    const ctl = UI.modal({
      title: `浏览目录 · ${s.name}`,
      size: 'lg',
      body: `
        <div class="row" style="margin-bottom:10px">
          <button class="btn ghost" id="br-up" title="上级目录">${U.icon('prev')}<span>上级</span></button>
          <input type="text" id="br-path" class="mono" style="flex:1" value="${U.esc(s.root || '')}">
          <button class="btn" id="br-go">${U.icon('folder')}<span>前往</span></button>
        </div>
        <div id="br-list" class="tbl-wrap" style="max-height:52vh"></div>`,
      footer: `<span class="muted grow" id="br-info"></span>
               <button class="btn ghost" data-act="close">关闭</button>
               <button class="btn primary" data-act="use">用此目录作为根路径</button>`,
    });
    const listBox = ctl.body.querySelector('#br-list');
    const pathInput = ctl.body.querySelector('#br-path');
    let cur = '';

    async function go(p) {
      listBox.innerHTML = '<div class="pad muted">加载中…</div>';
      try {
        const r = await API.browse(s.id, p);
        cur = p || '';
        pathInput.value = r.abs || '';
        ctl.body.querySelector('#br-info').textContent =
          `${r.dirs.length} 个文件夹 · ${r.file_count} 个音频文件`;
        const rows = [];
        if (cur) rows.push(`<tr data-p="${U.esc(U.parent(cur))}">
          <td>${U.icon('prev')} ..</td><td class="muted">上级目录</td><td></td></tr>`);
        r.dirs.forEach((d) => {
          rows.push(`<tr data-p="${U.esc(cur ? cur.replace(/\/+$/, '') + '/' + d : d)}">
            <td>${U.icon('folder')} ${U.esc(d)}</td><td class="muted">文件夹</td><td class="num"></td></tr>`);
        });
        r.files.forEach((f) => {
          rows.push(`<tr><td>${U.icon('music')} ${U.esc(f.name)}</td>
            <td class="muted">${U.esc((f.name.split('.').pop() || '').toUpperCase())}</td>
            <td class="num">${U.fmtBytes(f.size)}</td></tr>`);
        });
        listBox.innerHTML = `<table class="data">
          <thead><tr><th>名称</th><th>类型</th><th class="right">大小</th></tr></thead>
          <tbody>${rows.join('') || '<tr><td colspan="3" class="pad muted">空目录</td></tr>'}</tbody></table>`;
        listBox.querySelectorAll('tr[data-p]').forEach((tr) => {
          tr.style.cursor = 'pointer';
          tr.onclick = () => go(tr.dataset.p);
        });
      } catch (e) {
        listBox.innerHTML = `<div class="pad" style="color:var(--danger)">${U.esc(e.message)}</div>`;
      }
    }
    ctl.body.querySelector('#br-up').onclick = () => { if (cur) go(U.parent(cur)); };
    ctl.body.querySelector('#br-go').onclick = () => go(pathInput.value.trim());
    ctl.footer.querySelector('[data-act=close]').onclick = () => ctl.close();
    ctl.footer.querySelector('[data-act=use]').onclick = () => {
      ctl.close();
      openAdd(s, { presetRoot: pathInput.value.trim() || s.root });
    };
    go(s.root || '');
  }

  /* ------------------------------------------------------------ 新增 / 编辑 */
  async function openAdd(existing, { presetRoot } = {}) {
    if (!types.length) types = await API.sourceTypes();
    const s = existing || {
      id: '', name: '', type: 'local', root: presetRoot || '',
      read_only: false, enabled: true, follow_symlinks: false, extras: {},
    };
    if (existing && presetRoot) s.root = presetRoot;
    const isNew = !existing;

    const ctl = UI.modal({
      title: isNew ? '添加数据源' : `编辑数据源 · ${s.name}`,
      size: 'md',
      body: formHtml(s, types, isNew),
      footer: `<button class="btn ghost" data-act="test">${U.icon('plug')}<span>测试连接</span></button>
               <div class="grow"></div>
               <button class="btn ghost" data-act="cancel">取消</button>
               <button class="btn primary" data-act="save">${isNew ? '添加' : '保存'}</button>`,
    });

    const body = ctl.body;
    const typeSel = body.querySelector('#f-type');
    const sync = () => {
      body.querySelectorAll('[data-for]').forEach((n) => {
        const kinds = n.dataset.for.split(',');
        n.classList.toggle('hidden', !kinds.includes(typeSel.value));
      });
      const hint = body.querySelector('#f-type-hint');
      if (hint) {
        const t = types.find((x) => x.kind === typeSel.value);
        // ★ 实验性协议要**醒目**地标出来，不能只藏在下拉项的文字里：
        //   下拉一收起那个后缀就看不见了，用户会以为它和本地源一样可靠。
        //   而且必须说清"没验证到什么程度" —— 光"实验性"三个字太含糊。
        if (t && t.experimental) {
          hint.innerHTML =
            '<div class="exp-warn">'
            + `<b>${U.icon('warn')} 实验性功能</b>`
            + '<div>这个数据源类型<b>还没有对着真实服务器完整测试过</b>，'
            + '不能保证一定没问题。</div>'
            + (t.verify_note
                ? `<div class="exp-what">${U.esc(t.verify_note.replace(/\*\*/g, ''))}</div>`
                : '')
            + '<div class="exp-tip">建议先做两件事：'
            + '① 勾上「只读」当保险丝，确认能正常浏览、播放之后再取消；'
            + '② 先用一个小目录试一次搬运，核对结果再上大批量。</div>'
            + '</div>';
        } else {
          hint.textContent = t ? t.hint : '';
        }
      }
    };
    typeSel.onchange = sync;
    sync();

    ctl.footer.querySelector('[data-act=cancel]').onclick = () => ctl.close();
    ctl.footer.querySelector('[data-act=test]').onclick = async () => {
      if (isNew) { UI.toast('新数据源需要先保存才能测试连接', 'warn'); return; }
      const payload = collect(body, s.id, s);
      try {
        await API.updateSource(s.id, payload);
        const r = await API.testSource(s.id);
        UI.toast(r.ok ? `连接成功，根目录 ${r.entries} 项` : ('连接失败：' + U.esc(r.error || '')),
          r.ok ? 'ok' : 'err', 7000);
      } catch (e) { UI.toast('测试失败：' + U.esc(e.message), 'err'); }
    };

    // ---------------------------------------------------- 磁盘探测 / 选取
    const devBox = body.querySelector('#f-device');
    let probedPath = '';
    // 用户是否**主动**动过这个字段（要区分"人打的字"和"我们替它填的"）。
    // 踩过：原来监听 `input` 事件就当作用户改过 —— 但从磁盘菜单里选一个
    // 盘时代码也会写 `input.value`，如果顺手派发 input 事件就会被当成
    // "用户已定"，于是"可移除设备建议只读"永远弹不出来。
    let userEdited = false;

    /** 探测当前填的路径，给出"这是什么盘 + 建议怎么配"。 */
    const probeRoot = async (path, { auto = false } = {}) => {
      const p = (path || '').trim();
      if (!devBox) return;
      if (!p || typeSel.value !== 'local') { devBox.innerHTML = ''; probedPath = ''; return; }
      if (p === probedPath) return;
      probedPath = p;
      devBox.innerHTML = '<span class="muted">正在识别磁盘…</span>';
      let r;
      try {
        r = await API.deviceProbe(p);
      } catch (e) {
        // 路径还不存在是正常情况（用户正在输入），不要报错刷屏
        devBox.innerHTML = '<span class="muted">（这个路径暂时读不到，保存前请确认它存在）</span>';
        return;
      }
      const info = r.info || {};
      // 标签就够表达盘的性质了（"可移除存储（U 盘 / 移动硬盘 / SD 卡）"），
      // 不要再额外贴一个"网络位置"徽标 —— 会和下面的说明文字重复。
      const bits = [`<b>${U.esc(r.label || info.kind || '')}</b>`];
      if (info.fs) bits.push(U.esc(info.fs));
      if (info.mount) bits.push(U.esc(info.mount));
      if (info.read_mbps != null) bits.push(`约 ${Math.round(info.read_mbps)} MB/s`);
      devBox.innerHTML = `${U.icon('disk')} ${bits.join(' · ')}${
        (r.notes || []).length
          ? '<div class="muted" style="margin-top:2px">'
            + r.notes.map((n) => U.esc(n)).join('<br>') + '</div>'
          : ''}`;

      // ★ 只在**新建**数据源、且用户还没手动动过这两项时套用建议。
      //   编辑已有数据源时贸然覆盖用户的设置很讨厌
      //   （比如他特意把只读取消了，一编辑又被勾回来）。
      const sug = r.suggested || {};
      if (auto && isNew && !userEdited) {
        const ro = body.querySelector('#f-readonly');
        const sd = body.querySelector('#f-slowdisk');
        let changed = [];
        if (ro && sug.read_only && !ro.checked) {
          ro.checked = true;
          changed.push('只读');
        }
        const ex = sug.extras || {};
        if (sd && ex.slow_disk && !sd.checked) {
          sd.checked = true;
          changed.push('慢速盘模式');
        }
        if (changed.length) {
          UI.toast(`识别到${info.removable ? '可移除设备' : '网络位置'}，`
            + `已默认勾选「${changed.join('」「')}」（可取消）`, 'info', 8000);
        }
      }
    };

    const rootInput = body.querySelector('#f-root-local');
    if (rootInput) {
      // 只有**真实按键**才算"用户改过"（程序化赋值不派发这个事件）
      rootInput.addEventListener('keydown', () => { userEdited = true; });
      rootInput.addEventListener('paste', () => { userEdited = true; });
      // 失焦时探测（不是每次输入都探，否则一边打字一边读盘）
      rootInput.addEventListener('blur', () => probeRoot(rootInput.value, { auto: true }));
    }

    const pickBtn = body.querySelector('#f-pick-drive');
    if (pickBtn) {
      pickBtn.onclick = async () => {
        let drives = [];
        try {
          const r = await API.drives();
          drives = r.drives || [];
        } catch (e) {
          UI.toast('读取磁盘列表失败：' + U.esc(e.message), 'err', 7000);
          return;
        }
        if (!drives.length) { UI.toast('没有找到可用的磁盘', 'warn'); return; }
        const items = drives.map((d) => ({
          icon: d.removable ? 'disk' : 'db',
          label: `${d.root}   ${d.label}` +
            (d.removable ? '  · 可移除' : '') +
            `  · 剩余 ${U.fmtBytes(d.free)}`,
          onPick: () => {
            // 从菜单里选盘是**用户的明确选择**，仍然允许套用建议
            // （这正是最该给建议的时刻：插上一个 U 盘 → 选它 → 提示只读）
            if (rootInput) rootInput.value = d.root;
            probeRoot(d.root, { auto: true });
          },
        }));
        const r = pickBtn.getBoundingClientRect();
        UI.contextMenu(r.left, r.bottom + 4, items);
      };
    }

    // 打开编辑框时如果本来就有路径，先探一次（不自动改设置）
    if (s.root && typeSel.value === 'local') probeRoot(s.root);

    ctl.footer.querySelector('[data-act=save]').onclick = async () => {
      const payload = collect(body, s.id, s);
      if (!payload.name) { UI.toast('请填写名称', 'warn'); return; }
      try {
        if (isNew) await API.addSource(payload); else await API.updateSource(s.id, payload);
        ctl.close();
        UI.toast(isNew ? '数据源已添加，点磁贴上的刷新按钮即可扫描' : '已保存', 'ok');
        await App.loadSources(); await App.loadSystem();
        if (State.view === 'sources') render(); else App.renderSidebar();
      } catch (e) { UI.toast('保存失败：' + U.esc(e.message), 'err'); }
    };
  }

  function formHtml(s, types, isNew) {
    const opt = types.map((t) =>
      `<option value="${t.kind}" ${s.type === t.kind ? 'selected' : ''}>${t.label}</option>`).join('');
    return `
      <div class="grid c2">
        <div class="field"><label>名称 *</label>
          <input type="text" id="f-name" value="${U.esc(s.name)}" placeholder="例如 Windows 音乐库"></div>
        <div class="field"><label>唯一 ID ${isNew ? '*' : '（不可改）'}</label>
          <input type="text" id="f-id" value="${U.esc(s.id)}" placeholder="win-music" ${isNew ? '' : 'disabled'}></div>
      </div>
      <div class="field"><label>类型 *</label>
        <select id="f-type">${opt}</select>
        <div class="hint" id="f-type-hint"></div>
      </div>

      <div class="field" data-for="local"><label>本地根路径 *</label>
        <div class="row" style="gap:6px">
          <input type="text" id="f-root-local" value="${U.esc(s.root)}"
                 placeholder="Windows: D:\\Music　　Linux/macOS: /mnt/music"
                 style="flex:1">
          <button class="btn sm" id="f-pick-drive" type="button"
            title="从这台机器的磁盘里挑（含 U 盘 / 移动硬盘）">
            ${U.icon('disk')}<span>选磁盘</span></button>
        </div>
        <div class="hint">Windows 路径直接写即可：D:\\Music</div>
        <div id="f-device" class="hint"></div></div>

      <!-- ★ 网络数据源（SFTP / WebDAV / HTTP 目录 / SMB / FTP）的连接参数。
           以前**这些字段一个都没有** —— 后端早就实现了这几种源，
           界面上却只有"本地根路径"，于是选任何网络类型都填不了主机名，
           等于选不了。 -->
      <div data-for="sftp,webdav,http,smb,ftp">
        <div class="grid c2">
          <div class="field"><label>主机 / 地址 *</label>
            <input type="text" id="f-host" value="${U.esc(s.host || '')}"
                   placeholder="192.168.1.10 或 nas.local"></div>
          <div class="field"><label>端口</label>
            <input type="number" id="f-port" value="${s.port || ''}"
                   placeholder="SFTP 22 · FTP 21 · SMB 445 · WebDAV 留空"></div>
        </div>
        <div class="grid c2">
          <div class="field"><label>用户名</label>
            <input type="text" id="f-username" value="${U.esc(s.username || '')}"></div>
          <div class="field"><label>密码 ${s.has_secret ? '（已保存，留空则不改）' : ''}</label>
            <input type="password" id="f-password" value=""
                   placeholder="${s.has_secret ? '••••••（留空保持不变）' : '留空 = 匿名 / 免密'}"></div>
        </div>
        <div class="field" data-for="smb"><label>共享名 / 域</label>
          <div class="row">
            <input type="text" id="f-share" value="${U.esc(s.share || '')}"
                   placeholder="共享名，如 music" style="flex:1">
            <input type="text" id="f-domain" value="${U.esc(s.domain || '')}"
                   placeholder="域（可选）" style="flex:1">
          </div></div>
        <div class="field" data-for="webdav,http"><label>基地址（可选）</label>
          <input type="text" id="f-base" value="${U.esc(s.base_url || '')}"
                 placeholder="https://host/dav"></div>
        <div class="field" data-for="sftp,webdav,http,smb,ftp"><label>远端根路径 *</label>
          <input type="text" id="f-root-remote" value="${U.esc(s.root || '')}"
                 placeholder="/music　或　/mnt/music　或　\\\\主机\\共享\\music">
          <div class="hint">这是"库的根目录"，后面所有路径都相对它。
            FTP/SFTP/WebDAV 用 / 开头；SMB 用 \\\\主机\\共享 形式。</div></div>
      </div>

      <div class="field" data-for="local" id="f-opt-wrap">
        <label class="check" style="font-weight:600">
          <input type="checkbox" id="f-slowdisk" ${s.extras && s.extras.slow_disk ? 'checked' : ''}>
          慢速盘模式（U 盘 / 移动硬盘 / 网络位置建议开启）</label>
        <div class="hint" style="margin-left:22px">
          开启后：扫描并发压到 2、哈希并发 1、并跳过按内容的全量哈希校验
          （U 盘上按内容哈希会让扫描从几分钟变成几十分钟，而且中途拔盘就白干）。
          内置盘不要开，会白白变慢。</div>

        <label class="check" style="font-weight:600;margin-top:8px">传输模式</label>
        <select id="f-tmode" style="width:auto;margin:2px 0 0 22px">
          <option value="standard" ${(s.extras && s.extras.transfer_mode) === 'fast' ? '' : 'selected'}>
            标准传输（推荐）</option>
          <option value="fast" ${(s.extras && s.extras.transfer_mode) === 'fast' ? 'selected' : ''}>
            快速传输</option>
        </select>
        <div class="hint" style="margin-left:22px">
          <b>标准</b>：复制到目标 → <b>校验大小一致</b> → 才删源。慢一点，
          但"目标写歪了"能当场发现并中止。<br>
          <b>快速</b>：复制完直接删源，省掉那一次大小核对。往网络盘 / U 盘
          搬大批文件时能省下可观的往返开销。<br>
          <span class="muted">两种模式下源都<b>进回收站</b>（不是永久删除），
          所以"快速"最坏也只是目标那份可能不完整、而源还在回收站里，随时能拿回来。</span>
        </div>
      </div>

      <hr class="sep">
      <div class="row wrap">
        <label class="check"><input type="checkbox" id="f-readonly" ${s.read_only ? 'checked' : ''}>
          只读（拒绝一切写入/删除，强烈建议对源库勾选）</label>
        <label class="check"><input type="checkbox" id="f-enabled" ${s.enabled !== false ? 'checked' : ''}> 启用</label>
        <label class="check" data-for="local"><input type="checkbox" id="f-symlink" ${s.follow_symlinks ? 'checked' : ''}> 跟随符号链接</label>
      </div>`;
  }

  function collect(body, existingId, base) {
    const v = (id) => { const n = body.querySelector(id); return n ? n.value.trim() : ''; };
    const c = (id) => { const n = body.querySelector(id); return n ? n.checked : false; };
    const type = v('#f-type');
    const root = v('#f-root-local');
    // ★ extras 要**合并**而不是清空。
    //   原来这里写死 `extras: {}` —— 保存一次就把数据源上已有的
    //   扩展参数（慢速盘、自定义并发…）全抹掉了，
    //   用户会莫名其妙发现"改了名字之后扫描变慢了"。
    const extras = { ...((base && base.extras) || {}) };
    if (type === 'local') {
      if (c('#f-slowdisk')) extras.slow_disk = true;
      else delete extras.slow_disk;
      // 传输模式：只有"快速"才需要写进 extras（standard 是默认值，
      // 不写能让配置文件保持干净，以后改默认值也能自动跟上）
      const tm = (body.querySelector('#f-tmode') || {}).value || 'standard';
      if (tm === 'fast') extras.transfer_mode = 'fast';
      else delete extras.transfer_mode;
    }
    const payload = {
      id: existingId || v('#f-id') || ('src' + Date.now()),
      name: v('#f-name'),
      type,
      root,
      read_only: c('#f-readonly'),
      enabled: c('#f-enabled'),
      follow_symlinks: c('#f-symlink'),
      extras,
    };
    // 网络源的连接参数。以前这些键从来没被收集过，
    // 所以后端的 SFTP/WebDAV/HTTP/SMB 在界面上等于不可用。
    if (type !== 'local') {
      const host = v('#f-host');
      const rootRemote = v('#f-root-remote');
      if (host) payload.host = host;
      if (rootRemote) payload.root = rootRemote;
      const port = v('#f-port');
      if (port) payload.port = Number(port) || 0;
      const user = v('#f-username');
      if (user) payload.username = user;
      const base = v('#f-base');
      if (base) payload.base_url = base;
      const share = v('#f-share');
      if (share) payload.share = share;
      const domain = v('#f-domain');
      if (domain) payload.domain = domain;
      // 密码只在**填了**的时候才提交 —— 留空表示"不改"，
      // 否则每次编辑都会把已存的密码清成空串。
      const pw = (body.querySelector('#f-password') || {}).value || '';
      if (pw) payload.password = pw;
    }
    return payload;
  }

  App.registerView('sources', { mount, render, openAdd, refreshStatus });
})();
