/* ============================================================
   视图：双栏对比 + 同步计划
   ============================================================ */
(() => {
  let el = null;
  let filter = 'all';
  let q = '';
  const RENDER_LIMIT = 1500;

  function mount(root) { el = root; }

  function render() {
    const opts = State.sources.map((s) => `<option value="${s.id}">${U.esc(s.name)}（${s.indexed || 0}）</option>`).join('');
    el.innerHTML = `
      <div class="toolbar">
        <span class="side-L">左</span>
        <select id="cmp-left" style="width:auto;min-width:150px">${opts}</select>
        <select id="cmp-right" style="width:auto;min-width:150px">${opts}</select>
        <button class="btn primary sm" id="cmp-run">⇄ 开始对比</button>
        <span class="sep-v"></span>
        <span class="chip" data-f="all">全部</span>
        <span class="chip" data-f="matched">${U.icon('check')} 已配对</span>
        <span class="chip" data-f="only_left">仅左侧 ◀</span>
        <span class="chip" data-f="only_right">仅右侧 ▶</span>
        <span class="chip" data-f="conflict">名字不一致</span>
        <input type="text" id="cmp-q" placeholder="过滤…" style="width:150px">
        <div class="grow"></div>
        <button class="btn sm ok" id="cmp-plan">${U.icon('save')}<span>生成同步计划</span></button>
      </div>
      <div id="cmp-stats"></div>
      <div id="cmp-wrap"></div>`;

    const ids = State.sources.map((s) => s.id);
    const L = el.querySelector('#cmp-left'), R = el.querySelector('#cmp-right');
    L.value = State.cmpLeft || State.activeSource || ids[0] || '';
    R.value = State.cmpRight || ids.find((x) => x !== L.value) || ids[1] || '';
    el.querySelector('#cmp-run').onclick = () => run(L.value, R.value);
    el.querySelectorAll('.chip[data-f]').forEach((c) => {
      c.classList.toggle('on', c.dataset.f === filter);
      c.onclick = () => { filter = c.dataset.f; render(); };
    });
    const qi = el.querySelector('#cmp-q');
    qi.value = q;
    qi.oninput = U.debounce(() => { q = qi.value.trim(); paintRows(); }, 220);
    el.querySelector('#cmp-plan').onclick = () => planDialog(L.value, R.value);

    if (State.cmp) paintStats(), paintRows();
    else el.querySelector('#cmp-wrap').innerHTML =
      `<div class="empty"><div class="big">⇄</div><div>选择左右两个库，点「开始对比」</div>
       <div class="muted">对比只看文件名 + 时长 + 标签，不要求两边元数据一致</div></div>`;
  }

  async function run(left, right) {
    if (!left || !right) return UI.toast('请选择左右两个数据源', 'warn');
    if (left === right) return UI.toast('左右不能是同一个数据源', 'warn');
    State.cmpLeft = left; State.cmpRight = right;
    const btn = el.querySelector('#cmp-run');
    btn.disabled = true; btn.textContent = '对比中…';
    try {
      // 对比走后台任务：大库上要几十秒，能看到进度、能取消、刷新不丢
      const started = await API.compare({
        left, right, client: (typeof TaskSync !== 'undefined' ? TaskSync.ME : ''),
      });
      const r = started.job
        ? await UI.watchJobDialog(started.job, { title: '正在对比两个库' })
        : started;
      if (!r || r.cancelled) {
        UI.toast('对比已取消', 'info', 4000);
        return;
      }
      State.cmp = r;
      paintStats(); paintRows();
    } catch (e) {
      UI.toast('对比失败：' + U.esc(e.message), 'err');
    } finally { btn.disabled = false; btn.textContent = '⇄ 开始对比'; }
  }

  function groupRows() {
    const c = State.cmp;
    if (!c) return [];
    const out = [];
    const mq = q.toLowerCase();
    const hit = (t) => !mq || JSON.stringify(t).toLowerCase().includes(mq);
    (c.matches || []).forEach((m) => {
      if (hit(m.left) || hit(m.right)) out.push({ kind: 'pair', m });
    });
    (c.only_left || []).forEach((t) => {
      if (hit(t)) out.push({ kind: 'only_left', t, m: null });
    });
    (c.only_right || []).forEach((t) => {
      if (hit(t)) out.push({ kind: 'only_right', t });
    });
    return out;
  }

  function paintStats() {
    const c = State.cmp;
    if (!c) return;
    const s = c.stats;
    const L = App.sourceById(State.cmpLeft), R = App.sourceById(State.cmpRight);
    const totalLeft = s.left_total_all || s.left_total, totalRight = s.right_total_all || s.right_total;
    const truncated = (s.scanned_left < totalLeft) || (s.scanned_right < totalRight);
    el.querySelector('#cmp-stats').innerHTML = `
      <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr))">
        ${UI.stat('已配对', s.matched, '首')}
        ${UI.stat('<span class="side-L">◀ 仅左侧</span>', s.only_left, '首')}
        ${UI.stat('<span class="side-R">仅右侧 ▶</span>', s.only_right, '首')}
        ${UI.stat('内容完全一致', s.identical, '首')}
        ${UI.stat('需人工确认', s.suspect, '首')}
        ${UI.stat('左库总量', totalLeft)}
        ${UI.stat('右库总量', totalRight)}
      </div>
      ${truncated ? `<div class="badge warn" style="display:block;padding:8px 10px">⚠ 曲目过多已截断：仅对比了左 ${s.scanned_left}/${totalLeft} · 右 ${s.scanned_right}/${totalRight} 条。
        请先用侧栏选目录再对比，或在「设置 → 单次对比上限」调大。</div>` : ''}`;
  }

  function paintRows() {
    const wrap = el.querySelector('#cmp-wrap');
    if (!State.cmp) return;
    const L = App.sourceById(State.cmpLeft), R = App.sourceById(State.cmpRight);
    let rows = groupRows();
    if (filter === 'matched') rows = rows.filter((r) => r.kind === 'pair');
    else if (filter === 'only_left') rows = rows.filter((r) => r.kind === 'only_left');
    else if (filter === 'only_right') rows = rows.filter((r) => r.kind === 'only_right');
    else if (filter === 'conflict') rows = rows.filter((r) => r.kind === 'pair' && r.m.confidence !== 'identical' && r.m.name_sim < 0.99);
    const shown = rows.slice(0, RENDER_LIMIT);

    const cell = (t, side) => {
      if (!t) return `<div class="cmp-row" style="opacity:.35"><span class="nm muted">—</span></div>`;
      return `<div class="cmp-row" data-tid="${t.id}" title="${U.esc(t.path || t.name || '')}">
        ${UI.coverHtml(t, 'cover sm')}
        <span class="nm">${U.esc(t.name || U.base(t.path))}</span>
        <span class="tag ${t.is_lossless ? 'll' : ''}">${U.esc(t.codec || '')}${t.bitrate ? ' ' + Math.round(t.bitrate / 1000) + 'k' : ''}</span>
        <span class="sz">${U.fmtDur(t.duration)}</span>
        <span class="sz">${U.fmtBytes(t.size)}</span>
        <button class="btn icon sm" data-play="${t.id}" title="试听">${U.icon('play')}</button>
      </div>`;
    };

    // 左右两栏用同一套 DOM 结构，滚动位置互相同步
    wrap.innerHTML = `
      <div class="cmp-body" style="border:1px solid var(--line);border-radius:12px;overflow:hidden;max-height:none">
        <div class="pane" id="pane-l" style="border-right:1px solid var(--line)">
          <div style="position:sticky;top:0;background:#182030;z-index:3;display:flex;gap:8px;padding:8px 11px;border-bottom:1px solid var(--line)">
            <span class="side-L">◀ 左</span><b>${U.esc(L ? L.name : '')}</b><span class="muted">${U.esc(L ? L.type : '')}</span>
          </div>
          <div id="rows-l"></div>
        </div>
        <div class="pane" id="pane-r">
          <div style="position:sticky;top:0;background:#182030;z-index:3;display:flex;gap:8px;padding:8px 11px;border-bottom:1px solid var(--line)">
            <span class="side-R">右 ▶</span><b>${U.esc(R ? R.name : '')}</b><span class="muted">${U.esc(R ? R.type : '')}</span>
          </div>
          <div id="rows-r"></div>
        </div>
      </div>
      <div class="muted" style="font-size:12px;margin-top:6px">
        显示 ${shown.length} / ${rows.length} 行${rows.length > RENDER_LIMIT ? '（已截断，请用过滤缩小范围）' : ''}
        · 行左侧色条：<span style="color:#38c172">绿=已配对</span> / <span style="color:#f2b02c">黄=单侧独有</span> / <span style="color:#a678f0">紫=需确认</span>
      </div>`;

    const rowsL = wrap.querySelector('#rows-l');
    const rowsR = wrap.querySelector('#rows-r');
    const frag = document.createDocumentFragment();
    const fragR = document.createDocumentFragment();
    shown.forEach((r) => {
      const div = document.createElement('div');
      if (r.kind === 'pair') {
        const m = r.m;
        const cls = (m.confidence === 'low' || m.confidence === 'medium') ? 'conflict' : 'matched';
        div.innerHTML = cell(m.left).replace('class="cmp-row"', `class="cmp-row ${cls}"`);
        const div2 = document.createElement('div');
        div2.innerHTML = cell(m.right).replace('class="cmp-row"', `class="cmp-row ${cls}"`);
        div.dataset.pair = '1'; div2.dataset.pair = '1';
        div.title = div2.title = m.reasons.join('；') + `（置信度 ${(m.score * 100).toFixed(0)}%）`;
        const idx = frag.childNodes.length;
        frag.appendChild(div); fragR.appendChild(div2);
        div.onclick = div2.onclick = () => pairDialog(m, idx);
      } else if (r.kind === 'only_left') {
        div.innerHTML = cell(r.t).replace('class="cmp-row"', 'class="cmp-row only"');
        const div2 = document.createElement('div');
        div2.innerHTML = cell(null);
        frag.appendChild(div); fragR.appendChild(div2);
      } else {
        div.innerHTML = cell(null);
        const div2 = document.createElement('div');
        div2.innerHTML = cell(r.t).replace('class="cmp-row"', 'class="cmp-row only"');
        frag.appendChild(div); fragR.appendChild(div2);
      }
    });
    rowsL.appendChild(frag);
    rowsR.appendChild(fragR);
    wrap.querySelectorAll('[data-play]').forEach((b) => {
      b.onclick = (e) => {
        e.stopPropagation();
        const t = findTrack(Number(b.dataset.play));
        if (t) App.playTrack(t);
      };
    });
    // 双向滚动同步
    let lock = false;
    const sync = (from, to) => {
      from.onscroll = () => {
        if (lock) return;
        lock = true; to.scrollTop = from.scrollTop; requestAnimationFrame(() => { lock = false; });
      };
    };
    sync(rowsL.parentElement, rowsR.parentElement);
    sync(rowsR.parentElement, rowsL.parentElement);
  }

  function findTrack(id) {
    const c = State.cmp || {};
    for (const m of (c.matches || [])) {
      if (m.left.id === id) return m.left;
      if (m.right.id === id) return m.right;
    }
    return (c.only_left || []).find((t) => t.id === id) || (c.only_right || []).find((t) => t.id === id);
  }

  // ------------------------------------------------------------ 配对详情
  function pairDialog(m, idx) {
    const side = (t, label, cls) => `
      <div class="card" style="margin:0">
        <h3><span class="${cls}">${label}</span> ${U.esc(t.name)}</h3>
        <div class="row" style="gap:12px;align-items:flex-start">
          ${t.has_cover ? `<img class="cover lg" src="/api/cover/${t.id}?size=300">` : `<div class="cover lg cover ph">${U.icon('music', 'ico huge')}</div>`}
          <div class="kv" style="flex:1">
            <div class="k">标题</div><div>${U.esc(t.title || '-')}</div>
            <div class="k">作者</div><div>${U.esc(t.artist || '-')}</div>
            <div class="k">专辑</div><div>${U.esc(t.album || '-')}</div>
            <div class="k">格式</div><div>${U.esc(t.codec || '-')} ${t.is_lossless ? '<span class="tag ll">无损</span>' : ''}</div>
            <div class="k">码率</div><div>${U.fmtBitrate(t.bitrate)} · ${U.fmtRate(t.sample_rate)}${t.bits_per_sample ? ' · ' + t.bits_per_sample + 'bit' : ''}</div>
            <div class="k">时长</div><div>${U.fmtDur(t.duration)}</div>
            <div class="k">体积</div><div>${U.fmtBytes(t.size)}</div>
            <div class="k">音质分</div><div><b>${(t.quality || 0).toFixed(2)}</b></div>
            <div class="k">路径</div><div class="mono" style="word-break:break-all;font-size:11px">${U.esc(t.path)}</div>
          </div>
        </div>
        <button class="btn sm" style="margin-top:10px" data-play="${t.id}">${U.icon('play')}<span>试听</span></button>
      </div>`;
    const ctl = UI.modal({
      title: `配对详情 · 置信度 ${(m.score * 100).toFixed(0)}%（${U.confText(m.confidence)}）`,
      size: 'lg',
      body: `
        <div class="grid c2">${side(m.left, '左', 'side-L')}${side(m.right, '右', 'side-R')}</div>
        <div class="card" style="margin-top:12px">
          <h3>判定依据</h3>
          <ul style="margin:0;padding-left:18px;line-height:1.9">
            ${m.reasons.map((r) => `<li>${U.esc(r)}</li>`).join('')}
            <li>名称相似度：${(m.name_sim * 100).toFixed(1)}%
                ${m.duration_diff !== null && m.duration_diff !== undefined ? `· 时长差：${m.duration_diff.toFixed(2)} 秒` : ''}
                ${m.same_size ? '· 体积完全相同' : `· 体积差：${U.fmtBytes(Math.abs(m.size_delta))}`}
                · 音质差：${m.quality_delta > 0 ? '左更好 +' : '右更好 '}${m.quality_delta.toFixed(2)}</li>
          </ul>
          ${m.confidence === 'low' || m.confidence === 'medium'
            ? '<div class="badge warn" style="margin-top:8px">置信度不高：可能是不同版本（现场 / 混音 / 剪辑），请试听后决定</div>'
            : ''}
        </div>`,
      footer: `<span class="muted grow">可分别试听左右两份，再决定保留哪一边</span>
               <button class="btn ghost" data-act="close">关闭</button>`,
    });
    ctl.body.querySelectorAll('[data-play]').forEach((b) => {
      b.onclick = () => {
        const t = [m.left, m.right].find((x) => x.id === Number(b.dataset.play));
        if (t) App.playTrack(t, { label: '对比试听 · ' + U.qualityLabel(t) });
      };
    });
    ctl.footer.querySelector('[data-act=close]').onclick = () => ctl.close();
    void idx;
  }

  // ------------------------------------------------------------ 同步计划
  function planDialog(left, right) {
    if (!left || !right || left === right) return UI.toast('请先选择两个不同的数据源', 'warn');
    const L = App.sourceById(left), R = App.sourceById(right);
    const ctl = UI.modal({
      title: `生成同步计划 · ${L ? L.name : left} ⇄ ${R ? R.name : right}`,
      size: 'lg',
      body: `
        <div class="grid c2">
          <div class="card">
            <h3>补齐方向</h3>
            <div class="field">
              <select id="pl-dir">
                <option value="left_to_right">左 ➜ 右（把左库独有的文件复制到右）</option>
                <option value="right_to_left">右 ➜ 左（把右库独有的文件复制到左）</option>
                <option value="both">双向补齐（两边互相补）</option>
              </select>
            </div>
            <label class="check"><input type="checkbox" id="pl-overwrite">
              目标存在同名文件时覆盖（默认跳过）</label>
            <div class="hint" style="margin-left:22px">
              勾上它等于把下面的「目标同名时」设成"覆盖"。两者本来就该是一个开关 ——
              以前这里是**两个互不相干的控件**：这个勾选框只影响风险标签，
              真正决定行为的却只有下面那个下拉框，很容易以为勾了就已经覆盖了。
            </div>
            <label class="check" style="margin-top:6px"><input type="checkbox" id="pl-conflict"> 同时统一文件名（把一侧改成另一侧的名字）</label>
            <label class="check" style="margin-top:6px"><input type="checkbox" id="pl-xfer">
              用「先复制→校验→再删源」的方式搬运（跨盘/跨库更安全）</label>
            <div class="hint" style="margin-left:22px">
              <b>只复制b/b>（默认）：把缺的补过去，源那份保留 —— 两边都会有一份。
              <br><b>安全搬运b/b>：补过去、**校验大小一致之后**才删源那份（进回收站）。
              任何一步失败，源都不会消失。往 U 盘 / 网络位置搬、或者想"搬"而不是"留副本"时选它。
            </div>
            <div class="field" style="margin-top:10px"><label>只处理某个子目录（可选）</label>
              <input type="text" id="pl-subdir" placeholder="例如 Rock/周杰伦"></div>
          </div>
          <div class="card">
            <h3>镜像 / 清理（危险）</h3>
            <label class="check"><input type="checkbox" id="pl-mirror"> 镜像模式：右库中左库没有的文件 → 移入回收站</label>
            <div class="hint" style="margin:6px 0 12px">删除永远先进 <span class="mono">.music-sync-trash/日期/</span>，不会直接销毁。</div>
            <label class="check"><input type="checkbox" id="pl-dups"> 同时列出两侧内容完全一致的重复（可清理其中一份）</label>
            <div class="field" style="margin-top:10px"><label>匹配严格度</label>
              <select id="pl-thr">
                <option value="0.55">宽松（多配一些，可能有误配）</option>
                <option value="0.65" selected>标准（推荐）</option>
                <option value="0.75">严格（只配非常像的）</option>
              </select></div>
          </div>
        </div>
        <div id="pl-out" style="margin-top:12px"></div>`,
      footer: `<button class="btn ghost" data-act="close">关闭</button>
               <button class="btn" data-act="dry">${U.icon('eye')}<span>预演（不写入）</span></button>
               <button class="btn primary" data-act="go">生成计划</button>`,
    });
    const out = ctl.body.querySelector('#pl-out');
    const collect = () => ({
      left, right,
      copy_missing: true,
      direction: ctl.body.querySelector('#pl-dir').value,
      overwrite: ctl.body.querySelector('#pl-overwrite').checked,
      generate_conflicts: ctl.body.querySelector('#pl-conflict').checked,
      // ★ 用「复制 → 校验 → 删源」的传输语义，而不是纯复制。
      //   纯复制把缺的补过去、源那份留着（两边各一份），这是"补齐"该有的行为。
      //   但很多人的真实意图是**搬**（尤其是往 U 盘 / 网络位置搬），
      //   纯复制会悄悄把两边都占满，还得多做一步手工删源。
      //   勾上这个就走后端那套校验过的传输：目标校验大小一致之后才删源，
      //   任何一步失败源都不会消失。
      transfer: ctl.body.querySelector('#pl-xfer').checked,
      mirror_delete: ctl.body.querySelector('#pl-mirror').checked,
      include_duplicates: ctl.body.querySelector('#pl-dups').checked,
      subdir: ctl.body.querySelector('#pl-subdir').value.trim(),
      match_threshold: Number(ctl.body.querySelector('#pl-thr').value) || 0.65,
    });

    ctl.footer.querySelector('[data-act=close]').onclick = () => ctl.close();
    ctl.footer.querySelector('[data-act=go]').onclick = async () => {
      out.innerHTML = '<div class="muted"><span class="spinner"></span> 正在分析两侧曲库…</div>';
      try {
        const plan = await API.plan(collect());
        State.plan = plan;
        showPlan(plan);
      } catch (e) { out.innerHTML = `<div style="color:#ff9daa">${U.esc(e.message)}</div>`; }
    };
    ctl.footer.querySelector('[data-act=dry]').onclick = async () => {
      out.innerHTML = '<div class="muted"><span class="spinner"></span> 正在分析并预演…</div>';
      try {
        const plan = await API.plan(collect());
        State.plan = plan;
        if (!plan.steps.length) {
          out.innerHTML = `<div class="muted">${U.icon('check')} 两边已经一致，没有需要执行的操作</div>`;
          return;
        }
        const dry = await API.execute({ steps: plan.steps, execute: false, policy: 'skip' });
        showPreview(plan, dry);
      } catch (e) { out.innerHTML = `<div style="color:#ff9daa">${U.esc(e.message)}</div>`; }
    };
  }

  function planSummaryHtml(plan) {
    const s = plan.summary;
    const opName = { copy: '复制补齐', trash: '移入回收站', delete: '永久删除', rename: '重命名统一' };
    const ops = Object.entries(s.by_op || {}).map(([k, v]) =>
      `<span class="chip ${v.risk_max === 'high' ? 'on' : ''}">${opName[k] || k} ${v.count} 个 · ${U.fmtBytes(v.bytes)}</span>`).join(' ');
    return `
      <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr));margin-bottom:10px">
        ${UI.stat('已配对', plan.stats.matched)}
        ${UI.stat('左库独有', plan.stats.only_left)}
        ${UI.stat('右库独有', plan.stats.only_right)}
        ${UI.stat('待复制体积', U.fmtBytes(s.copy_bytes))}
        ${UI.stat('高风险步骤', s.high_risk)}
      </div>
      <div class="row wrap" style="margin-bottom:10px">${ops || '<span class="muted">没有操作</span>'}</div>
      ${(plan.warnings || []).map((w) => `<div class="badge warn" style="display:block;margin-bottom:6px">⚠ ${U.esc(w)}</div>`).join('')}`;
  }

  function showPlan(plan) {
    const out = document.querySelector('#pl-out') || el.querySelector('#cmp-wrap');
    const opName = { copy: '复制', trash: '回收', delete: '删除', rename: '重命名' };
    const rows = plan.steps.slice(0, 400).map((s) => `
      <tr>
        <td><input type="checkbox" data-step="${s.id}" checked></td>
        <td><span class="tag">${opName[s.op] || s.op}</span></td>
        <td>${U.esc(s.src)}</td>
        <td class="mono ell" title="${U.esc(s.src_path)}">${U.esc(s.src_path)}</td>
        <td>→</td>
        <td>${U.esc(s.dst)}</td>
        <td class="mono ell" title="${U.esc(s.dst_path)}">${U.esc(s.dst_path)}</td>
        <td class="num">${U.fmtBytes(s.size)}</td>
        <td>${s.risk === 'high' ? '<span class="badge err">高危</span>' : (s.risk === 'medium' ? '<span class="badge warn">注意</span>' : '<span class="badge ok">安全</span>')}</td>
        <td class="muted" style="font-size:11px">${U.esc(s.reason)}</td>
      </tr>`).join('');
    out.innerHTML = `
      ${planSummaryHtml(plan)}
      ${plan.steps.length ? `
      <div class="tbl-wrap" style="max-height:340px">
        <table class="data"><thead><tr>
          <th><input type="checkbox" id="pl-all" checked></th><th>操作</th><th>源</th><th>源路径</th><th></th>
          <th>目标</th><th>目标路径</th><th class="right">体积</th><th>风险</th><th>原因</th>
        </tr></thead><tbody>${rows}</tbody></table>
      </div>
      <div class="row" style="margin-top:10px">
        <label class="check"><input type="checkbox" id="pl-verify" checked> 复制后校验大小</label>
        <select id="pl-policy" style="width:auto">
          <option value="skip">目标存在时跳过</option>
          <option value="overwrite">目标存在时覆盖</option>
          <option value="rename">目标存在时改名 (2)</option>
        </select>
        <div class="grow"></div>
        <button class="btn" id="pl-dry2">${U.icon('eye')}<span>预演这批步骤</span></button>
        <button class="btn primary" id="pl-exec">${U.icon('check')}<span>执行选中步骤</span></button>
      </div>` : `<div class="muted">${U.icon('check')} 两边已经一致，没有需要执行的操作</div>`}`;

    const all = out.querySelector('#pl-all');
    if (all) all.onchange = () => {
      const list = out.querySelectorAll('[data-step]');
      if (list) list.forEach((c) => { c.checked = all.checked; });
    };

    // ★ 「覆盖」勾选框 ↔ 「目标同名时」下拉框：**双向联动成同一个开关**。
    //   以前这两个控件互不相干：勾选框只改风险标签、真正生效的只有下拉框，
    //   于是用户勾了"覆盖"、标签还变绿，实际却按默认的"跳过"执行。
    //   现在任一改动都同步到另一个，行为与所见一致。
    const ovr = out.querySelector('#pl-overwrite');
    const pol = out.querySelector('#pl-policy');
    if (ovr && pol) {
      ovr.onchange = () => {
        if (ovr.checked) {
          if (pol.value === 'skip') pol.value = 'overwrite';
        } else if (pol.value === 'overwrite') {
          pol.value = 'skip';
        }
      };
      pol.onchange = () => { ovr.checked = (pol.value === 'overwrite'); };
      ovr.checked = (pol.value === 'overwrite');
    }

    const dry2 = out.querySelector('#pl-dry2');
    if (dry2) dry2.onclick = async () => {
      // 预演也要接住错误：否则失败就是一个未捕获的 rejection，
      // 界面上什么都不会发生，用户以为按钮坏了。
      try {
        const steps = pickSteps(plan, out);
        const r = await API.execute({
          steps, execute: false, policy: out.querySelector('#pl-policy').value,
        });
        showPreview(plan, r);
      } catch (e) {
        UI.toast('预演失败：' + U.esc(e && e.message ? e.message : e), 'err', 8000);
      }
    };
    const exec = out.querySelector('#pl-exec');
    if (exec) exec.onclick = async () => {
      const steps = pickSteps(plan, out);
      if (!steps.length) return UI.toast('没有勾选任何步骤', 'warn');
      const danger = steps.filter((s) => s.op === 'trash' || s.op === 'delete').length;
      const ok = await UI.confirm(
        `即将执行 <b>${steps.length}</b> 个步骤${danger ? `，其中 <b style="color:#ff9daa">${danger}</b> 个是删除（会移入回收站）` : ''}。<br>
         <span class="muted">写入采用「先临时文件再改名」，中断不会留下半截文件。</span>`,
        { okText: '确认执行', danger: !!danger, title: '执行确认' });
      if (!ok) return;
      exec.disabled = true; exec.textContent = '执行中…';
      try {
        // 走 runExclusive：多页面同时执行会被拦下（**不排队** ——
        // 排队会让第二个任务基于已经变化的文件状态操作）
        // ★ 把**选中步骤**的指纹带回去：服务端会逐条比对，
        //   发现"步骤被改动 / 曲库在生成方案之后变了"就 409 拒绝执行。
        //   只发选中的子集，是因为取消勾选是正常操作、不该被当成"方案变了"。
        const hashes = (plan && plan.step_hashes) || {};
        const stepHashes = {};
        for (const s of steps) {
          const h = hashes[String(s.id)];
          if (h) stepHashes[String(s.id)] = h;
        }
        const r = await App.runExclusive('sync', () => API.execute({
          steps, execute: true, confirm: true, step_hashes: stepHashes,
          policy: out.querySelector('#pl-policy').value,
          verify: out.querySelector('#pl-verify').checked,
        }));
        App.logOp(plan.left.label + ' ⇄ ' + plan.right.label, r, steps);
        showResult(r);
        await App.loadSources();
        App.renderSidebar();
      } catch (e) {
        UI.toast('执行失败：' + U.esc(e.message), 'err', 8000);
        exec.disabled = false; exec.innerHTML = U.icon('check') + '<span>执行选中步骤</span>';
      }
    };
  }

  function pickSteps(plan, out) {
    const ids = new Set(Array.from(out.querySelectorAll('[data-step]')).filter((c) => c.checked).map((c) => c.dataset.step));
    return plan.steps.filter((s) => ids.has(s.id));
  }

  function showPreview(plan, dry) {
    const box = document.querySelector('#pl-out');
    const s = dry.summary || {};
    box.innerHTML = `
      ${planSummaryHtml(plan)}
      <div class="card" style="margin-bottom:10px">
        <h3>预演结果（未写入任何文件）</h3>
        <div class="row wrap">
          <span class="badge ok">可执行 ${s.planned || 0}</span>
          <span class="badge warn">已存在 ${s.skipped || 0}</span>
          <span class="badge err">被阻断 ${s.blocked || 0}</span>
          ${s.error ? `<span class="badge err">异常 ${s.error}</span>` : ''}
        </div>
      </div>
      <div class="tbl-wrap" style="max-height:280px">
        <table class="data"><thead><tr><th>步骤</th><th>操作</th><th>目标/对象</th><th>结论</th></tr></thead><tbody>
          ${dry.results.slice(0, 300).map((r) => `<tr>
            <td class="mono">${U.esc(r.step)}</td>
            <td>${U.esc(r.op)}</td>
            <td class="mono ell" title="${U.esc(r.dst || r.src || '')}">${U.esc(r.dst || r.src || '')}</td>
            <td>${r.state === 'planned' ? '<span class="badge ok">将执行</span>' :
                   r.state === 'blocked' ? '<span class="badge err">阻断</span>' :
                   r.state === 'skipped' ? '<span class="badge warn">跳过</span>' : U.esc(r.state)}
                <span class="muted"> ${U.esc(r.detail || '')}</span></td>
          </tr>`).join('')}
        </tbody></table>
      </div>`;
  }

  function showResult(r) {
    const box = document.querySelector('#pl-out');
    const s = r.summary || {};
    box.innerHTML = `
      <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(130px,1fr))">
        ${UI.stat('成功', s.done || 0)}
        ${UI.stat('跳过', s.skipped || 0)}
        ${UI.stat('阻断', s.blocked || 0)}
        ${UI.stat('失败', s.error || 0)}
        ${UI.stat('已复制', U.fmtBytes(s.bytes_moved || 0))}
        ${UI.stat('已清理', U.fmtBytes(s.bytes_trashed || 0))}
      </div>
      <div class="tbl-wrap" style="max-height:320px;margin-top:10px">
        <table class="data"><thead><tr><th>操作</th><th>对象</th><th>结果</th></tr></thead><tbody>
        ${r.results.slice(0, 500).map((x) => `<tr>
          <td>${U.esc(x.op)}</td>
          <td class="mono ell" title="${U.esc(x.dst || x.src || '')}">${U.esc(x.dst || x.src || '')}</td>
          <td>${x.state === 'done' ? '<span class="badge ok">完成</span>' :
                 x.state === 'error' ? '<span class="badge err">失败</span>' :
                 x.state === 'blocked' ? '<span class="badge err">阻断</span>' :
                 `<span class="badge">${U.esc(x.state)}</span>`}
             <span class="muted"> ${U.esc(x.detail || '')}${x.trash ? ' → ' + U.esc(x.trash) : ''}</span></td>
        </tr>`).join('')}
        </tbody></table></div>`;
    UI.toast(`执行完成：成功 ${s.done || 0}，失败 ${s.error || 0}，跳过 ${s.skipped || 0}`, (s.error ? 'warn' : 'ok'), 7000);
  }

  App.registerView('compare', { mount, render, run, planDialog });
})();
