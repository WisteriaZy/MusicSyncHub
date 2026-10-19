/* ============================================================
   候选区（跨目录暂存区）

   解决的痛点：想在多个文件夹里各挑几首歌一起处理时，以前每换一个目录
   选择就被清空，只能一个目录一个目录地重复操作。

   做法：选中项进这里暂存，可以在任意目录之间来回挑；暂存区里能对
   全部或单个文件做操作（移动 / 集合 / 回收站 / 试听 / 移除）。

   目标分组用 7 种颜色区分：
     · 去向相同的文件自动同色，一眼能看出"这几个一起去 A、那几个去 B"
     · 「待定」是警示色，不参与并发
     · 执行时按去向分列执行（正好对应 7 色），超了要求先合并去向，
       免得颜色不够用反而看不清
   ============================================================ */
const Staging = (() => {

  /** 一组高对比度颜色，配深色界面。索引 0..6，循环使用。 */
  const COLORS = [
    '#4cc2ff',  // 蓝
    '#7ee787',  // 绿
    '#ffd166',  // 黄
    '#ff8fab',  // 粉
    '#b392f0',  // 紫
    '#5ee6d0',  // 青
    '#ffa657',  // 橙
  ];
  /** 「待定」用的警示色：不是分组色，也不占用颜色槽位。 */
  const PENDING = '#ff6b6b';

  let el = null;                 // 容器
  let items = [];                // { did, path, name, title, artist, album, size,
                                 //   id, cover_url, srcFolder, dst, dstLabel }
  let dstSeq = 0;
  /** 去向 → 颜色槽位。先到先得，删掉之后别人不重排（否则颜色会乱跳）。 */
  const slots = new Map();
  let nextSlot = 0;
  let listeners = [];

  // ------------------------------------------------------------ 持久化
  /**
   * 候选区要**跨刷新存活**。
   *
   * 它的用途就是"跨目录凑齐再一起处理" —— 用户可能花十几分钟从好几个专辑里
   * 挑歌，中间刷新一下页面（或者切走再回来）就全没了，那这个功能根本没法用。
   *
   * 存在 localStorage：只放"待处理清单"这种界面状态，不放任何业务真相；
   * 写失败（隐私模式/配额满）就静默降级成不持久化，不影响使用。
   */
  const SAVE_KEY = 'stagingItems';
  let saveTimer = null;

  function saveNow() {
    if (typeof Prefs === 'undefined') return;
    try {
      // 只存必要的字段：封面 URL 能按 id 重建，不用占地方
      const slim = items.map((i) => ({
        did: i.did, path: i.path, name: i.name, title: i.title,
        artist: i.artist, album: i.album, size: i.size, id: i.id,
        srcFolder: i.srcFolder, dst: i.dst, dstLabel: i.dstLabel,
      }));
      Prefs.set(SAVE_KEY, slim);
    } catch (e) { /* 存不下就算了 */ }
  }

  function save() {
    // 合并短时间内的多次变更，避免每加一首就写一次 localStorage
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(saveNow, 400);
  }

  function restore() {
    if (typeof Prefs === 'undefined') return 0;
    let saved = null;
    try { saved = Prefs.get(SAVE_KEY, null); } catch (e) { return 0; }
    if (!Array.isArray(saved) || !saved.length) return 0;
    const seen = new Set();
    items = saved.filter((x) => x && x.did && x.path && !seen.has(x.did)
      && (seen.add(x.did), true)).map((x) => ({
        ...x,
        cover_url: x.id ? `/api/cover/${x.id}?size=96` : null,
      }));
    // 颜色槽按存下来的顺序重建，保证刷新前后同一去向颜色一致
    items.forEach((i) => { if (i.dst) colorFor(i.dst); });
    return items.length;
  }

  const keyOf = (t) => (t.did || (t.source_id ? `${t.source_id}::${t.path}` : t.path));

  function colorFor(dst) {
    if (!dst) return PENDING;
    if (!slots.has(dst)) {
      // 颜色只有 7 种，但**分组数量不设上限** —— 超了就循环复用颜色。
      // （之前是"最多 7 个去向，多了就拦住不让操作"，那是把颜色的限制
      //   强加给了功能，用户还得多做一步合并，属于本末倒置。）
      slots.set(dst, nextSlot % COLORS.length);
      nextSlot++;
    }
    return COLORS[slots.get(dst) % COLORS.length];
  }

  /** 换掉某个去向时释放它的颜色槽，让新去向能复用。 */
  function releaseColor(dst) {
    if (dst && slots.has(dst)) slots.delete(dst);
  }

  function targets() {
    // 按颜色槽位排序，保证顺序稳定
    const set = new Map();
    items.forEach((it) => {
      if (!it.dst) return;
      if (!set.has(it.dst)) set.set(it.dst, { dst: it.dst, label: it.dstLabel, n: 0, bytes: 0 });
      const g = set.get(it.dst);
      g.n++; g.bytes += it.size || 0;
    });
    return Array.from(set.values())
      .sort((a, b) => (slots.get(a.dst) ?? 99) - (slots.get(b.dst) ?? 99));
  }

  const pendingCount = () => items.filter((i) => !i.dst).length;

  // ------------------------------------------------------------ 数据操作

  function add(tracks) {
    const list = Array.isArray(tracks) ? tracks : [tracks];
    let added = 0;
    let skipped = 0;
    list.forEach((t) => {
      // ★ 候选区**只收音频**。
      //   候选区的用途是"凑齐后统一分配去向并执行移动"，它整条链路
      //   （移动计划、跨库传输、智能整理）都按曲目处理；把 .nfo / .jpg /
      //   .cue 放进来，轻则界面显示一堆没封面的怪条目，重则被
      //   跨库传输捎到对面去（而源那份已经进回收站了）。
      //   非音频文件仍然可以正常查看和做基础文件操作，只是不进候选区。
      if (!isAudioName(t.name || (t.path || '').split('/').pop() || '')) {
        skipped++;
        return;
      }
      const k = keyOf(t);
      if (!k || items.some((i) => i.did === k)) return;
      const folder = t.rel_folder
        || (t.path || '').replace(/\/[^/]*$/, '').replace(/^[A-Za-z]:\//, '');
      items.push({
        did: k,
        path: t.path,
        name: t.name || (t.path || '').split('/').pop(),
        title: t.title || '',
        artist: t.artist || '',
        album: t.album || '',
        size: t.size || 0,
        id: t.id || null,
        cover_url: t.cover_url || (t.id && t.has_cover ? `/api/cover/${t.id}?size=96` : null),
        srcFolder: t.rel_folder || folder || '',
        dst: '',
        dstLabel: '',
      });
      added++;
    });
    save();
    render();
    if (added) UI.toast(`已加入候选区 ${added} 个文件`, 'ok', 2600);
    if (skipped) {
      UI.toast(`跳过 ${skipped} 个非音频文件（候选区只收音频）`, 'warn', 5000);
    }
    return added;
  }

  function remove(did) {
    const it = items.find((i) => i.did === did);
    if (it) items = items.filter((i) => i.did !== did);
    // 如果这个去向没人用了，释放颜色
    if (it && it.dst && !items.some((i) => i.dst === it.dst)) releaseColor(it.dst);
    save();
    render();
  }

  function setDst(dids, dst, label) {
    const list = Array.isArray(dids) ? dids : [dids];
    list.forEach((did) => {
      const it = items.find((i) => i.did === did);
      if (!it) return;
      const old = it.dst;
      it.dst = dst || '';
      it.dstLabel = label || '';
      if (old && old !== dst && !items.some((i) => i.dst === old)) releaseColor(old);
    });
    save();
    render();
    return true;
  }

  function clear() {
    if (!items.length) return;
    items = [];
    slots.clear();
    nextSlot = 0;
    save();
    render();
  }

  /** 已就绪（都分配了去向）的条目；待定的不算。 */
  const ready = () => items.filter((i) => i.dst);

  function attach(node) {
    el = node;
    // 首次挂载时把上次的候选区恢复出来（刷新页面不丢）
    if (!attach._restored) {
      attach._restored = true;
      const n = restore();
      if (n) UI.toast(`已恢复候选区 ${n} 个文件`, 'info', 4000);
    }
    render();
  }

  function onChange(fn) { listeners.push(fn); }
  function fire() { listeners.forEach((f) => { try { f(); } catch (e) { /* ignore */ } }); }

  // ------------------------------------------------------- 音频判定
  /**
   * 这个文件名是不是音频。
   *
   * 扩展名清单**从后端拿**（`/api/system` 的 `audio_exts`），不在这里抄一份 ——
   * Python 侧已经有一条测试专门禁止抄第二份清单（`test_non_audio.py`），
   * 前端抄一份同样会走偏。拿不到（还没加载 / 旧后端）时**放行**：
   * 宁可漏挡也不能把用户的音频误挡在候选区外面。
   */
  let _audioExts = null;
  function audioExts() {
    if (_audioExts) return _audioExts;
    try {
      const s = (typeof State !== 'undefined' && State.system) || null;
      const list = s && s.audio_exts;
      if (Array.isArray(list) && list.length) {
        _audioExts = new Set(list.map((x) => String(x).toLowerCase()));
        return _audioExts;
      }
    } catch (e) { /* 拿不到就用 null */ }
    return null;
  }

  function isAudioName(name) {
    const n = String(name || '');
    const i = n.lastIndexOf('.');
    if (i < 0) return false;             // 没有扩展名 → 不是音频
    const ext = n.slice(i).toLowerCase();
    const set = audioExts();
    if (!set) return true;               // 清单还没拿到 → 放行（宁可漏挡）
    return set.has(ext);
  }

  // ------------------------------------------------------- 过期项检查（B12）
  /**
   * 候选区的条目是存在浏览器里的，可能已经放了好几天 ——
   * 期间文件被移走、删掉、或者 U 盘拔了。
   * 原来这些**过期项**照样能点「开始移动」，要等执行器报错才知道，白等一趟。
   *
   * 这里按数据源分组批量问一次"还在不在"，标在条目上并置灰。
   * 只是**提示**，不自动移除 —— 用户自己决定是删掉这条还是再找找。
   */
  async function checkStale() {
    const bySrc = new Map();
    items.forEach((it) => {
      const sid = it.source_id || (it.did || '').split('::')[0];
      if (!sid || !it.path) return;
      if (!bySrc.has(sid)) bySrc.set(sid, []);
      bySrc.get(sid).push(it);
    });
    if (!bySrc.size) return 0;
    let missingTotal = 0;
    // 后端单次上限 5000 条（`/api/files/exists`），超过会整批 400 ——
    // 那样所有条目都会被标成"无法确认"，看着像数据源挂了。
    // 候选区凑够几千首是正常用法，所以这里必须分批。
    const CHUNK = 4000;
    for (const [sid, list] of bySrc) {
      const miss = new Set();
      let failed = false;
      for (let i = 0; i < list.length; i += CHUNK) {
        const part = list.slice(i, i + CHUNK);
        let r;
        try {
          r = await API.filesExists({ source: sid, paths: part.map((x) => x.path) });
        } catch (e) {
          // 数据源离线（比如 U 盘拔了）不能把整批都标成"过期" ——
          // 那会让用户以为文件没了。标成"未知"更诚实。
          failed = true;
          break;
        }
        (r.missing || []).forEach((p) => miss.add(p));
      }
      if (failed) {
        list.forEach((x) => { x.stale = false; x.unknown = true; });
        continue;
      }
      list.forEach((x) => {
        x.stale = miss.has(x.path);
        x.unknown = false;
      });
      missingTotal += miss.size;
    }
    render();
    if (missingTotal) {
      UI.toast(`候选区里有 ${missingTotal} 个文件已经找不到了（已标灰），`
        + `执行前请先移除它们`, 'warn', 8000);
    }
    return missingTotal;
  }

  // ------------------------------------------------------------ 渲染

  function render() {
    if (!el) return;
    el.classList.toggle('has-items', items.length > 0);
    if (!items.length) {
      el.innerHTML = `<div class="stg-empty muted">
        跨目录挑歌：勾选任意目录里的文件后点「加入候选区」，这些文件会留在这里，
        切换目录不会丢失；凑齐后可在本区域统一分配去向并执行移动。
      </div>`;
      fire();
      return;
    }
    const groups = targets();
    const pend = pendingCount();
    const bytes = items.reduce((a, i) => a + (i.size || 0), 0);
    // 待定的排最后，其余按颜色槽位顺序，颜色相同的自然挨在一起
    const ordered = (list) => list.slice().sort((a, b) => {
      const sa = a.dst ? (slots.get(a.dst) ?? 50) : 99;
      const sb = b.dst ? (slots.get(b.dst) ?? 50) : 99;
      if (sa !== sb) return sa - sb;
      return (a.srcFolder || '').localeCompare(b.srcFolder || '', 'zh');
    });
    const pendItems = ordered(items.filter((i) => !i.dst));

    /** 单个文件行（列内的条目）。 */
    const itemHtml = (it, inPendingCol) => {
      const c = colorFor(it.dst);
      // 过期项（文件已经不在磁盘上了）要一眼能看出来，别让用户白等一趟
      const cls = it.stale ? ' stale' : (it.unknown ? ' unknown' : '');
      const badge = it.stale
        ? `<span class="stg-badge err" title="这个文件已经不在磁盘上了：可能被移走、删掉，或者所在的 U 盘/网络位置没连上">已失效</span>`
        : (it.unknown
          ? `<span class="stg-badge warn" title="连不上这个文件所在的数据源，暂时无法确认它还在不在">无法确认</span>`
          : '');
      return `
        <div class="stg-item${cls}" data-did="${U.esc(it.did)}" style="--c:${c}">
          <input type="checkbox" data-stgcheck="${U.esc(it.did)}">
          ${it.cover_url
            ? `<img class="cover xs" src="${U.esc(it.cover_url)}" loading="lazy" alt="">`
            : '<span class="cover xs ph"></span>'}
          <div class="stg-item-main">
            <div class="ell" title="${U.esc(it.path)}">${U.esc(it.name)}${badge}</div>
            <div class="ell muted" style="font-size:11px">
              ${U.esc(it.artist || '未知作者')} · ${U.fmtBytes(it.size)}
              · 来自 ${U.esc(it.srcFolder || '根目录')}</div>
          </div>
          <div class="stg-item-act">
            <button class="btn icon sm ghost" data-stgdst="${U.esc(it.did)}"
              title="${inPendingCol ? '分配去向' : '更改去向'}">${U.icon('folder')}</button>
            <button class="btn icon sm ghost" data-stgplay="${it.id || ''}"
              title="试听" ${it.id ? '' : 'disabled'}>${U.icon('play')}</button>
            <button class="btn icon sm ghost" data-stgdel="${U.esc(it.did)}"
              title="从候选区移除">✕</button>
          </div>
        </div>`;
    };

    /** 一个去向列。 */
    const colHtml = (g) => {
      const c = colorFor(g.dst);
      const list = ordered(items.filter((i) => i.dst === g.dst));
      return `
        <section class="stg-col" data-col="${U.esc(g.dst)}" style="--c:${c}">
          <header class="stg-col-hd">
            <span class="stg-dot" style="background:${c}"></span>
            <div class="stg-col-title" title="${U.esc(g.dst)}">
              <b>${U.esc(g.label || g.dst.split('/').pop() || g.dst)}</b>
              <span class="muted">${U.esc(g.dst)}</span>
            </div>
            <span class="stg-col-n">${list.length} 个 · ${U.fmtBytes(g.bytes)}</span>
            <button class="btn icon sm ghost" data-colall="${U.esc(g.dst)}"
              title="勾选本列全部文件">☑</button>
          </header>
          <div class="stg-col-list">${list.map((it) => itemHtml(it, false)).join('')}</div>
        </section>`;
    };

    const staleN = items.filter((i) => i.stale).length;
    el.innerHTML = `
      <div class="stg-head">
        <b>候选区</b>
        <span class="muted">共 ${items.length} 个文件 · ${U.fmtBytes(bytes)}</span>
        <span class="sep-v"></span>
        <span class="muted">去向 ${groups.length} 个</span>
        ${pend ? `<span class="stg-warn">${pend} 个未分配</span>` : ''}
        ${staleN ? `<span class="stg-warn" style="color:var(--danger)">
          ${staleN} 个已失效</span>` : ''}
        <div class="grow"></div>
        <button class="btn sm ghost" id="stg-check"
                title="逐个确认这些文件是否还在磁盘上（存放久了可能已被移走）">
          ${U.icon('search')}<span>检查失效</span></button>
        <button class="btn sm primary" id="stg-go"
                title="按各列去向分别执行移动">
          ${U.icon('check')}<span>执行移动</span></button>
        <button class="btn sm ghost" id="stg-clear">清空</button>
      </div>
      ${staleN ? `<div class="stg-note" style="color:var(--danger)">
        有 ${staleN} 个文件在磁盘上已经找不到了（已标灰）：
        可能被移走、删掉，或者所在的 U 盘 / 网络位置没连上。
        执行前建议用下方的批量操作把它们移除，否则会白跑一趟。
      </div>` : ''}
      ${pend ? `<div class="stg-note">
        有 ${pend} 个文件尚未分配去向（见最后一列）。可逐个分配，
        或勾选后使用下方的批量操作统一设置。
      </div>` : ''}
      <div class="stg-cols">
        ${groups.map(colHtml).join('')}
        ${pendItems.length ? `
          <section class="stg-col stg-col-pending" data-col="" style="--c:${PENDING}">
            <header class="stg-col-hd">
              <span class="stg-dot" style="background:${PENDING}"></span>
              <div class="stg-col-title"><b>未分配去向</b>
                <span class="muted">需要指定目标目录</span></div>
              <span class="stg-col-n">${pendItems.length} 个</span>
              <button class="btn icon sm ghost" data-colall="" title="勾选本列全部文件">☑</button>
            </header>
            <div class="stg-col-list">${pendItems.map((it) => itemHtml(it, true)).join('')}</div>
          </section>` : ''}
      </div>`;

    // 勾选
    el.querySelectorAll('[data-stgcheck]').forEach((cb) => {
      cb.onchange = () => { paintBulk(); };
    });
    el.querySelectorAll('[data-stgdel]').forEach((b) => {
      b.onclick = () => remove(b.dataset.stgdel);
    });
    el.querySelectorAll('[data-stgdst]').forEach((b) => {
      b.onclick = () => pickDst([b.dataset.stgdst]);
    });
    el.querySelectorAll('[data-stgplay]').forEach((b) => {
      b.onclick = () => {
        const it = items.find((i) => String(i.id) === b.dataset.stgplay);
        if (it) App.playTrack(it, { label: it.artist });
      };
    });
    // 点列头右侧的 ☑ = 勾选本列全部文件（便于整列统一改去向或移除）
    el.querySelectorAll('[data-colall]').forEach((b) => {
      b.onclick = () => {
        const dst = b.dataset.colall;
        el.querySelectorAll('[data-stgcheck]').forEach((cb) => {
          const it = items.find((i) => i.did === cb.dataset.stgcheck);
          cb.checked = !!(it && (it.dst || '') === dst);
        });
        paintBulk();
      };
    });
    const clearBtn = el.querySelector('#stg-clear');
    if (clearBtn) clearBtn.onclick = () => clear();
    // 「检查失效」：逐个 stat 一遍，标出文件已经不在了的条目
    const chkBtn = el.querySelector('#stg-check');
    if (chkBtn) {
      chkBtn.onclick = async () => {
        chkBtn.disabled = true;
        const old = chkBtn.innerHTML;
        chkBtn.innerHTML = '<span>检查中…</span>';
        try {
          const n = await checkStale();
          if (!n) UI.toast(`候选区里 ${items.length} 个文件都还在`, 'ok', 4000);
        } catch (e) {
          UI.toast('检查失败：' + U.esc(e.message), 'err', 6000);
        } finally {
          chkBtn.disabled = false;
          chkBtn.innerHTML = old;
        }
      };
    }
    // 「执行移动」如果包含了失效项，先挡一下 —— 让用户自己决定怎么处理，
    // 而不是跑一趟再回来说"有几个文件找不到"。
    const goBtn0 = el.querySelector('#stg-go');
    if (goBtn0 && staleN) {
      goBtn0.title = `有 ${staleN} 个文件已失效，执行会白跑一趟；建议先移除它们`;
    }
    // 实际执行交给文件浏览视图（它持有 Executor 那套调用的细节）
    const goBtn = el.querySelector('#stg-go');
    if (goBtn) {
      goBtn.disabled = !ready().length;
      goBtn.onclick = () => {
        if (!ready().length) {
          return UI.toast(`还有 ${pendingCount()} 个文件未分配去向`, 'warn');
        }
        document.dispatchEvent(new CustomEvent('staging:move'));
      };
    }

    paintBulk();
    fire();
  }

  const checkedDids = () => Array.from(el.querySelectorAll('[data-stgcheck]'))
    .filter((cb) => cb.checked).map((cb) => cb.dataset.stgcheck);

  /**
   * 渲染批量操作条。
   *
   * 注意：**只要候选区里有文件就一直显示**，哪怕一个都没勾。
   * 原来"没勾选就整条不显示"，结果"全选"按钮也一起没了 ——
   * 用户必须先手动勾一个才能看到全选，这不合逻辑（就是要全选才不需要手动勾）。
   * 没勾选时按钮置灰，但看得见。
   */
  function paintBulk() {
    const old = el.querySelector('#stg-bulk');
    if (old) old.remove();
    if (!items.length) return;
    const sel = checkedDids();
    const bar = document.createElement('div');
    bar.id = 'stg-bulk';
    bar.className = 'stg-bulk';
    const selItems = items.filter((i) => sel.includes(i.did));
    const bytes = selItems.reduce((a, i) => a + (i.size || 0), 0);
    // 全选按钮也做成三态：一个没勾 / 勾了一部分 / 全勾。
    // 之前永远写"全选"，勾了 3 个和勾了 20 个看起来一模一样。
    const total = items.length;
    const allOn = total > 0 && sel.length === total;
    const someOn = sel.length > 0 && sel.length < total;
    bar.innerHTML = `
      <span class="muted">已勾选 <b>${sel.length}</b> / ${total} 个${sel.length ? ' · ' + U.fmtBytes(bytes) : ''}</span>
      <span class="sep-v"></span>
      <label class="check" style="margin:0">
        <input type="checkbox" id="stg-ball-box" ${allOn ? 'checked' : ''}>
        <span>${allOn ? '取消勾选' : someOn ? '已部分勾选' : '全选'}</span>
      </label>
      <button class="btn sm ghost" id="stg-binv">反选</button>
      <button class="btn sm primary" id="stg-bsmart" ${sel.length ? '' : 'disabled'}
        title="用「智能整理」的同一套规则，为**勾选的这些文件**推算去向">
        ${U.icon('settings')}<span>智能分配去向</span></button>
      <button class="btn sm" id="stg-bdst" ${sel.length ? '' : 'disabled'}>${U.icon('folder')}<span>手动设去向…</span></button>
      <button class="btn sm ghost" id="stg-bdel">移出候选区</button>`;
    el.querySelector('.stg-head').after(bar);
    const ballBox = bar.querySelector('#stg-ball-box');
    ballBox.indeterminate = someOn;
    ballBox.onchange = () => {
      const want = ballBox.checked;
      el.querySelectorAll('[data-stgcheck]').forEach((cb) => { cb.checked = want; });
      paintBulk();
    };
    bar.querySelector('#stg-binv').onclick = () => {
      el.querySelectorAll('[data-stgcheck]').forEach((cb) => { cb.checked = !cb.checked; });
      paintBulk();
    };
    bar.querySelector('#stg-bsmart').onclick = () => smartAssign(sel);
    bar.querySelector('#stg-bdst').onclick = () => pickDst(sel);
    bar.querySelector('#stg-bdel').onclick = () => {
      if (!sel.length) return UI.toast('先勾选要移出的文件', 'warn', 4000);
      sel.forEach((d) => {
        const it = items.find((i) => i.did === d);
        if (it && it.dst && !items.some((x) => x.dst === it.dst && x.did !== d)) {
          releaseColor(it.dst);
        }
      });
      items = items.filter((i) => !sel.includes(i.did));
      render();
    };
  }

  /**
   * 智能分配去向：用「智能整理」的同一套规则，但**只处理勾选的这些文件**。
   *
   * 这里最容易搞错的一点：不是把整个库搬一遍，而是"我正在挑的这几个文件
   * 各自该去哪"。所以后端拿到的是**指定路径清单**，规划范围就限定在这几个
   * 文件上；库里已有的目录会被复用，达不到门槛的会新建或落到收纳区 ——
   * 判定顺序与「智能整理」完全一致（同一套 plan_by_album）。
   */
  /**
   * 智能分配的**选项**对话框。
   *
   * ★ 反馈："待选区的智能整理怎么一下就整理了，不能像文件浏览的智能整理
   *   那样有手工操作选项？"
   *
   *   原来这里把 `group_by='artist_album'`、`min_group=5` 写死，点一下
   *   就直接把去向全填了 —— 用户既不知道用了什么规则，也没机会改。
   *   现在先让用户选（与「文件浏览 → 智能整理」同一套参数），再预览，最后才写。
   */
  function smartOptions(count) {
    return new Promise((resolve) => {
      const ctl = UI.modal({
        title: `智能分配去向（${count} 个文件）`,
        body: `
          <div class="muted" style="margin-bottom:12px">
            规则与「文件浏览 → 智能整理」完全相同，只是**只处理你勾选的这些文件**。
          </div>
          <div class="row wrap" style="gap:14px;align-items:flex-end">
            <label class="col" style="gap:4px">分组依据
              <select id="so-gb" style="width:auto">
                <option value="artist_album">作者 + 专辑（推荐）</option>
                <option value="artist">仅作者</option>
                <option value="album">仅专辑</option>
              </select>
            </label>
            <label class="col" style="gap:4px">目录结构
              <select id="so-st" style="width:auto">
                <option value="nested">分层（作者/专辑）</option>
                <option value="flat">单层</option>
              </select>
            </label>
            <label class="col" style="gap:4px">最少同组
              <input type="number" id="so-mg" min="1" max="100" value="5" style="width:88px">
            </label>
          </div>
          <div class="muted" style="font-size:12px;margin-top:10px">
            「最少同组」= 一个目录里至少有几首才单独建目录；不够的会落到收纳区或留在原处。
          </div>`,
        footer: `<button class="btn" id="so-cancel">取消</button>
                 <button class="btn primary" id="so-ok">预览去向</button>`,
      });
      const finish = (v) => { ctl.close(); resolve(v); };
      ctl.footer.querySelector('#so-cancel').onclick = () => finish(null);
      ctl.footer.querySelector('#so-ok').onclick = () => {
        const gb = ctl.body.querySelector('#so-gb').value;
        const st = ctl.body.querySelector('#so-st').value;
        const mg = Math.max(1, Math.min(100,
          Number(ctl.body.querySelector('#so-mg').value) || 5));
        finish({ group_by: gb, structure: st, min_group: mg });
      };
    });
  }

  /** 预览：列出"哪些文件去哪个目录"，确认后才写入去向（此时还没移动任何文件）。 */
  function previewMoves(moves, opt) {
    return new Promise((resolve) => {
      const byDst = new Map();
      moves.forEach((m) => {
        if (!byDst.has(m.dst_dir)) byDst.set(m.dst_dir, []);
        byDst.get(m.dst_dir).push(m.path);
      });
      const rows = [...byDst.entries()]
        .sort((a, b) => b[1].length - a[1].length)
        .map(([dst, ps]) => `<tr><td class="ell" title="${U.esc(dst)}">${U.esc(dst)}</td>
          <td class="num">${ps.length}</td></tr>`).join('');
      const gbText = opt.group_by === 'artist_album' ? '作者+专辑'
        : opt.group_by === 'artist' ? '仅作者' : '仅专辑';
      const ctl = UI.modal({
        title: '确认去向',
        size: 'lg',
        body: `<div class="muted" style="margin-bottom:10px">
            规则：${gbText} · ${opt.structure === 'nested' ? '分层' : '单层'}
            · 最少同组 ${opt.min_group}
          </div>
          <div class="tbl-wrap" style="max-height:46vh;overflow:auto">
            <table class="tbl"><thead><tr><th>去向目录</th><th class="num">文件数</th></tr></thead>
            <tbody>${rows}</tbody></table>
          </div>
          <div class="muted" style="margin-top:10px">
            共 ${moves.length} 个文件 → ${byDst.size} 个目录。
            **确认后只是填好「去向」，不会立刻移动** —— 你还可以逐项改，最后再执行。
          </div>`,
        footer: `<button class="btn" id="pm-cancel">取消</button>
                 <button class="btn primary" id="pm-ok">采用这些去向</button>`,
      });
      ctl.footer.querySelector('#pm-cancel').onclick = () => { ctl.close(); resolve(false); };
      ctl.footer.querySelector('#pm-ok').onclick = () => { ctl.close(); resolve(true); };
    });
  }

  async function smartAssign(dids) {
    const sel = items.filter((i) => dids.includes(i.did));
    if (!sel.length) return;
    const sid = sel[0].did.includes('::') ? sel[0].did.split('::')[0] : State.activeSource;
    // ★ 不要在开头抓住按钮节点：`setDst()` 内部会 `render()`，
    //   而 render 是 `el.innerHTML = …` **整块重建** ——
    //   之前抓住的 `btn` 立刻变成脱离文档的死节点，
    //   第二次循环起改的是死节点，"分析中…"根本不会持续显示。
    //   每次现取。
    const setBtn = (state) => {
      const b = el.querySelector('#stg-bsmart');
      if (!b) return;
      if (state === 'busy') {
        b.disabled = true;
        b.textContent = '分析中…';
      } else {
        b.disabled = false;
        b.innerHTML = U.icon('settings') + '<span>智能分配去向</span>';
      }
    };
    // ① 先让用户选规则（以前这里写死 artist_album / min_group=5 直接执行）
    const opt = await smartOptions(sel.length);
    if (!opt) return;
    setBtn('busy');
    try {
      const paths = sel.map((i) => i.path);
      const r = await API.organizePlanPaths({ source: sid, paths, ...opt });
      const moves = r.moves || [];
      if (!moves.length) {
        return UI.toast('这几个文件按当前规则不需要移动（可能已经在正确位置）',
          'warn', 6000);
      }
      // ② 再预览：确认后才写入去向
      if (!(await previewMoves(moves, opt))) return;
      // 逐个写入去向（保持与手动设去向同一条路径，颜色/分组逻辑复用）
      const byDst = new Map();
      moves.forEach((m) => {
        if (!byDst.has(m.dst_dir)) byDst.set(m.dst_dir, []);
        byDst.get(m.dst_dir).push(m.path);
      });
      let n = 0;
      byDst.forEach((paths2, dst) => {
        const ids = sel.filter((i) => paths2.includes(i.path)).map((i) => i.did);
        if (ids.length) {
          setDst(ids, dst, dst.split('/').pop() || dst);
          n += ids.length;
        }
      });
      UI.toast(`已为 ${n} 个文件分配去向（分为 ${byDst.size} 组），可逐项调整`,
        'ok', 6000);
    } catch (e) {
      UI.toast('智能分配失败：' + e.message, 'err', 8000);
    } finally {
      // render() 已经重建过 DOM，这里现取按钮再恢复
      setBtn('idle');
      paintBulk();
    }
  }

  /** 给一批文件选去向：用带搜索的目录选择器，目标存在就复用（颜色也复用）。 */
  async function pickDst(dids) {
    const cur = items.find((i) => i.did === dids[0]);
    const sid = (cur && cur.did.includes('::'))
      ? cur.did.split('::')[0] : State.activeSource;
    const picked = await FolderPicker.open({
      source: sid,
      start: (cur && cur.dst) || (cur && cur.path ? cur.path.replace(/\/[^/]*$/, '') : ''),
      title: `给 ${dids.length} 个文件选去向`,
    });
    if (picked === null) return;
    setDst(dids, picked, picked.split('/').pop() || picked);
  }

  /** 供外部（批量移动等）取去向分组。 */
  function groupByDst() {
    const out = new Map();
    ready().forEach((it) => {
      if (!out.has(it.dst)) out.set(it.dst, []);
      out.get(it.dst).push(it);
    });
    return out;
  }

  return {
    attach, add, remove, setDst, clear, render,
    save, restore,
    checkedDids, pickDst, groupByDst,
    get items() { return items; },
    ready, pendingCount, targets, onChange,
    colorFor, COLORS,
    /** 批量检查候选区条目是否还在磁盘上（标出过期项） */
    checkStale,
    /** 供文件表格用：某个去向的颜色 */
    colorOf: (dst) => colorFor(dst),
    pendingColor: PENDING,
  };
})();
