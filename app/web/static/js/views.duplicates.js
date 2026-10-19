/* ============================================================
   视图：查重（分组、保留谁、清理谁 —— 全部由用户决定）

   关于"看起来只有几组"：这里有三层口径，界面上必须都写清楚，
   否则很容易以为结果被截断了——
     · 总组数       后端返回的全部重复组
     · 内容完全一致  组内存在字节相同的子簇 → 可以安全清理
     · 仅相似       同名/同歌但内容不同（不同音质版本）→ 只能人工决定
   筛选是"显示"层面的，永远不改变总数。
   ============================================================ */
(() => {
  let el = null;
  let selection = {};      // groupKey -> keepPath
  let skipped = new Set(); // 用户手动取消勾选的组（这一组不参与清理）
  let maxGroupCount = 0;
  let shown = 0;           // 当前已渲染的组数（渐进渲染）
  const PAGE = 60;         // 每次渲染多少组，避免上千组时一次性塞爆 DOM
  const PREF_SEP = 'msh.dedupDiffVersions';   // 偏好：是否区分版本（默认区分）

  /**
   * 版本标记 -> 中文短标签。
   *
   * ★ 映射**不再写在这里**。后端 `/api/system.version_kinds` 是唯一来源
   *   （来自 `paths.VERSION_KINDS` + `VERSION_LABELS`）。
   *
   *   踩过的坑：这里原来抄了一份 12 项的映射，注释还写着"与后端一致"，
   *   而后端有 14 项 —— 漏掉 `clip` 和 `speed`，这两类在界面上显示成英文。
   *   两份清单必然会分叉，所以改成跟着后端走，没有本地兜底表。
   */
  function verMap() {
    const ks = (window.State && State.system && State.system.version_kinds) || [];
    const out = Object.create(null);
    for (const it of ks) {
      if (it && it.kind) out[it.kind] = it.label || it.kind;
    }
    return out;
  }

  function verLabel(tags) {
    if (!tags) return '';
    const map = verMap();
    return String(tags).split('|').filter(Boolean).map((t) => {
      if (map[t]) return map[t];
      if (t.startsWith('v:')) return t.slice(2) + '版';
      return t;                      // 后端没认出来的原样显示
    }).join(' · ');
  }

  function mount(root) { el = root; }

  /** 读取"区分版本"偏好。默认 true：伴奏/重制版不该和原曲并成一组。 */
  function diffVersions() {
    try {
      const v = (typeof Prefs !== 'undefined')
    ? Prefs.get('dupSepVer', null) : localStorage.getItem(PREF_SEP);
      return v === null ? true : v === '1';
    } catch (e) { return true; }
  }

  function setDiffVersions(on) {
    try { Prefs.set('dupSepVer', !!on); } catch (e) { /* 隐私模式忽略 */ }
  }

  /** 从 items 重算"字节完全一致"的子簇（合并视图用）。 */
  function clustersFromItems(items) {
    const buckets = new Map();
    const order = [];
    items.forEach((t, i) => {
      const h = t.hash_full || (t.hash_state && t.hash_state !== 'none' ? t.hash_quick : '');
      const key = h ? 'h:' + h : '?' + i;
      if (!buckets.has(key)) { buckets.set(key, []); order.push(key); }
      buckets.get(key).push(t);
    });
    return order.map((k) => buckets.get(k));
  }

  function recompute(g) {
    const safe = (g.clusters || []).reduce(
      (a, c) => (c.length < 2 ? a : a + c.slice(1).reduce((x, t) => x + (t.size || 0), 0)), 0);
    return { ...g, safe_wasted_bytes: safe };
  }

  /** 按"是否区分版本"决定最终展示的组。
   *
   *  区分版本（默认）：后端已按 标题+艺术家+时长+版本标记 分组，直接用。
   *  不区分版本：按 base_key（去掉版本段）把同一首歌的各版本并成一个总体视图，
   *             组内再按哈希切成子簇——清理时依然只在"字节一致"的子簇内删，
   *             不同版本只是摆在一起方便你对照挑选。
   */
  function resolvedGroups() {
    const d = State.dup;
    if (!d || !d.groups) return [];
    // 后端的 suggested_keep 是"路径"，而这里的身份键是 source::path。
    // 不统一的话 selection 永远匹配不上任何一行，每行都会显示"将移入回收站"。
    if (!d._idNormalized) {
      d.groups.forEach((g) => {
        if (g.suggested_keep && !g.suggested_keep.includes('::')) {
          const hit = (g.items || []).find((t) => t.path === g.suggested_keep)
            || (g.items || [])[0];
          g.suggested_keep = hit ? itemId(hit) : g.suggested_keep;
        }
      });
      d._idNormalized = true;
    }
    if (diffVersions() || !d.groups.some((g) => g.base_key)) return d.groups;

    const buckets = new Map();
    const order = [];
    d.groups.forEach((g) => {
      const k = g.base_key || g.key;
      if (!buckets.has(k)) { buckets.set(k, []); order.push(k); }
      buckets.get(k).push(g);
    });
    return order.map((k) => {
      const members = buckets.get(k);
      if (members.length === 1) return members[0];
      const items = [];
      const seen = new Set();
      members.forEach((m) => (m.items || []).forEach((t) => {
        const id = itemId(t);
        if (seen.has(id)) return;
        seen.add(id);
        items.push(t);
      }));
      items.sort((a, b) => (b.quality || 0) - (a.quality || 0));
      const clusters = clustersFromItems(items);
      const keep = items[0] ? itemId(items[0]) : '';
      const rep = members[0];
      return recompute({
        ...rep,
        key: k,
        count: items.length,
        items,
        clusters,
        suggested_keep: keep,
        wasted_bytes: items.reduce((a, t) => a + (itemId(t) === keep ? 0 : (t.size || 0)), 0),
        total_bytes: items.reduce((a, t) => a + (t.size || 0), 0),
        size_equal: new Set(items.map((t) => t.size)).size === 1,
        exact: clusters.length === 1 && items.length > 1,
        version_tags: '',
        _merged: members.length,
      });
    });
  }

  function render() {
    const srcs = State.sources || [];
    const srcOpts = srcs.map((s) =>
      `<option value="${s.id}" ${State.activeSource === s.id ? 'selected' : ''}>${U.esc(s.name)}（${s.indexed || 0}）</option>`).join('');
    el.innerHTML = `
      <div class="toolbar">
        <b>查重范围</b>
        <select id="dp-src" style="width:auto;min-width:180px">
          <option value="">全部数据源（${srcs.length}）</option>${srcOpts}
        </select>
        <select id="dp-within" style="width:auto">
          <option value="same">各库内部查重</option>
          <option value="cross">跨库查重（两个库之间）</option>
        </select>
        <select id="dp-hash" style="width:auto" title="内容校验强度">
          <option value="quick" selected>抽样哈希（快，推荐）</option>
          <option value="full">全量哈希（慢，最准）</option>
          <option value="none">不算哈希（只看文件名+时长）</option>
        </select>
        <label class="inline" title="相似度阈值">严格度
          <select id="dp-thr" style="width:auto">
            <option value="0.95">很严（几乎同名）</option>
            <option value="0.9" selected>标准</option>
            <option value="0.82">宽松（捕捉改名的重复）</option>
          </select></label>
        <button class="btn primary" id="dp-run">${U.icon('search')}<span>开始查重</span></button>
        <div class="grow"></div>
        <label class="check" id="dp-sep-label"
               title="开启后：伴奏 / 重制版 / 现场版 / 翻唱 等会被当成本歌不同的录音，各自成组，绝不和原曲并在一起">
          <input type="checkbox" id="dp-sepver" ${diffVersions() ? 'checked' : ''}>
          区分不同版本（伴奏 / 重制版 / 现场版…）
        </label>
        <span class="muted" id="dp-info"></span>
      </div>
      <div id="dp-stats"></div>
      <div class="toolbar" id="dp-tools" style="display:none">
        <span>保留策略：</span>
        <select id="dp-policy" style="width:auto">
          <option value="best_quality">音质最好的</option>
          <option value="largest">体积最大的</option>
          <option value="oldest">最早的</option>
          <option value="newest">最新的</option>
        </select>
        <button class="btn sm" id="dp-apply">应用到全部组</button>
        <span class="sep-v"></span>
        <button class="btn sm ghost" id="dp-reset-skip" title="把所有被取消勾选的组重新勾上">重新勾选全部</button>
        <span class="sep-v"></span>
        <span class="muted" style="font-size:12.5px">显示：</span>
        <div class="seg" id="dp-filter">
          <button class="chip on" data-f="all">全部组</button>
          <button class="chip" data-f="exact">内容完全一致</button>
          <button class="chip" data-f="similar">仅相似</button>
          <button class="chip" data-f="skipped" title="只看被忽略的组，方便逐个确认">已忽略</button>
        </div>
        <div class="grow"></div>
        <button class="btn sm ghost" id="dp-keep-all"
          title="让所有组都参与清理（等于取消全部忽略）">全部勾选</button>
        <button class="btn sm ghost" id="dp-skip-all"
          title="忽略所有组的改动：这一步不会清理任何文件">忽略全部</button>
        <button class="btn sm ghost" id="dp-skip-only"
          title="切到「已忽略」筛选，逐个确认这些组是否真的不用管">只看已忽略</button>
        <button class="btn danger" id="dp-plan">${U.icon('trash')}<span>生成清理计划</span></button>
      </div>
      <div id="dp-filternote"></div>
      <div id="dp-groups"></div>`;
    if (!srcs.length) {
      el.querySelector('#dp-groups').innerHTML =
        `<div class="empty"><div class="big">${U.icon('copy', 'ico')}</div><div>先添加数据源并扫描</div></div>`;
      return;
    }
    el.querySelector('#dp-run').onclick = run;
    el.querySelector('#dp-apply').onclick = applyPolicy;
    el.querySelector('#dp-plan').onclick = cleanupPlan;
    el.querySelector('#dp-reset-skip').onclick = () => {
      if (!skipped.size) return UI.toast('当前没有被取消勾选的组', 'info', 2600);
      const n = skipped.size;
      skipped.clear();
      paintAll();
      UI.toast(`已重新勾选 ${n} 组`, 'ok', 3000);
    };
    // 批量勾选：组多的时候一组组点太累
    el.querySelector('#dp-skip-all').onclick = () => {
      const list = resolvedGroups();
      if (!list.length) return UI.toast('还没有查重结果', 'warn');
      skipped = new Set(list.map((g) => g.key));
      paintAll();
      UI.toast(`已忽略全部 ${list.length} 组改动（不会清理任何文件）`, 'ok', 4000);
    };
    el.querySelector('#dp-keep-all').onclick = () => {
      const list = resolvedGroups();
      if (!list.length) return UI.toast('还没有查重结果', 'warn');
      skipped.clear();
      paintAll();
      UI.toast('已重新勾选全部组', 'ok', 3000);
    };
    // 只看被忽略的组：方便逐个确认"这些真的不用管吗"
    el.querySelector('#dp-skip-only').onclick = () => {
      // 复用已有的筛选 chip 组，新增一个"已忽略"口径
      const chips = el.querySelectorAll('#dp-filter .chip');
      const target = Array.from(chips).find((c) => c.dataset.f === 'skipped');
      if (!target) return;
      chips.forEach((x) => x.classList.toggle('on', x === target));
      shown = 0;
      paintGroups();
      paintStats();
    };
    el.querySelectorAll('#dp-filter .chip').forEach((b) => {
      b.onclick = () => {
        el.querySelectorAll('#dp-filter .chip').forEach((x) => x.classList.toggle('on', x === b));
        shown = 0;
        paintGroups();
      };
    });
    // "区分版本"是偏好设置，改完立即重算视图并记住（下次打开仍是这个选择）
    const sep = el.querySelector('#dp-sepver');
    sep.onchange = () => {
      setDiffVersions(sep.checked);
      selection = {};
      shown = 0;
      paintAll();
      UI.toast(sep.checked
        ? '已区分版本：伴奏 / 重制版 / 现场版会各自成组'
        : '已合并版本：同一首歌的各版本会放在一组里对照', 'ok', 4200);
    };
    if (State.dup) paintAll();
  }

  function selectedSources() {
    const v = el.querySelector('#dp-src').value;
    // 不选具体数据源 = 全部（后端也支持传全部 id）
    return v ? [v] : (State.sources || []).map((s) => s.id);
  }

  function filterMode() {
    const on = el.querySelector('#dp-filter .chip.on');
    return on ? on.dataset.f : 'all';
  }

  async function run() {
    const btn = el.querySelector('#dp-run');
    btn.disabled = true;
    try {
      const opts = {
        sources: selectedSources(),
        within: el.querySelector('#dp-within').value,
        hash_mode: el.querySelector('#dp-hash').value,
        threshold: Number(el.querySelector('#dp-thr').value) || 0.9,
        client: (typeof TaskSync !== 'undefined' ? TaskSync.ME : ''),
      };
      // 查重现在跑在**后台任务**里：大库上要几十秒，
      // 同步请求会让界面只有一个转圈 —— 不知道进度、也没法取消，
      // 用户以为卡死就会刷新甚至强杀。
      const started = await API.duplicates(opts);
      const jid = started.job;
      btn.innerHTML = '<span class="spinner"></span><span>查重中…</span>';
      State.dup = await UI.watchJobDialog(jid, { title: '正在查重' });
      if (!State.dup || State.dup.cancelled) {
        UI.toast('查重已取消', 'info', 4000);
        btn.disabled = false; btn.innerHTML = U.icon('search') + '<span>开始查重</span>';
        return;
      }
      selection = {};
      skipped = new Set();     // 新一轮查重的组键不同，旧的取消勾选要清空
      // 必须按**当前显示口径**（区分/合并版本）来填"保留谁"：
      // 直接遍历原始 groups 的话，合并视图下的组键与这里填的键对不上，
      // 结果就是每行 radio 都没选中、全部显示"将移入回收站"。
      resolvedGroups().forEach((g) => { selection[g.key] = g.suggested_keep; });
      maxGroupCount = Math.max(0, ...(State.dup.groups || []).map((g) => g.count));
      shown = 0;
      btn.disabled = false; btn.innerHTML = U.icon('search') + '<span>开始查重</span>';
      paintAll();
      const s = State.dup.stats || {};
      UI.toast(s.groups
        ? `共 ${s.groups} 组重复（内容完全一致 ${s.groups_exact || 0} 组 · 仅相似 ${s.groups_similar || 0} 组）`
        : '没有发现重复文件',
        s.groups ? 'warn' : 'ok', 6000);
    } catch (e) {
      btn.disabled = false; btn.innerHTML = U.icon('search') + '<span>开始查重</span>';
      UI.toast('查重失败：' + U.esc(e.message), 'err', 7000);
    }
  }

  function paintAll() { paintStats(); paintGroups(); }

  function paintStats() {
    const d = State.dup;
    const box = el.querySelector('#dp-stats');
    if (!d) { box.innerHTML = ''; return; }
    const s = d.stats;
    const groups = resolvedGroups();          // 当前口径下的组（区分/合并版本）
    const active = groups.filter((g) => !skipped.has(g.key));
    const files = active.reduce((a, g) => a + g.count, 0);
    const exact = active.filter(hasIdenticalCluster).length;
    const similar = active.length - exact;
    const safeBytes = active.reduce((a, g) => a + (g.safe_wasted_bytes || 0), 0);
    const wasted = active.reduce((a, g) => a + (g.wasted_bytes || 0), 0);
    const merged = diffVersions() ? 0 : active.filter((g) => g._merged).length;
    const skipCount = groups.length - active.length;
    box.innerHTML = `
      <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr))">
        ${UI.stat('参与清理的组', active.length, skipCount ? `共 ${groups.length} 组 · 已取消 ${skipCount} 组` : `共 ${groups.length} 组`)}
        ${UI.stat('内容完全一致', exact, '组 · 可安全清理')}
        ${UI.stat('仅相似', similar, '组 · 需人工判断')}
        ${UI.stat('可释放空间', U.fmtBytes(safeBytes), '已验证一致的部分')}
        ${UI.stat('含相似总体积', U.fmtBytes(wasted), '上界，非建议删除量')}
        ${UI.stat('校验方式', { quick: '抽样哈希', full: '全量哈希', none: '仅文件名' }[s.hash_mode] || s.hash_mode)}
      </div>`;
    el.querySelector('#dp-tools').style.display = groups.length ? 'flex' : 'none';
    el.querySelector('#dp-info').textContent =
      `${active.length} 组 · ${files} 个文件` +
      (skipCount ? ` · 已取消 ${skipCount} 组` : '') +
      (merged ? ` · 合并了 ${merged} 首多版本歌曲` : '') +
      (diffVersions() ? ' · 已区分版本' : ' · 不区分版本');
    void d;
  }

  function assignDefault() {
    if (!State.dup) return;
    resolvedGroups().forEach((g) => {
      if (!selection[g.key]) selection[g.key] = g.suggested_keep;
    });
  }

  /** 组内是否存在"内容完全一致"的子簇（≥2 份）→ 可安全清理。 */
  function hasIdenticalCluster(g) {
    if ((g.clusters || []).some((c) => c.length >= 2)) return true;
    // 不算哈希时退化为"体积相同"，此时也给个可清理的判断依据
    return !!(g.size_equal && State.dup && State.dup.stats.hash_mode === 'none');
  }

  function visibleGroups() {
    if (!State.dup) return [];
    const list = resolvedGroups();
    const mode = filterMode();
    if (mode === 'exact') return list.filter(hasIdenticalCluster);
    if (mode === 'similar') return list.filter((g) => !hasIdenticalCluster(g));
    if (mode === 'skipped') return list.filter((g) => skipped.has(g.key));
    return list;
  }

  function paintGroups() {
    const box = el.querySelector('#dp-groups');
    const note = el.querySelector('#dp-filternote');
    const d = State.dup;
    if (!d) {
      note.innerHTML = '';
      box.innerHTML = `<div class="empty"><div class="big">${U.icon('copy', 'ico')}</div>
        <div>选择范围后点「开始查重」</div>
        <div class="muted">算法：文件名归一化 → 时长校验 → 内容哈希；不会自动删任何东西</div></div>`;
      return;
    }
    const list = resolvedGroups();
    const all = list.length;
    const skipCount = list.filter((g) => skipped.has(g.key)).length;
    // 保证每组都有明确的"保留谁"。之前只有 run() 会填 selection，
    // 于是切换视图后重绘时 radio 全是未选中、每行都显示"将移入回收站"，
    // 而清理计划却按 suggested_keep 保留一份——界面和实际行为对不上。
    list.forEach((g) => {
      if (!selection[g.key]) selection[g.key] = g.suggested_keep;
    });
    const groups = visibleGroups();
    const mode = filterMode();
    const activeList = list.filter((g) => !skipped.has(g.key));
    const exact = activeList.filter(hasIdenticalCluster).length;
    const similar = activeList.length - exact;
    const mergedInfo = !diffVersions()
      ? `（同一首歌的多个版本已合并显示：伴奏/重制版等只是摆在一起对照，
         <b>清理时仍只在"字节完全一致"的副本之间删</b>）`
      : '（伴奏 / 重制版 / 现场版等已各自成组，不会和原曲混在一起）';
    const skipInfo = skipCount
      ? `<br>已取消勾选 <b>${skipCount}</b> 组，它们不会进入清理计划。`
      : '';

    // 关键：把"被筛掉了多少"明明白白写出来，避免误以为结果被截断
    if (!all) {
      note.innerHTML = '';
    } else if (mode === 'all') {
      note.innerHTML = `<div class="filtnote">共 <b>${all}</b> 组：内容完全一致 <b>${exact}</b> 组（可安全清理）、
        仅相似 <b>${similar}</b> 组。当前显示全部 ${all} 组。${mergedInfo}${skipInfo}</div>`;
    } else if (mode === 'skipped') {
      note.innerHTML = `<div class="filtnote">
        当前只看<b>已忽略</b>的 ${groups.length} 组 —— 这些组不会进入清理计划。
        逐个确认后如果发现有不该忽略的，点该组的「重新勾选」即可。
        <button class="btn sm ghost" data-showall>返回全部 ${all} 组</button>${skipInfo}</div>`;
      bindShowAll(note);
    } else {
      const hidden = all - groups.length;
      note.innerHTML = `<div class="filtnote">已筛选：显示 <b>${groups.length}</b> 组 / 共 <b>${all}</b> 组，
        隐藏 <b>${hidden}</b> 组${mode === 'exact' ? '（仅相似，不会自动清理）' : '（内容完全一致，可安全清理）'}。
        <button class="btn sm ghost" data-showall>显示全部 ${all} 组</button>${skipInfo}</div>`;
      bindShowAll(note);
    }

    /** "显示/返回全部"按钮：两个分支共用，用 data 属性而不是重复的 id。 */
    function bindShowAll(scope) {
      const b = scope.querySelector('[data-showall]');
      if (!b) return;
      b.onclick = () => {
        el.querySelectorAll('#dp-filter .chip')
          .forEach((x) => x.classList.toggle('on', x.dataset.f === 'all'));
        shown = 0;
        paintGroups();
        paintStats();
      };
    }

    if (!groups.length) {
      box.innerHTML = `<div class="empty"><div class="big">${U.icon('check', 'ico')}</div>
        <div>${all ? (mode === 'exact' ? '没有"内容完全一致"的组，切到「全部组」可以看相似组'
                                      : '当前筛选下没有可显示的组') : '没有发现重复文件'}</div></div>`;
      return;
    }

    // 渐进渲染：一次 60 组，剩下的按需展开
    if (shown <= 0 || shown > groups.length) shown = Math.min(PAGE, groups.length);
    const slice = groups.slice(0, shown);
    box.innerHTML = '';
    slice.forEach((g, gi) => box.appendChild(groupEl(g, gi)));
    if (shown < groups.length) {
      const more = document.createElement('button');
      more.className = 'btn block';
      more.style.marginTop = '4px';
      more.textContent = `还有 ${groups.length - shown} 组未显示 —— 点此继续加载`;
      more.onclick = () => { shown = Math.min(shown + PAGE, groups.length); paintGroups(); };
      box.appendChild(more);
    }
  }

  function groupEl(g, gi) {
    const wrap = document.createElement('div');
    wrap.className = 'dup-group';
    const isSkipped = skipped.has(g.key);
    const clusters = g.clusters || [];
    const multiCluster = clusters.length > 1;
    const identicalClusterCount = clusters.filter((c) => c.length >= 2).length;
    const safe = identicalClusterCount > 0;
    const riskBadge = safe && !multiCluster
      ? '<span class="badge err">内容完全一致（已用哈希验证）</span>'
      : (multiCluster
        ? `<span class="badge warn">同名但内容不同 · ${clusters.length} 个版本</span>`
        : (g.size_equal ? '<span class="badge warn">体积相同 · 未验证</span>'
                        : '<span class="badge">仅相似（体积不同）</span>'));
    wrap.innerHTML = `
      <div class="dup-hd">
        <label class="check" title="取消勾选后这一组不参与清理计划（结果仍会保留显示）">
          <input type="checkbox" data-skip="${U.esc(g.key)}" ${isSkipped ? '' : 'checked'}>
        </label>
        <span class="badge">组 ${gi + 1}</span>
        <span class="t">${U.esc(g.title || '未知标题')}</span>
        <span class="muted">${U.esc(g.artist || '')}${g.album ? ' · ' + U.esc(g.album) : ''}</span>
        ${isSkipped ? '<span class="badge warn">已取消勾选 · 不参与清理</span>' : ''}
        ${riskBadge}
        ${g._merged ? `<span class="badge warn">含 ${g._merged} 个版本</span>` : ''}
        ${g.version_tags ? `<span class="badge">${U.esc(verLabel(g.version_tags))}</span>` : ''}
        <span class="badge">${g.count} 份</span>
        ${safe ? `<span class="badge ok">可安全释放 ${U.fmtBytes(g.safe_wasted_bytes || 0)}</span>`
               : `<span class="badge warn">含相似，不自动清理</span>`}
        <div class="grow"></div>
        <button class="btn sm ghost" data-toggle-skip="${U.esc(g.key)}">${isSkipped ? '重新勾选' : '取消勾选'}</button>
        <button class="btn sm ghost" data-suggest="${U.esc(g.key)}">按音质推荐保留</button>
      </div>`;
    wrap.classList.toggle('skipped', isSkipped);
    const skipBox = wrap.querySelector('[data-skip]');
    if (skipBox) skipBox.onchange = () => setSkipped(g.key, !skipBox.checked);
    wrap.querySelector('[data-toggle-skip]').onclick = () => setSkipped(g.key, !isSkipped);
    const clusterOf = {};
    clusters.forEach((cl, ci) => cl.forEach((t) => { clusterOf[itemId(t)] = ci + 1; }));
    const items = g.items.slice().sort((a, b) => (b.quality || 0) - (a.quality || 0));
    const best = items[0];
    items.forEach((t) => {
      const row = document.createElement('div');
      row.className = 'dup-item';
      const tid = itemId(t);
      const isKeep = selection[g.key] === tid;
      row.classList.toggle('keep', isKeep);
      const src = App.sourceById(t.source_id);
      const diff = (t.quality || 0) - (best.quality || 0);
      row.innerHTML = `
        <input type="radio" name="keep-${g.key}" data-keep="${U.esc(tid)}" ${isKeep ? 'checked' : ''}>
        ${UI.coverHtml(t, 'cover')}
        <div style="flex:1;min-width:0">
          <div class="ell" title="${U.esc(t.path)}"><b>${U.esc(t.name)}</b>
            <span class="side-${t.source_id === (State.cmp && State.cmpLeft) ? 'L' : 'R'}"></span>
            <span class="tag">${U.esc(src ? src.name : t.source_id)}</span>
            ${verLabel(t.version_tags || '') ? `<span class="tag hi">${U.esc(verLabel(t.version_tags))}</span>` : ''}
          </div>
          <div class="srcpath" title="${U.esc(t.path)}">${U.esc(t.path)}</div>
          <div class="why">${U.esc(U.qualityLabel(t))} · 修改于 ${U.fmtDate(t.mtime)}
            ${multiCluster ? ` · <b>内容版本 ${clusterOf[tid] || '?'}/${clusters.length}</b>` : ''}
            ${t.hash_quick ? ' · 哈希 ' + U.esc(t.hash_quick.slice(0, 10)) : ' · <span style="color:#ffd982">未校验</span>'}</div>
        </div>
        <div class="q">${U.fmtDur(t.duration)}<br><span class="muted">${U.fmtBytes(t.size)}</span></div>
        <div class="q" title="音质综合分">${(t.quality || 0).toFixed(1)}
          ${diff < 0 ? `<br><span style="color:#ff9daa">${diff.toFixed(1)}</span>` : '<br><span style="color:#8ce6ae">最佳</span>'}</div>
        <div style="text-align:right;min-width:150px">
          <span class="badge ${isKeep ? 'ok' : 'warn'}" data-role="keepbadge">${isKeep ? '保留这份' : '将移入回收站'}</span>
          <div style="margin-top:4px">
            <button class="btn icon sm" data-play="${t.id}" title="试听">${U.icon('play')}</button>
            <button class="btn icon sm ghost" data-path="${U.esc(t.path)}" title="复制路径">⧉</button>
          </div>
        </div>`;
      const radio = row.querySelector('[data-keep]');
      radio.onchange = () => {
        selection[g.key] = tid;
        paintGroups();
      };
      row.querySelector('[data-play]').onclick = () => {
        const tt = g.items.find((x) => x.id === t.id) || t;
        App.playTrack(tt, { label: U.qualityLabel(tt) + ' · ' + (src ? src.name : '') });
      };
      row.querySelector('[data-path]').onclick = () => navigator.clipboard.writeText(t.path).then(() => UI.toast('路径已复制', 'ok'));
      wrap.appendChild(row);
    });
    wrap.querySelector('[data-suggest]').onclick = () => {
      selection[g.key] = g.suggested_keep;
      paintGroups();
    };
    return wrap;
  }

  async function applyPolicy() {
    const d = State.dup;
    if (!d) return;
    const policy = el.querySelector('#dp-policy').value;
    // 按当前口径（区分/合并版本）把"保留谁"的建议算出来
    const r = await API.keepAdvice({ groups: resolvedGroups(), policy });
    (r.selections || []).forEach((s) => { selection[s.key] = s.keep; });
    paintGroups();
    UI.toast(`已按「${el.querySelector('#dp-policy').selectedOptions[0].textContent}」更新 ${r.selections.length} 组`, 'ok');
  }

  /** 每条记录的唯一键。跨库查重时两个库可能存在同样的 path，
   *  所以优先用后端给的 did（source_id::path）。 */
  function itemId(t) {
    return t.did || `${t.source_id || ''}::${t.path || ''}`;
  }

  function pickBest(items) {
    return items.slice().sort((a, b) => (b.quality || 0) - (a.quality || 0))[0];
  }

  /** 取消 / 恢复勾选某一组。取消后该组不进入清理计划，但结果仍然显示。 */
  function setSkipped(key, on) {
    if (on) skipped.add(key); else skipped.delete(key);
    paintStats();
    paintGroups();
    const n = skipped.size;
    UI.toast(on
      ? (n > 1 ? `已取消勾选 ${n} 组，它们不参与清理` : '已取消勾选该组，不参与清理')
      : (n ? `已重新勾选（还有 ${n} 组被取消）` : '已重新勾选'), 'ok', 3200);
  }

  const activeGroups = () => resolvedGroups().filter((g) => !skipped.has(g.key));

  async function cleanupPlan() {
    const d = State.dup;
    if (!d) return;
    // 清理永远针对"内容完全一致"的子簇，与当前显示筛选、以及是否区分版本都无关：
    // 合并版本视图下，不同版本属于不同子簇，也绝不会被碰到。
    // 用户手动取消勾选的组直接排除在外。
    const all = resolvedGroups();
    const groups = all.filter((g) => !skipped.has(g.key) && hasIdenticalCluster(g));
    if (!groups.length) {
      return UI.toast(skipped.size
        ? '可清理的组都被你取消勾选了：重新勾选后再试'
        : '没有"内容完全一致"的组：不同版本 / 不同音质永远不会被自动清理', 'warn', 6000);
    }
    const steps = [];
    groups.forEach((g) => {
      const keep = selection[g.key] || g.suggested_keep;
      // 只在"内容完全一致"的子簇内部删除多余的副本：
      // 同一首歌的不同音质版本属于不同子簇，绝不自动动它，交给用户自己决定。
      (g.clusters || [g.items]).forEach((cl) => {
        if (cl.length < 2) return;
        const keepInCluster = cl.some((t) => itemId(t) === keep)
          ? keep
          : itemId(cl.find((t) => itemId(t) === keep) || pickBest(cl));
        cl.forEach((t) => {
          if (itemId(t) === keepInCluster) return;
          steps.push({
            op: 'trash', src: t.source_id, src_path: t.path, dst: t.source_id, dst_path: t.path,
            size: t.size || 0, risk: 'high', kind: 'duplicate', group_key: g.key,
            reason: `与保留项内容完全一致（同一哈希），保留 ${keepInCluster}`,
            meta: { keep: keepInCluster, cluster: true },
          });
        });
      });
    });
    if (!steps.length) {
      return UI.toast('没有可安全清理的项：只有内容完全一致的副本才会被自动勾选', 'warn', 6000);
    }
    const bytes = steps.reduce((a, s) => a + (s.size || 0), 0);
    const keptGroups = groups.filter((g) => steps.some((s) => s.group_key === g.key)).length;

    // ★ 这里原来还有一次 `UI.confirm`，紧接着下面又弹 `UI.confirmHard`
    //   —— 连着两次确认，第二次还要手打「确认清理」。
    //   第一次纯属多余：它的内容（多少个文件、多少体积、涉及几组）
    //   下面那个预览弹窗里全都有，而且更准确（那才是预演之后的真实数字）。
    //   删掉第一次，把它的说明并进预览弹窗，用户少点一次、信息一点没少。

    const ctl = UI.modal({
      title: '清理执行',
      size: 'md',
      body: '<div class="muted">正在预演…</div>',
      footer: '<button class="btn ghost" data-act="close">关闭</button>',
    });
    const box = ctl.body;
    ctl.footer.querySelector('[data-act=close]').onclick = () => ctl.close();
    try {
      const dry = await API.execute({ steps, execute: false, policy: 'skip' });
      const blocked = dry.summary.blocked || 0;
      box.innerHTML = `
        <div class="muted" style="margin-bottom:8px">
          将把 <b>${steps.length}</b> 个重复文件（约 ${U.fmtBytes(bytes)}）移入回收站，
          涉及 <b>${keptGroups}</b> / ${all.length} 组。
          每组都会保留一份你选定的版本；删除是移到
          <span class="mono">.music-sync-trash/日期/</span>，可随时恢复。
          其余「仅相似」的组不会被触碰。
        </div>
        <div class="row wrap" style="margin-bottom:10px">
          <span class="badge ok">将删除 ${dry.summary.planned || 0}</span>
          ${blocked ? `<span class="badge err">被阻断 ${blocked}</span>` : ''}
        </div>
        <div class="row"><div class="grow"></div>
          <button class="btn danger" id="dp-exec">确认移入回收站</button></div>`;
      box.querySelector('#dp-exec').onclick = async () => {
        // 高风险复核：清理是不可逆的（回收站里还能找回，但库内已经是删除状态）
        const okHard = await UI.confirmHard({
          title: '确认清理重复文件',
          message: `将把 <b>${steps.length}</b> 个文件移入回收站。<br>
            <span class="muted">回收站里还能找回，但库内索引会同步删除。</span>`,
          phrase: '确认清理',
          okText: '移入回收站',
          details: steps.map((s) => s.src_path || s.dst_path || '').filter(Boolean),
        });
        if (!okHard) return;
        box.querySelector('#dp-exec').disabled = true;
        box.querySelector('#dp-exec').textContent = '执行中…';
        // 走 runExclusive：多页面同时操作会被拦下，且刷新页面不会丢任务。
        //
        // ★ 必须包 try/finally：独占位被别人占着时 `runExclusive` **会抛 409**
        //   （设计如此 —— 不排队，因为排队会基于过期的文件状态操作）。
        //   不接住的话按钮会永久卡在"执行中…"，用户只能刷新页面。
        let r;
        try {
          r = await App.runExclusive('trash', () => API.execute(
            { steps, execute: true, confirm: true, policy: 'skip' }));
        } catch (e) {
          UI.toast('清理没有执行：' + (e && e.message ? e.message : e), 'err', 10000);
          const b = box.querySelector('#dp-exec');
          if (b) { b.disabled = false; b.textContent = '确认移入回收站'; }
          return;
        }
        App.logOp('查重清理', r, steps);
        box.innerHTML = `
          <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(120px,1fr))">
            ${UI.stat('成功', r.summary.done || 0)}
            ${UI.stat('失败', r.summary.error || 0)}
            ${UI.stat('释放', U.fmtBytes(r.summary.bytes_trashed || 0))}
          </div>
          <div class="tbl-wrap" style="max-height:300px;margin-top:10px">
            <table class="data"><thead><tr><th>文件</th><th>结果</th></tr></thead><tbody>
            ${r.results.slice(0, 400).map((x) => `<tr>
              <td class="mono ell" title="${U.esc(x.src || '')}">${U.esc(x.src || '')}</td>
              <td>${x.state === 'done' ? '<span class="badge ok">已移入回收站</span>' : `<span class="badge err">${U.esc(x.detail || x.state)}</span>`}
                ${x.trash ? `<span class="muted mono"> → ${U.esc(x.trash)}</span>` : ''}</td>
            </tr>`).join('')}</tbody></table></div>`;
        await App.loadSources(); App.renderSidebar();
        UI.toast(`清理完成：${r.summary.done || 0} 个文件已移入回收站`, 'ok', 6000);
        run();
      };
    } catch (e) {
      box.innerHTML = `<div style="color:#ff9daa">${U.esc(e.message)}</div>`;
    }
  }

  App.registerView('duplicates', { mount, render, run });
})();
