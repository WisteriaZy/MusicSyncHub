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
      </div>

      <div class="card" id="tr-card">
        <div class="row" style="justify-content:space-between;align-items:baseline">
          <h3 style="margin:0">回收站</h3>
          <span class="muted" id="tr-dir" style="font-size:11.5px"></span>
        </div>
        <div class="muted" style="font-size:12px;margin:4px 0 8px">
          「删除」= 移进数据源根目录下的
          <code class="mono">.music-sync-trash/日期/</code>,随时能还原;
          还原默认<b>回到原位置</b>、默认<b>跳过同名</b>,绝不覆盖。
          「彻底删除」要输入短语二次确认,不可恢复。
        </div>
        <div id="tr-stats" class="grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr))"></div>
        <div class="toolbar" style="margin-top:8px;flex-wrap:wrap">
          <label style="display:flex;align-items:center;gap:6px">数据源
            <select id="tr-src" style="width:auto"></select></label>
          <input type="search" id="tr-q" placeholder="按名称 / 原位置筛选" style="width:190px">
          <label style="display:flex;align-items:center;gap:6px">日期
            <select id="tr-day" style="width:auto"></select></label>
          <button class="btn sm" id="tr-refresh">${U.icon('refresh')}<span>刷新</span></button>
          <span class="sep-v"></span>
          <button class="btn sm" id="tr-all">全选</button>
          <button class="btn sm ghost" id="tr-none">全不选</button>
          <div class="grow"></div>
          <label style="display:flex;align-items:center;gap:6px">同名时
            <select id="tr-policy" style="width:auto">
              <option value="skip">跳过</option>
              <option value="rename">自动改名</option>
            </select></label>
          <button class="btn sm primary" id="tr-restore" data-exclusive disabled>还原选中</button>
          <button class="btn sm danger" id="tr-purge" data-exclusive disabled>彻底删除...</button>
        </div>
        <div id="tr-list" style="margin-top:10px"></div>
        <div class="muted" id="tr-sum" style="margin-top:6px;font-size:12px"></div>
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
    // ---- 回收站(区域在本页下方) ----
    el.querySelector('#tr-refresh').onclick = () => loadTrash();
    el.querySelector('#tr-src').onchange = () => { trashPicked.clear(); loadTrash(); };
    const trQ = el.querySelector('#tr-q');
    trQ.oninput = () => { trashFilter = trQ.value; paintTrashList(); };
    el.querySelector('#tr-day').onchange = () => {
      trashDay = el.querySelector('#tr-day').value;
      paintTrashList();
    };
    el.querySelector('#tr-all').onclick = () => {
      shownTrash().forEach((i) => trashPicked.add(i.rel));
      paintTrashList();
    };
    el.querySelector('#tr-none').onclick = () => {
      shownTrash().forEach((i) => trashPicked.delete(i.rel));
      paintTrashList();
    };
    el.querySelector('#tr-restore').onclick = doTrashRestore;
    el.querySelector('#tr-purge').onclick = doTrashPurge;
  }

  async function load() {
    const box = el.querySelector('#st-list');
    if (box) box.innerHTML = '<div class="pad muted">读取中转站…</div>';
    await Promise.all([loadStation(), loadStaging(), loadTrash()]);
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


  /* ============================================================
     回收站(本页下方那块区域)

     为什么放在这里、而不是文件浏览页的弹窗里:
       · 回收站是"看清楚再动手"的地方 —— 弹窗每操作一次就得重开,关掉就没了
         状态;还原/彻底删除都要等后台(还原目录还要重扫),弹窗一关用户就没有
         任何进度可看,连"刚才到底做了什么"都留不下;
       · 中转站页本来就是"这些文件还没安排"的地方,回收站是同一类东西;
       · 形态和上面两块一致:统计 + 工具栏 + 列表 + 就地刷新。
     回收站**只支持本地 / 映射盘**源(网络源的 `remove()` 语义各不一样),
     所以这里单独有个数据源下拉,只列本地源。
     ============================================================ */
  let trash = { items: [], bytes: 0, total: 0, root: '', source: '' };
  let trashPicked = new Set();      // 选中的条目 rel
  let trashFilter = '';
  let trashDay = '';
  let trashBusy = false;

  const trashSources = () => (State.sources || [])
    .filter((s) => (s.type || 'local') === 'local');

  /** 当前看哪个源的回收站:下拉优先 → 当前选中的源(本地)→ 第一个本地源。 */
  function trashSourceId() {
    const sel = el.querySelector('#tr-src');
    if (sel && sel.value) return sel.value;
    const loc = trashSources();
    if (loc.some((s) => s.id === State.activeSource)) return State.activeSource;
    return loc.length ? loc[0].id : '';
  }

  function paintTrashSources() {
    const sel = el.querySelector('#tr-src');
    if (!sel) return;
    const loc = trashSources();
    const want = trashSourceId();
    sel.innerHTML = loc.length
      ? loc.map((s) => `<option value="${U.esc(s.id)}">${U.esc(s.name || s.id)}</option>`).join('')
      : '<option value="">(没有本地数据源)</option>';
    if (want) sel.value = want;
    sel.disabled = !loc.length;
  }

  const shownTrash = () => {
    const q = (trashFilter || '').trim().toLowerCase();
    return (trash.items || []).filter((i) => {
      if (trashDay && String(i.date || '') !== trashDay) return false;
      if (!q) return true;
      return `${i.name || ''} ${i.origin || ''} ${i.rel || ''}`.toLowerCase().includes(q);
    });
  };

  /** 按日期分组 —— 回收站本身就是按 `<库根>/.music-sync-trash/<日期>/` 存的。 */
  function trashGroups() {
    const map = new Map();
    shownTrash().forEach((i) => {
      const d = String(i.date || i.rel || '').split('/')[0] || '';
      if (!map.has(d)) map.set(d, []);
      map.get(d).push(i);
    });
    return [...map.entries()].sort((a, b) => (a[0] < b[0] ? 1 : -1));   // 新日期在前
  }

  async function loadTrash() {
    paintTrashSources();
    const box = el.querySelector('#tr-list');
    const sid = trashSourceId();
    if (!sid) {
      if (box) {
        box.innerHTML = `<div class="pad muted">
          回收站只支持本地 / 映射盘数据源。先去「数据源」添加一个。</div>`;
      }
      return;
    }
    if (box) box.innerHTML = '<div class="pad muted">读取回收站...</div>';
    try {
      trash = await API.trash(sid);
    } catch (e) {
      if (box) {
        box.innerHTML = `<div class="pad" style="color:var(--danger)">${U.esc(e.message)}</div>`;
      }
      return;
    }
    // 已经不存在的条目要从选中集合里清掉(还原 / 彻底删除之后)
    const alive = new Set((trash.items || []).map((i) => i.rel));
    Array.from(trashPicked).forEach((k) => { if (!alive.has(k)) trashPicked.delete(k); });
    paintTrash();
  }

  function paintTrash() {
    const items = trash.items || [];
    const days = new Set(items.map((i) => String(i.date || '')).filter(Boolean));
    const dirEl = el.querySelector('#tr-dir');
    if (dirEl) dirEl.textContent = trash.root || '';
    const dirs = items.filter((i) => i.is_dir).length;
    const sorted = [...days].sort();
    const st = el.querySelector('#tr-stats');
    if (st) {
      st.innerHTML = UI.stat('条目', items.length, dirs ? `含 ${dirs} 个目录` : '没有目录')
        + UI.stat('占用', U.fmtBytes(trash.bytes || 0), '还在占磁盘,清掉才释放')
        + UI.stat('日期', days.size, days.size
          ? (sorted.length === 1 ? `全部是 ${sorted[0]}`
            : `${sorted[0]} ~ ${sorted[sorted.length - 1]}`)
          : '');
    }
    // 日期下拉:保留当前选择 —— 重画一次就跳回"全部"会让人以为筛选没生效
    const daySel = el.querySelector('#tr-day');
    if (daySel) {
      const opts = sorted.slice().reverse();
      if (trashDay && !opts.includes(trashDay)) trashDay = '';
      daySel.innerHTML = '<option value="">全部日期</option>'
        + opts.map((d) => `<option value="${U.esc(d)}">${U.esc(d)}</option>`).join('');
      daySel.value = trashDay;
    }
    paintTrashList();
  }

  function paintTrashList() {
    const box = el.querySelector('#tr-list');
    if (!box) return;
    const items = trash.items || [];
    if (!items.length) {
      box.innerHTML = `<div class="stg-empty">回收站是空的。<br>
        <span class="muted">删除文件时会移到
        <code class="mono">.music-sync-trash/日期/</code>,可在这里还原。</span></div>`;
      paintTrashButtons();
      return;
    }
    const groups = trashGroups();
    if (!groups.length) {
      box.innerHTML = '<div class="pad muted">没有符合筛选条件的条目。</div>';
      paintTrashButtons();
      return;
    }
    box.innerHTML = groups.map(([day, list]) => {
      const bytes = list.reduce((a, x) => a + (x.size || 0), 0);
      const allOn = list.length > 0 && list.every((x) => trashPicked.has(x.rel));
      return `
      <section class="tr-day" data-day="${U.esc(day)}">
        <div class="row" style="gap:8px;align-items:center;margin:8px 0 4px">
          <b class="mono">${U.esc(day || '(未记录日期)')}</b>
          <span class="muted" style="font-size:12px">${list.length} 项 · ${U.fmtBytes(bytes)}</span>
          <div class="grow"></div>
          <button class="btn sm ghost" data-dayall="${U.esc(day)}">
            ${allOn ? '取消本日' : '选中本日'}</button>
        </div>
        <div class="tbl-wrap" style="max-height:34vh">
          <table class="data"><thead><tr>
            <th></th><th>名称</th><th>原位置</th>
            <th class="right">大小</th><th class="right">时间</th>
          </tr></thead><tbody>
          ${list.map((it) => `<tr class="${trashPicked.has(it.rel) ? 'sel' : ''}">
            <td><input type="checkbox" data-tritem="${U.esc(it.rel)}"
              ${trashPicked.has(it.rel) ? 'checked' : ''}></td>
            <td class="ell" title="${U.esc(it.rel)}">${U.esc(it.name)}${
              it.is_dir ? ' <span class="tag">目录</span>' : ''}</td>
            <td class="ell muted" style="font-size:11.5px" title="${U.esc(it.origin || '')}">${
              it.origin ? U.esc(it.origin)
                : '<span class="muted">(未记录,还原时放回库根目录)</span>'}</td>
            <td class="num">${it.is_dir ? '—' : U.fmtBytes(it.size || 0)}</td>
            <td class="num">${it.mtime ? U.fmtDate(it.mtime) : '-'}</td>
          </tr>`).join('')}
          </tbody></table>
        </div>
      </section>`;
    }).join('');

    box.querySelectorAll('input[data-tritem]').forEach((cb) => {
      cb.onchange = () => {
        const rel = cb.dataset.tritem;
        if (cb.checked) trashPicked.add(rel); else trashPicked.delete(rel);
        const tr = cb.closest('tr');
        if (tr) tr.classList.toggle('sel', cb.checked);
        paintTrashButtons();
      };
    });
    box.querySelectorAll('[data-dayall]').forEach((b) => {
      b.onclick = () => {
        const day = b.dataset.dayall;
        const list = (trash.items || []).filter((i) => String(i.date || '') === day);
        const allOn = list.length > 0 && list.every((i) => trashPicked.has(i.rel));
        list.forEach((i) => {
          if (allOn) trashPicked.delete(i.rel); else trashPicked.add(i.rel);
        });
        paintTrashList();
      };
    });
    paintTrashButtons();
  }

  const chosenTrash = () => (trash.items || []).filter((i) => trashPicked.has(i.rel));

  function paintTrashButtons() {
    const sel = chosenTrash();
    const rb = el.querySelector('#tr-restore');
    const pb = el.querySelector('#tr-purge');
    if (rb) {
      if (!trashBusy) rb.textContent = '还原选中';
      rb.disabled = trashBusy || !sel.length;
    }
    if (pb) pb.disabled = trashBusy || !sel.length;
    const sum = el.querySelector('#tr-sum');
    if (sum) {
      sum.textContent = sel.length
        ? `已选 ${sel.length} 项 · ${U.fmtBytes(sel.reduce((a, x) => a + (x.size || 0), 0))}`
        : `共 ${(trash.items || []).length} 项 · ${U.fmtBytes(trash.bytes || 0)}`;
    }
  }

  /** 干活时把按钮定住并说清在干什么 —— 做完一定恢复。 */
  async function withTrashBusy(label, fn) {
    const rb = el.querySelector('#tr-restore');
    trashBusy = true;
    if (rb) { rb.dataset.label = rb.textContent; rb.textContent = label; }
    paintTrashButtons();
    try {
      return await fn();
    } finally {
      trashBusy = false;
      if (rb) rb.textContent = rb.dataset.label || '还原选中';
      paintTrashButtons();
    }
  }

  async function doTrashRestore() {
    const sel = chosenTrash();
    if (!sel.length) return UI.toast('先勾选要还原的条目', 'warn', 3000);
    const policy = (el.querySelector('#tr-policy') || {}).value || 'skip';
    await withTrashBusy('还原中...', async () => {
      try {
        const r = await App.runExclusive('trash', () => API.trashRestore({
          source: trashSourceId(), items: sel.map((x) => ({ rel: x.rel })), policy,
        }));
        const done = (r.results || []).filter((x) => x.state === 'done').length;
        const rest = (r.results || []).length - done;
        sel.forEach((x) => trashPicked.delete(x.rel));
        if (rest) {
          const names = (r.results || []).filter((x) => x.state !== 'done')
            .slice(0, 3).map((x) => x.rel).join('、');
          UI.toast(`已还原 ${done} 项;${rest} 项未还原(同名跳过或出错):${names}`,
            done ? 'warn' : 'err', 9000);
        } else {
          UI.toast(`已还原 ${done} 项`, 'ok', 5000);
        }
        await loadTrash();
        // 还原动了磁盘和索引:让文件浏览 / 曲库也跟着刷新,别留下过期列表
        try {
          const fv = App.viewApi('files');
          if (fv && fv.reload) fv.reload();
        } catch (e) { /* 视图没挂载就算了 */ }
        try { if (App.loadSources) await App.loadSources(); } catch (e) { /* ignore */ }
      } catch (e) {
        UI.toast('还原失败:' + (e && e.message ? e.message : e), 'err', 9000);
      }
    });
  }

  async function doTrashPurge() {
    const sel = chosenTrash();
    if (!sel.length) return UI.toast('先勾选要彻底删除的条目', 'warn', 3000);
    const ok = await UI.confirmHard({
      title: '彻底删除回收站里的文件?',
      phrase: '永久删除',
      message: `这 ${sel.length} 项会从磁盘上<b>永久删除</b>,不进回收站,无法恢复。`,
      details: sel.slice(0, 20).map((x) => x.rel),
      okText: '永久删除',
    });
    if (!ok) return;
    await withTrashBusy('删除中...', async () => {
      try {
        const r = await App.runExclusive('trash', () => API.trashPurge({
          source: trashSourceId(), items: sel.map((x) => ({ rel: x.rel })), confirm: true,
        }));
        sel.forEach((x) => trashPicked.delete(x.rel));
        await loadTrash();
        UI.toast(`已彻底删除 ${r.done} 项`, 'ok', 5000);
      } catch (e) {
        UI.toast('删除失败:' + (e && e.message ? e.message : e), 'err', 9000);
      }
    });
  }

  /** 从别的页面(文件浏览的「回收站」按钮)跳进来时,把这块区域滚到眼前。 */
  function focusTrash() {
    const card = el.querySelector('#tr-card');
    if (!card) return;
    try { card.scrollIntoView({ block: 'start', behavior: 'smooth' }); } catch (e) { card.scrollIntoView(); }
    const old = card.style.outline;
    card.style.outline = '2px solid var(--accent, #4a9eff)';
    setTimeout(() => { card.style.outline = old; }, 1400);
  }
  App.registerView('station', { mount, render, load });
  return { load, focusTrash };
})();
