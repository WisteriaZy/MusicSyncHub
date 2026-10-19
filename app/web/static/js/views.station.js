/* ============================================================
   中转站（station）

   上传后暂时没想好放哪的文件先放这里；库里的文件也可以临时挪进来。
   这个页面要回答三件事：
     1. 现在占了多少 / 上限多少 / 还剩多少（防爆盘的核心信息）；
     2. 里面有哪些文件；
     3. 我能对它们做什么（智能分配去向 / 归入目录 / 移入回收站）。
   ============================================================ */
const Station = (() => {
  let el = null;
  let data = null;
  let staging = null;         // 上传暂存区（/api/upload/batches）
  let checked = new Set();     // 选中的文件 rel
  let stgChecked = new Set();  // 选中的暂存批次
  let busy = false;

  const id = (rel) => rel;

  function mount(root) { el = root; }

  function render() {
    el.innerHTML = `
      <h1 class="page-title">中转站</h1>
      <div class="page-sub">
        中转站放"我还没决定放哪"的文件（可长期存放，库里的文件也能临时挪进来）；
        上传暂存区只放"这次上传还没走完流程"的文件，属于过路区。
        两个区域的位置和容量上限都能在「设置」里改。
      </div>

      <div class="card">
        <div class="row" style="justify-content:space-between;align-items:baseline">
          <h3 style="margin:0">中转站</h3>
          <span class="muted" id="st-dir" style="font-size:11.5px"></span>
        </div>
        <div id="st-stats" class="grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr));margin-top:8px"></div>
        <div class="toolbar" style="margin-top:8px">
          <button class="btn sm" id="st-refresh">${U.icon('refresh')}<span>刷新</span></button>
          <span class="sep-v"></span>
          <button class="btn sm" id="st-all">全选</button>
          <button class="btn sm ghost" id="st-none">全不选</button>
          <div class="grow"></div>
          <button class="btn sm" id="st-smart" data-exclusive disabled
            title="用「智能整理」的同一套规则，为选中的文件推算去向">
            ${U.icon('settings')}<span>智能分配去向</span></button>
          <button class="btn sm" id="st-out" data-exclusive disabled
            title="把选中的文件归入库里某个目录">${U.icon('folder')}<span>归入目录…</span></button>
          <button class="btn sm" id="st-restore" data-exclusive disabled
            title="把选中的文件放回它们原来的位置（移入中转站时记下了来源）">
            ${U.icon('history')}<span>还原原位置</span></button>
          <button class="btn sm danger" id="st-del" data-exclusive disabled
            title="移入回收站（不是直接删除）">${U.icon('trash')}<span>移入回收站</span></button>
        </div>
        <div id="st-list" style="margin-top:10px"></div>
      </div>

      <div class="card" id="stg-card">
        <div class="row" style="justify-content:space-between;align-items:baseline">
          <h3 style="margin:0">上传暂存区</h3>
          <span class="muted" id="stg-dir" style="font-size:11.5px"></span>
        </div>
        <div class="muted" style="font-size:12px;margin:4px 0 8px">
          上传后还没走完「入库」向导的批次。它只是过路区 ——
          长期放在这里会占着上传配额，建议要么入库、要么转进中转站。
        </div>
        <div id="stg-stats" class="grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr))"></div>
        <div class="toolbar" style="margin-top:8px">
          <button class="btn sm" id="stg-refresh">${U.icon('refresh')}<span>刷新</span></button>
          <span class="sep-v"></span>
          <button class="btn sm" id="stg-all">全选</button>
          <button class="btn sm ghost" id="stg-none">全不选</button>
          <div class="grow"></div>
          <button class="btn sm" id="stg-to-station" disabled
            title="把选中批次的文件转进中转站长期存放（不是丢掉）">
            ${U.icon('history')}<span>转入中转站</span></button>
          <button class="btn sm ghost" id="stg-clean-empty"
            title="清掉空批次目录和散落在暂存区根目录的孤儿文件">清空壳</button>
          <button class="btn sm ghost" id="stg-clean-stale"
            title="清掉超过滞留时限的批次">清滞留</button>
          <button class="btn sm danger" id="stg-del" disabled
            title="直接删掉选中批次（不动音乐库）">${U.icon('trash')}<span>删除批次</span></button>
        </div>
        <div id="stg-list" style="margin-top:10px"></div>
      </div>`;
    el.querySelector('#st-refresh').onclick = () => load();
    el.querySelector('#st-all').onclick = () => {
      (data && data.files || []).forEach((f) => checked.add(id(f.rel)));
      paint();
    };
    el.querySelector('#st-none').onclick = () => { checked.clear(); paint(); };
    el.querySelector('#st-smart').onclick = smartAssign;
    el.querySelector('#st-out').onclick = () => moveOut();
    el.querySelector('#st-restore').onclick = restore;
    el.querySelector('#st-del').onclick = remove;

    el.querySelector('#stg-refresh').onclick = () => loadStaging();
    el.querySelector('#stg-all').onclick = () => {
      ((staging && staging.batches) || []).forEach((b) => stgChecked.add(b.batch));
      paintStaging();
    };
    el.querySelector('#stg-none').onclick = () => { stgChecked.clear(); paintStaging(); };
    el.querySelector('#stg-to-station').onclick = toStation;
    el.querySelector('#stg-del').onclick = deleteBatches;
    el.querySelector('#stg-clean-empty').onclick = () => cleanup('empty');
    el.querySelector('#stg-clean-stale').onclick = () => cleanup('stale');
  }

  async function load() {
    const box = el.querySelector('#st-list');
    if (box) box.innerHTML = '<div class="pad muted">读取中转站…</div>';
    await Promise.all([loadStation(), loadStaging()]);
  }

  async function loadStation() {
    try {
      data = await API.station();
    } catch (e) {
      const box = el.querySelector('#st-list');
      if (box) box.innerHTML = `<div class="pad" style="color:var(--danger)">${U.esc(e.message)}</div>`;
      return;
    }
    // 文件可能已经被移走了，清掉不存在的选中项
    const alive = new Set((data.files || []).map((f) => id(f.rel)));
    Array.from(checked).forEach((k) => { if (!alive.has(k)) checked.delete(k); });
    paint();
  }

  async function loadStaging() {
    const box = el.querySelector('#stg-list');
    if (box) box.innerHTML = '<div class="pad muted">读取暂存区…</div>';
    try {
      staging = await API.uploadBatches();
    } catch (e) {
      if (box) box.innerHTML = `<div class="pad" style="color:var(--danger)">${U.esc(e.message)}</div>`;
      return;
    }
    const alive = new Set(((staging && staging.batches) || []).map((b) => b.batch));
    Array.from(stgChecked).forEach((k) => { if (!alive.has(k)) stgChecked.delete(k); });
    paintStaging();
  }

  function paint() {
    const d = data || { files: [], count: 0, bytes: 0, cap_bytes: 0, remaining: null };
    const cap = d.cap_bytes || 0;
    const pct = cap ? Math.min(100, (d.bytes / cap) * 100) : 0;
    const warn = cap && pct >= 85;
    el.querySelector('#st-stats').innerHTML =
      UI.stat('已用', U.fmtBytes(d.bytes),
        cap ? `上限 ${U.fmtBytes(cap)}` : '未设上限') +
      UI.stat('文件数', d.count) +
      UI.stat('剩余容量',
        d.remaining == null ? '不限' : U.fmtBytes(d.remaining),
        cap ? `${pct.toFixed(1)}% 已用` : '') +
      UI.stat('所在位置', '', d.dir);

    const files = d.files || [];
    const ignored = d.ignored || [];
    const sel = files.filter((f) => checked.has(id(f.rel)));
    const selBytes = sel.reduce((a, f) => a + (f.size || 0), 0);
    const dirEl = el.querySelector('#st-dir');
    if (dirEl) dirEl.textContent = d.dir || '';

    // ★ 用户手扔进中转站目录的非音频文件：列出来，说清"不会归库"。
    //   不列的话它们占着盘却完全找不到，用户只会觉得"空间怎么少了"。
    const junkHtml = ignored.length ? `
      <div class="card" style="margin-top:12px">
        <div class="row" style="justify-content:space-between">
          <b>${U.icon('info')}<span>非音频文件 ${ignored.length} 个</span></b>
          <span class="muted">共 ${
            U.fmtBytes(ignored.reduce((a, f) => a + (f.size || 0), 0))}　不会归入曲库</span>
        </div>
        <div class="muted" style="font-size:12px;margin:6px 0">
          这些不是音乐文件（说明.txt、截图等），<b>不会出现在上面的待归位列表里</b>，
          也不能归入曲库。它们只是占着磁盘空间，可以直接删掉。
        </div>
        <div class="tbl-wrap" style="max-height:22vh">
          <table class="data"><thead><tr>
            <th>文件</th><th class="right">大小</th><th></th></tr></thead>
          <tbody>${ignored.slice(0, 100).map((f) => `<tr>
            <td class="mono ell" title="${U.esc(f.path)}">${U.esc(f.name)}</td>
            <td class="num">${U.fmtBytes(f.size || 0)}</td>
            <td class="num"><button class="btn sm ghost danger"
              data-stjunk="${U.esc(f.rel)}"
              title="永久删除这个文件（它不在曲库里，删了不影响音乐）">删除</button></td>
          </tr>`).join('')}</tbody></table></div>
      </div>` : '';

    const box = el.querySelector('#st-list');
    // 容量条只要设了上限就显示（哪怕空着）—— 用户要一眼看到"还剩多少、
    // 上限多少"，这正是防爆盘的关键信息，不能等有文件了才出现。
    const barHtml = cap ? `
      <div class="stor-bar ${warn ? 'warn' : ''}">
        <div class="stor-fill" style="width:${pct}%"></div>
        <span class="stor-text">
          ${U.fmtBytes(d.bytes)} / ${U.fmtBytes(cap)}
          （${pct.toFixed(1)}%）${warn ? '　⚠ 快满了，建议先清理或归位' : ''}</span>
      </div>` : `
      <div class="muted" style="font-size:12px;margin-bottom:8px">
        未设置容量上限（可在设置里配置 station_max_gb）</div>`;
    if (!files.length) {
      box.innerHTML = `${barHtml}
        <div class="empty" style="padding:34px">
        <div class="big">${U.icon('db', 'ico')}</div>
        <div>中转站是空的</div>
        <div class="muted" style="font-size:12px;margin-top:6px">
          上传后没想好放哪的文件、或从库里临时挪出来的文件，都会出现在这里</div>
      </div>`;
    } else {
      box.innerHTML = `
        ${barHtml}
        <div class="tbl-wrap" style="max-height:56vh">
          <table class="data"><thead><tr>
            <th style="width:34px"></th><th>文件</th><th>加入时间</th>
            <th class="right">大小</th><th></th></tr></thead><tbody>
          ${files.map((f) => {
            const on = checked.has(id(f.rel));
            return `<tr data-rel="${U.esc(f.rel)}" class="${on ? 'sel' : ''}">
              <td><input type="checkbox" data-st="${U.esc(f.rel)}" ${on ? 'checked' : ''}></td>
              <td class="ell" style="max-width:420px" title="${U.esc(f.path)}">
                <b>${U.esc(f.name)}</b>
                ${f.is_audio ? '' : '<span class="tag hi">非音频</span>'}</td>
              <td class="muted" style="font-size:12px">${U.fmtDate(f.mtime)}</td>
              <td class="num">${U.fmtBytes(f.size)}</td>
              <td class="num nowrap">
                <button class="btn sm ghost" data-one="${U.esc(f.rel)}"
                  title="只把这个文件归入目录">归入</button>
              </td></tr>`;
          }).join('')}
          </tbody></table>
        </div>${junkHtml}`;
      box.querySelectorAll('[data-st]').forEach((cb) => {
        cb.onchange = () => {
          if (cb.checked) checked.add(cb.dataset.st); else checked.delete(cb.dataset.st);
          paint();
        };
      });
      // 删掉非音频杂物（它不在曲库里，删除不影响音乐）
      box.querySelectorAll('[data-stjunk]').forEach((b) => {
        b.onclick = async (e) => {
          e.stopPropagation();
          const rel = b.dataset.stjunk;
          const junkFile = ignored.find((f) => f.rel === rel);
          const okd = await UI.confirm(
            `把 <b>${U.esc(junkFile ? junkFile.name : rel)}</b> 移入回收站？<br>
             <span class="muted">它不是音乐文件、不在曲库里，删掉不会影响任何歌曲。
             文件会移到 <span class="mono">.music-sync-trash/日期/</span>，需要时能拿回来。</span>`,
            { title: '清理非音频文件', okText: '移入回收站', danger: true });
          if (!okd) return;
          try {
            await App.runExclusive('cleanup', () => API.stationRemove({
              source: destSource(), files: [rel], confirm: true,
            }));
            UI.toast('已移入回收站', 'ok', 3000);
          } catch (err) {
            UI.toast('删除失败：' + (err && err.message ? err.message : err),
              'err', 7000);
          }
          await loadStation();
        };
      });
      box.querySelectorAll('tr[data-rel]').forEach((tr) => {
        tr.onclick = (e) => {
          if (e.target.closest('button')) return;
          const cb = tr.querySelector('[data-st]');
          if (!cb) return;
          cb.checked = !cb.checked;
          cb.onchange();
        };
      });
      box.querySelectorAll('[data-one]').forEach((b) => {
        b.onclick = (e) => {
          e.stopPropagation();
          moveOut([b.dataset.one]);
        };
      });
    }

    el.querySelector('#st-smart').disabled = !sel.length || busy;
    el.querySelector('#st-out').disabled = !sel.length || busy;
    el.querySelector('#st-restore').disabled = !sel.length || busy;
    el.querySelector('#st-del').disabled = !sel.length || busy;
    const s = el.querySelector('#st-selinfo');
    if (s) s.remove();
    if (sel.length) {
      const info = document.createElement('span');
      info.id = 'st-selinfo';
      info.className = 'muted';
      info.textContent = `已选 ${sel.length} 个 · ${U.fmtBytes(selBytes)}`;
      el.querySelector('#st-refresh').parentElement.appendChild(info);
    }
  }

  /** 选中的 rel 列表 */
  const picked = () => Array.from(checked);

  // ================================================================ 上传暂存区

  function paintStaging() {
    const d = staging || { batches: [], stray: [], count: 0, bytes: 0, cap_bytes: 0 };
    const cap = d.cap_bytes || 0;
    const pct = cap ? Math.min(100, (d.bytes / cap) * 100) : 0;
    const warn = cap && pct >= 85;
    const dirEl = el.querySelector('#stg-dir');
    if (dirEl) dirEl.textContent = d.dir || '';
    el.querySelector('#stg-stats').innerHTML =
      UI.stat('已用', U.fmtBytes(d.bytes), cap ? `上限 ${U.fmtBytes(cap)}` : '未设上限') +
      UI.stat('批次', (d.batches || []).length,
        d.stale_count ? `${d.stale_count} 个滞留` : '') +
      UI.stat('剩余容量', d.remaining == null ? '不限' : U.fmtBytes(d.remaining),
        cap ? `${pct.toFixed(1)}% 已用` : '');

    const batches = d.batches || [];
    const stray = d.stray || [];
    const sel = batches.filter((b) => stgChecked.has(b.batch));
    const box = el.querySelector('#stg-list');

    const bar = cap ? `<div class="stor-bar ${warn ? 'warn' : ''}">
        <div class="stor-fill" style="width:${pct}%"></div>
        <span class="stor-text">${U.fmtBytes(d.bytes)} / ${U.fmtBytes(cap)}
          （${pct.toFixed(1)}%）${warn ? '　⚠ 快满了，建议先入库或清理' : ''}</span>
      </div>` : '';

    // ★ 非音频杂项：要**看得见**。
    //   用户会往暂存区目录里手动扔东西（说明.txt、截图、压缩包…），
    //   这些不会入库 —— 但如果不列出来，它们占着盘却完全找不到，
    //   用户只会觉得"空间怎么少了"。列出来 + 说明为什么不动它们。
    const junk = [];
    batches.forEach((b) => (b.ignored || []).forEach((f) => junk.push(
      { ...f, batch: b.batch })));
    const junkTotal = junk.reduce((a, f) => a + (f.size || 0), 0);
    const junkHtml = junk.length ? `
      <div class="card" style="margin-top:12px">
        <div class="row" style="justify-content:space-between">
          <b>${U.icon('info')}<span>非音频文件 ${junk.length} 个</span></b>
          <span class="muted">共 ${U.fmtBytes(junkTotal)}　不会入库</span>
        </div>
        <div class="muted" style="font-size:12px;margin:6px 0">
          这些不是音乐文件（说明.txt、截图、压缩包等），
          <b>不会被入库</b>，但确实占着磁盘空间 ——
          需要的话可以直接删掉它们所在的批次。
        </div>
        <div class="tbl-wrap" style="max-height:22vh">
          <table class="data"><thead><tr>
            <th>文件</th><th class="right">大小</th><th>所属批次</th></tr></thead>
          <tbody>${junk.slice(0, 100).map((f) => `<tr>
            <td class="mono ell" title="${U.esc(f.rel || f.name)}">${U.esc(f.name)}</td>
            <td class="num">${U.fmtBytes(f.size || 0)}</td>
            <td class="mono muted">${U.esc(f.batch)}</td>
          </tr>`).join('')}</tbody></table></div>
      </div>` : '';

    if (!batches.length && !stray.length) {
      box.innerHTML = `${bar}<div class="empty" style="padding:26px">
        <div>暂存区是空的</div>
        <div class="muted" style="font-size:12px;margin-top:6px">
          上传文件后、还没走完「入库」向导的批次会出现在这里</div></div>`;
    } else {
      box.innerHTML = `${bar}
        <div class="tbl-wrap" style="max-height:44vh">
          <table class="data"><thead><tr>
            <th style="width:34px"></th><th>批次</th><th>状态</th>
            <th class="right">文件</th><th class="right">大小</th>
            <th>最后活动</th><th></th></tr></thead><tbody>
          ${batches.map((b) => `
            <tr data-batch="${U.esc(b.batch)}" class="${stgChecked.has(b.batch) ? 'sel' : ''}">
              <td><input type="checkbox" data-stgb="${U.esc(b.batch)}"
                    ${stgChecked.has(b.batch) ? 'checked' : ''}></td>
              <td class="mono">${U.esc(b.batch)}</td>
              <td>${b.stale ? '<span class="badge warn">滞留</span>' : '<span class="badge">正常</span>'}
                  ${b.orphan ? '<span class="badge err" title="清单文件丢失或没记全，已按磁盘实际情况列出">清单缺失</span>' : ''}
                  ${b.other_count ? `<span class="badge" title="非音频文件">非音频 ${b.other_count}</span>` : ''}</td>
              <td class="num">${b.audio_count}${b.other_count ? ` (+${b.other_count})` : ''}</td>
              <td class="num">${U.esc(b.human_bytes)}</td>
              <td class="muted" style="font-size:12px">${
                b.updated ? U.fmtAgo(b.updated) + `（${b.age_hours}h 前）` : '-'}</td>
              <td class="num nowrap">
                <button class="btn sm ghost" data-stgopen="${U.esc(b.batch)}"
                  title="在「入库」向导里继续处理这个批次">继续入库</button>
              </td>
            </tr>`).join('')}
          ${stray.map((f) => `
            <tr class="muted">
              <td></td>
              <td class="mono" title="${U.esc(f.path)}">（散落文件）${U.esc(f.name)}</td>
              <td><span class="badge err">孤儿</span></td>
              <td class="num">1</td>
              <td class="num">${U.fmtBytes(f.size)}</td>
              <td class="muted" style="font-size:12px">${U.fmtAgo(f.mtime)}</td>
              <td class="muted" style="font-size:11.5px">用「清空壳」清理</td>
            </tr>`).join('')}
          </tbody></table>
        </div>${junkHtml}`;
      box.querySelectorAll('[data-stgb]').forEach((cb) => {
        cb.onchange = () => {
          if (cb.checked) stgChecked.add(cb.dataset.stgb);
          else stgChecked.delete(cb.dataset.stgb);
          paintStaging();
        };
      });
      box.querySelectorAll('tr[data-batch]').forEach((tr) => {
        tr.onclick = (e) => {
          if (e.target.closest('button') || e.target.type === 'checkbox') return;
          const cb = tr.querySelector('[data-stgb]');
          if (cb) { cb.checked = !cb.checked; cb.onchange(); }
        };
      });
      box.querySelectorAll('[data-stgopen]').forEach((b) => {
        b.onclick = (e) => {
          e.stopPropagation();
          // 复用「入库」向导：打开上传对话框并预选这个批次
          if (typeof Uploader !== 'undefined' && Uploader.open) {
            Uploader.open({ batch: b.dataset.stgopen });
          } else {
            UI.toast('请到「文件浏览」→ 上传 打开入库向导处理该批次', 'info', 6000);
          }
        };
      });
    }
    const n = sel.length;
    el.querySelector('#stg-to-station').disabled = !n || busy;
    el.querySelector('#stg-del').disabled = !n || busy;
    el.querySelector('#stg-to-station').innerHTML =
      `${U.icon('history')}<span>转入中转站</span>${n ? `<b>(${n})</b>` : ''}`;
    el.querySelector('#stg-del').innerHTML =
      `${U.icon('trash')}<span>删除批次</span>${n ? `<b>(${n})</b>` : ''}`;
  }

  /** 把选中的暂存批次转进中转站（长期存放，不是丢掉）。 */
  async function toStation() {
    const list = Array.from(stgChecked);
    if (!list.length) return;
    busy = true; paintStaging();
    try {
      const r = await API.uploadBatchToStation({ batches: list });
      const parts = [];
      if (r.moved_count) parts.push(`已转入中转站 ${r.moved_count} 个文件`);
      if (r.failed_count) parts.push(`失败 ${r.failed_count} 个`);
      UI.toast(parts.join(' · ') || '没有可转移的文件',
        r.failed_count ? 'warn' : 'ok', 7000);
      if (r.failed_count) {
        UI.modal({
          title: '部分文件未能转移',
          body: `<div class="tbl-wrap" style="max-height:40vh"><table class="data">
            <thead><tr><th>批次</th><th>文件</th><th>原因</th></tr></thead><tbody>
            ${(r.failed || []).map((x) => `<tr><td class="mono">${U.esc(x.batch)}</td>
              <td>${U.esc(x.name || '')}</td>
              <td class="muted">${U.esc(x.error || '')}</td></tr>`).join('')}
          </tbody></table></div>`,
          footer: '<button class="btn primary">知道了</button>',
        });
      }
      stgChecked.clear();
      await load();
    } catch (e) {
      UI.toast('转移失败：' + e.message, 'err', 8000);
    } finally { busy = false; paintStaging(); }
  }

  /** 删除选中的暂存批次（不动音乐库）。 */
  async function deleteBatches() {
    const list = Array.from(stgChecked);
    if (!list.length) return;
    const ok = await UI.confirm(
      `删掉这 <b>${list.length}</b> 个批次？<br>
       <span class="muted">只是从暂存区移除，<b>不会</b>动音乐库。
       如果想留着以后再决定，请改用「转入中转站」。</span>`,
      { title: '删除暂存批次', okText: '删除', danger: true });
    if (!ok) return;
    busy = true; paintStaging();
    try {
      let done = 0;
      for (const b of list) {
        try { await API.uploadDelete(b); done++; } catch (e) { /* 记数即可 */ }
      }
      UI.toast(`已删除 ${done} 个批次`, 'ok', 5000);
      stgChecked.clear();
      await load();
    } finally { busy = false; paintStaging(); }
  }

  /** 清理空壳 / 滞留批次。 */
  async function cleanup(mode) {
    const d = staging || {};
    const label = mode === 'empty' ? '空壳（空批次目录 + 散落孤儿文件）'
      : `滞留批次（超过 ${d.stale_hours || 48} 小时未处理）`;
    const ok = await UI.confirm(
      `清理${label}？<br><span class="muted">只是从暂存区移除，<b>不会</b>动音乐库。</span>`,
      { title: '清理暂存区', okText: '清理', danger: true });
    if (!ok) return;
    busy = true; paintStaging();
    try {
      const r = await API.uploadCleanup({ mode });
      UI.toast(r.removed_count
        ? `已清理 ${r.removed_count} 项，释放 ${U.fmtBytes(r.freed)}`
        : '没有需要清理的内容', r.removed_count ? 'ok' : 'info', 6000);
      stgChecked.clear();
      await load();
    } catch (e) {
      UI.toast('清理失败：' + e.message, 'err', 8000);
    } finally { busy = false; paintStaging(); }
  }

  /** 智能分配：算出去向，然后**逐个问用户确认**再归位。 */
  async function smartAssign() {
    const rels = picked();
    if (!rels.length) return;
    busy = true; paint();
    try {
      // 先归入一个临时目标不现实（要真移动），所以这里只**算出建议**
      // 并展示，用户点确认后再调用 move-out。
      const files = (data.files || []).filter((f) => checked.has(id(f.rel)));
      const r = await API.stationSuggest({ files: rels });
      const all = r.plan || [];
      // 只对"确实需要移动"的做归位；keep=true 的是"暂不移动"，
      // 之前会把它们当成"归到根目录"，等于把歌从专辑目录里拽出来。
      const plan = all.filter((p) => p.dest);
      const kept = all.length - plan.length;
      if (!plan.length) {
        return UI.toast(kept
          ? `这 ${kept} 个文件按当前规则都不需要移动（库里没有更合适的现成目录）`
          : '没有算出可用的去向建议', 'warn', 7000);
      }
      const groups = new Map();
      plan.forEach((p) => {
        if (!groups.has(p.dest)) groups.set(p.dest, []);
        groups.get(p.dest).push(p);
      });
      const ok = await UI.confirm(
        `${plan.length} 个文件建议归入 <b>${groups.size}</b> 个目录：<br>
         <div style="margin-top:8px;max-height:260px;overflow:auto">
         ${Array.from(groups.entries()).map(([dest, list]) => `
           <div style="margin-bottom:6px">
             <b class="mono">${U.esc(dest)}</b>
             <span class="muted">（${list.length} 个）</span>
             <div class="muted" style="font-size:11.5px">
               ${U.esc(list[0].why || '')}</div>
           </div>`).join('')}
         </div>
         ${kept ? `<div class="muted" style="margin-top:6px">
           另有 ${kept} 个按当前规则不需要移动，将保持原样。</div>` : ''}`,
        { title: '智能分配去向', okText: `按建议归位 ${plan.length} 个` });
      if (!ok) return;
      let done = 0, fail = 0;
      for (const [dest, list] of groups) {
        try {
          const res = await API.stationMoveOut({
            source: destSource(), files: list.map((x) => x.rel), dest,
          });
          done += res.moved_count || 0;
          fail += res.failed_count || 0;
        } catch (e) { fail += list.length; }
      }
      UI.toast(`已归位 ${done} 个${fail ? ` · 失败 ${fail} 个` : ''}`,
        fail ? 'warn' : 'ok', 6000);
      await load();
      if (App.loadSources) await App.loadSources();
      if (App.renderSidebar) App.renderSidebar();
    } catch (e) {
      UI.toast('分配失败：' + e.message, 'err', 8000);
    } finally {
      busy = false; paint();
    }
  }

  /** 归入目录（手动选目录）。 */
  async function moveOut(only) {
    const rels = only || picked();
    if (!rels.length) return;
    const sid = destSource();
    if (!sid) return UI.toast('请先在左侧选择一个数据源', 'warn');
    const dest = await FolderPicker.open({
      source: sid, start: '',
      title: `把 ${rels.length} 个文件归入哪个目录`,
    });
    if (dest === null) return;
    busy = true; paint();
    try {
      const r = await API.stationMoveOut({ source: sid, files: rels, dest });
      UI.toast(`已归入 ${dest || '根目录'}：${r.moved_count} 个`
        + (r.failed_count ? ` · 失败 ${r.failed_count}` : ''), 'ok', 6000);
      checked.clear();
      await load();
      if (App.loadSources) await App.loadSources();
    } catch (e) {
      UI.toast('归入失败：' + e.message, 'err', 8000);
    } finally { busy = false; paint(); }
  }

  /** 还原到原位置（移入时记下了来源）。 */
  async function restore() {
    const rels = picked();
    if (!rels.length) return;
    // ★ 二次确认。还原会把文件**搬回它们原来的位置**，是一次真实写操作：
    //   原来点一下就直接发请求，误触没有补救机会。
    //   与「删除」不同，还原通常是善意的，所以用轻量确认（不要求输入短语）。
    const okGo = await UI.confirmHard({
      title: `还原 ${rels.length} 个文件到原位置？`,
      phrase: '确认还原',
      okText: '还原',
      details: rels.slice(0, 8).map((x) => String(x)),
    });
    if (!okGo) return;
    busy = true; paint();
    try {
      const r = await API.stationRestore({ files: rels });
      const parts = [];
      if (r.restored_count) parts.push(`已还原 ${r.restored_count} 个`);
      if (r.failed_count) parts.push(`失败 ${r.failed_count} 个`);
      UI.toast(parts.join(' · ') || '没有可还原的文件',
        r.failed_count ? 'warn' : 'ok', 7000);
      if (r.failed_count) {
        UI.modal({
          title: '部分文件未能还原',
          body: `<div class="muted" style="margin-bottom:8px">
              这些文件没有来源记录（比如不是从库里移入的），
              可以用「归入目录」手动指定位置：</div>
            <div class="tbl-wrap" style="max-height:40vh"><table class="data">
              <thead><tr><th>文件</th><th>原因</th></tr></thead><tbody>
              ${r.failed.map((x) => `<tr><td class="mono">${U.esc(x.name)}</td>
                <td class="muted">${U.esc(x.error)}</td></tr>`).join('')}
            </tbody></table></div>`,
          footer: '<button class="btn primary">知道了</button>',
        });
      }
      checked.clear();
      await load();
      if (App.loadSources) await App.loadSources();
    } catch (e) {
      UI.toast('还原失败：' + e.message, 'err', 8000);
    } finally { busy = false; paint(); }
  }

  async function remove() {
    const rels = picked();
    if (!rels.length) return;
    const ok = await UI.confirm(
      `把选中的 <b>${rels.length}</b> 个文件移入回收站？<br>
       <span class="muted">不是直接删除，仍可在回收站目录里找回。</span>`,
      { title: '移入回收站', okText: '移入回收站', danger: true });
    if (!ok) return;
    busy = true; paint();
    try {
      const r = await API.stationRemove({
        source: destSource(), files: rels, confirm: true,
      });
      UI.toast(`已移入回收站 ${r.trashed_count} 个`, 'ok', 6000);
      checked.clear();
      await load();
    } catch (e) {
      UI.toast('操作失败：' + e.message, 'err', 8000);
    } finally { busy = false; paint(); }
  }

  const destSource = () => State.activeSource
    || (State.sources[0] && State.sources[0].id) || '';

  App.registerView('station', { mount, render, load });
  return { load };
})();
