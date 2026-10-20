/* ============================================================
   智能整理：把"放错位置"的单曲挪到正确的位置

   核心原则：<b>只在"可证明位置有误且存在明确目标目录"时才移动文件</b>。
   本功能不创建新的目录层级，也不改动已整理好的目录结构。

   判定规则（界面均会显示依据）：
     · 收纳区（单曲01 / 英语歌精选集 / 日语歌精选集 …）内的曲目不受影响，一律不移动
     · 目录名与曲目的作者或专辑一致（周杰伦/范特西）→ 位置正确
     · 目录内多数曲目属于同一作者或专辑，而本曲目不属于 → 判定为位置有误
   ============================================================ */
const Organizer = (() => {

  /* ============================================================
     按专辑整理（智能整理的主流程）

     三步向导：
       1. 选范围：勾选的文件夹<b>直接忽略</b>，不参与整理
       2. 查看方案：按专辑分组，逐组给出目标目录与依据，可单独调整，
                   也可为某个专辑或作者建立"自定义映射"，固定归入自建目录
       3. 执行   —— 只搬勾选的专辑

     目标目录的判定顺序：
       自定义映射 → 库里同名专辑目录 → 同名作者目录/作者下建专辑子目录
       → 库里最相似的目录 → 同专辑够 ≥N 首就新建 → 否则原地不动
     ============================================================ */
  const KIND_INFO = {
    custom: ['自定义映射', 'ok', '自定义映射'],
    album_exact: ['同名专辑目录', 'ok', '库内已存在该专辑的同名目录'],
    artist_album: ['作者/专辑', 'ok', '在作者目录下为这张专辑建子目录'],
    artist_exact: ['同名作者目录', 'ok', '库内已存在该作者的同名目录'],
    similar: ['相似目录', '', '库内存在名称高度相似的既有目录'],
    new: ['新建目录', 'warn', '该专辑曲目数达到门槛，建议新建独立目录'],
    sink: ['收进收纳区', '', '不够门槛，先收进指定的收纳区'],
  };

  /* ============================================================
     智能整理：把库按"分组依据"归类

     两个核心选择直接决定结果形态：① 按什么分组 ② 目录结构。
     其余（忽略范围、收纳区、门槛）都有能用的默认值，收在「高级设置」里。

     参数会**按数据源记住**：忽略范围和收纳区这种是长期偏好，
     每次重开都要重新勾一遍太折磨人。
     ============================================================ */
  const PREF_KEY = (sid) => `msh.orgPref.${sid}`;

  function loadPref(sid) {
    try {
      const raw = localStorage.getItem(PREF_KEY(sid));
      if (!raw) return null;
      const d = JSON.parse(raw);
      if (!d || d.v !== 1) return null;       // 版本变了就当没有，避免读到旧结构
      return d;
    } catch (e) { return null; }
  }

  function savePref(sid, st) {
    try {
      localStorage.setItem(PREF_KEY(sid), JSON.stringify({ v: 1, ...st }));
    } catch (e) { /* 存不下就算了，不影响使用 */ }
  }

  async function albumWizard(opt = {}) {
    const sid = opt.source || State.activeSource;
    if (!sid) return UI.toast('先选一个数据源', 'warn');

    let plan = null;
    let preset = null;
    let ignored = new Set();           // 勾上=忽略
    let sinks = new Set();
    let custom = {};                   // 专辑/作者 → 目录
    let smallPolicy = 'keep';
    let only = null;                   // null=全选
    // 两个核心选择，直接决定结果形态
    let groupBy = 'artist_album';
    let structure = 'nested';
    // 上次的选择（按数据源分别记）
    const pref = loadPref(sid);
    if (pref) {
      if (['artist', 'album', 'artist_album'].includes(pref.groupBy)) groupBy = pref.groupBy;
      if (['flat', 'nested'].includes(pref.structure)) structure = pref.structure;
      if (['keep', 'sink'].includes(pref.smallPolicy)) smallPolicy = pref.smallPolicy;
      if (pref.ignored) ignored = new Set(pref.ignored);
      if (pref.custom && typeof pref.custom === 'object') custom = pref.custom;
    }

    const ctl = UI.modal({
      title: '智能整理',
      size: 'lg',
      body: `
        <div class="field">
          <label>① 按什么分组</label>
          <div class="seg" id="aw-by">
            <button class="chip" data-by="artist">仅作者</button>
            <button class="chip on" data-by="artist_album">作者 + 专辑</button>
            <button class="chip" data-by="album">仅专辑</button>
          </div>
          <div class="hint" id="aw-by-hint"></div>
        </div>

        <div class="field" id="aw-struct-wrap">
          <label>② 目录结构</label>
          <div class="seg" id="aw-struct">
            <button class="chip on" data-st="nested">两层：作者 / 专辑</button>
            <button class="chip" data-st="flat">单层：仅作者</button>
          </div>
          <div class="hint" id="aw-struct-hint"></div>
        </div>

        <div class="field">
          <label>③ 达到几首才单独建目录</label>
          <div class="row" style="gap:10px;align-items:center">
            <input type="number" id="aw-min" value="5" min="1" max="100" style="width:78px">
            <span class="muted" style="font-size:12px" id="aw-min-hint"></span>
          </div>
        </div>

        <details class="og-sec" id="aw-adv">
          <summary><span class="oga" aria-hidden="true">▸</span>高级设置(可选)</summary>
          <div class="field" style="margin-top:8px">
            <label>映射规则</label>
            <div class="row" style="margin:6px 0;align-items:center">
              <button class="btn sm" id="aw-rules">${U.icon('settings')}
                <span>查看 / 编辑规则</span></button>
              <span class="muted" style="font-size:12px" id="aw-rules-info"></span>
            </div>
            <div class="hint">
              为某个专辑或作者固定一个目录，优先级最高。规则会被记住。</div>
          </div>
          <div class="field">
            <label>忽略范围：勾选的目录不参与整理</label>
            <div class="row" style="margin:6px 0">
              <button class="btn sm ghost" id="aw-none">全部整理</button>
              <button class="btn sm ghost" id="aw-only-root">只整理根目录</button>
              <div class="grow"></div>
              <span class="muted" style="font-size:12px" id="aw-scopeinfo"></span>
            </div>
            <div class="wiz-scope" id="aw-scope"></div>
          </div>
          <div class="field" id="aw-sink-field">
            <label>收纳区：未达门槛的组归入何处</label>
            <div class="row" style="gap:16px;align-items:center;margin-bottom:6px">
              <label class="inline"><input type="radio" name="aw-small" value="keep" checked>
                保留原位（推荐）</label>
              <label class="inline"><input type="radio" name="aw-small" value="sink">
                归入收纳区</label>
            </div>
            <!-- 只灰这一块：单选按钮本身必须保持可点，否则选了"保留原位"
                 就再也切不回"归入收纳区"了 -->
            <div id="aw-sink-body">
              <div class="wiz-scope" id="aw-sinks"></div>
              <div class="hint" id="aw-sink-hint"></div>
            </div>
          </div>
        </details>

        <div class="row" style="margin:10px 0 6px">
          <button class="btn sm" id="aw-recalc">${U.icon('refresh')}<span>重新计算</span></button>
          <div class="grow"></div>
          <span class="muted" id="aw-progress"></span>
        </div>
        <div id="aw-plan"></div>`,
      footer: `<span class="muted grow" id="aw-sum"></span>
               <button class="btn ghost" data-act="no">关闭</button>
               <button class="btn" data-act="dry" disabled>${U.icon('eye')}<span>预演</span></button>
               <button class="btn primary" data-act="yes" data-exclusive disabled>
                 ${U.icon('check')}<span>执行整理</span></button>`,
    });

    const scopeBox = ctl.body.querySelector('#aw-scope');
    const sinkBox = ctl.body.querySelector('#aw-sinks');
    const planBox = ctl.body.querySelector('#aw-plan');
    const sumBox = ctl.footer.querySelector('#aw-sum');
    const yesBtn = ctl.footer.querySelector('[data-act=yes]');
    const dryBtn = ctl.footer.querySelector('[data-act=dry]');
    const minBox = ctl.body.querySelector('#aw-min');
    const byHint = ctl.body.querySelector('#aw-by-hint');
    const structWrap = ctl.body.querySelector('#aw-struct-wrap');
    const structHint = ctl.body.querySelector('#aw-struct-hint');
    const minHint = ctl.body.querySelector('#aw-min-hint');
    const progBox = ctl.body.querySelector('#aw-progress');

    // ★ 高级设置的展开箭头要自己补:原生三角被 CSS 藏掉了(`.og-sec>summary`
    //   的 list-style/::marker 全清空),而"给 details 塞文本箭头"那段只处理了
    //   方案区(`planBox`),向导自己的这一块就成了**一行纯文字** ——
    //   用户根本看不出它能展开。
    const advBox = ctl.body.querySelector('#aw-adv');
    if (advBox) {
      const advArrow = advBox.querySelector('.oga');
      advBox.addEventListener('toggle', () => {
        if (advArrow) advArrow.textContent = advBox.open ? '▾' : '▸';
      });
    }

    /* 生成方案的**内嵌**进度:进度条 + 文案 + 取消,就写在「重新计算」那一行。
       为什么不用 `UI.watchJobDialog`:那是个模态框,生成一次就要关一次,
       调参数反复重算时非常烦人,而且它会盖住方案区,看不到上一次的结果。
       返回方案本身;取消返回 `{cancelled:true}`;失败抛出。 */
    function progressReset() {
      progBox.innerHTML = '';
    }

    async function watchInline(jobId) {
      progBox.innerHTML =
        '<span class="pbar" style="display:inline-block;width:150px;vertical-align:middle">'
        + '<i id="aw-pbar"></i></span>'
        + '<span class="muted" id="aw-pmsg" style="font-size:11.5px">正在开始...</span>'
        + '<button class="btn sm ghost" id="aw-pstop">取消</button>';
      const bar = progBox.querySelector('#aw-pbar');
      const msg = progBox.querySelector('#aw-pmsg');
      const stop = progBox.querySelector('#aw-pstop');
      stop.onclick = async () => {
        stop.disabled = true;
        msg.textContent = '正在取消...(已经算完的部分不会回退)';
        try { await API.cancelJob(jobId); } catch (e) { /* 取消失败就算了 */ }
      };
      const res = await UI.watchJob(jobId, {
        onProgress: (j) => {
          if (j.percent != null) {
            bar.style.width = Math.max(0, Math.min(100, Number(j.percent))) + '%';
          }
          const n = j.total
            ? `（${j.done}/${j.total}${j.eta ? ` 约剩 ${j.eta}s` : ''}）` : '';
          msg.textContent = (j.message || '处理中...') + n;
        },
      });
      const j = (res && res._job) || {};
      if (j.state === 'cancelled' || j.cancel) return { cancelled: true };
      if (j.state === 'error') throw new Error(j.error || '生成方案失败');
      if (res && typeof res === 'object' && '_job' in res) delete res._job;
      return res;
    }

    /** 把当前选择写回记忆（忽略范围与收纳区是长期偏好，最值得记）。 */
    const remember = () => savePref(sid, {
      groupBy, structure, smallPolicy,
      minGroup: Number(minBox.value) || 5,
      ignored: Array.from(ignored),
      sinks: Array.from(sinks),
      custom,
    });

    const body = () => ({
      source: sid,
      group_by: groupBy,
      structure: groupBy === 'artist_album' ? structure : 'flat',
      scopes: Array.from(ignored),        // 勾上的 = 忽略
      sinks: Array.from(sinks),
      custom,
      min_group: Number(minBox.value) || 5,
      small_policy: smallPolicy,
    });

    // ---------------- ①②③ 的说明与交互 ----------------
    function paintHints() {
      const BY = {
        artist: '按作者分组：同一作者的全部曲目归入一个目录，该作者的各专辑放在一起。',
        album: '按专辑分组：同一专辑的曲目归入一个目录，不设作者层级。',
        artist_album: '按作者与专辑分组：同一作者同一专辑归为一组，'
          + '再选择一层还是两层结构。',
      };
      byHint.textContent = BY[groupBy] || '';
      // 只有"作者+专辑"才谈得上层数；另外两种天然单层
      const single = groupBy !== 'artist_album';
      structWrap.style.opacity = single ? '.45' : '1';
      ctl.body.querySelectorAll('#aw-struct .chip').forEach((b) => {
        b.disabled = single;
      });
      if (single) {
        structHint.textContent = groupBy === 'artist'
          ? '按作者分组时，作者目录即目标，不再分级。'
          : '按专辑分组时，专辑目录即目标，不设作者层级。';
        if (structure !== 'flat') {
          structure = 'flat';
          ctl.body.querySelectorAll('#aw-struct .chip')
            .forEach((x) => x.classList.toggle('on', x.dataset.st === 'flat'));
        }
      } else {
        structHint.textContent = structure === 'nested'
          ? '两层：目标形如「作者 / 专辑」。曲目数未达门槛的专辑保留在作者目录内，不再分层。'
          : '单层：目标形如「作者」，该作者的各专辑混放于同一目录。';
      }
      const n = Number(minBox.value) || 5;
      minHint.textContent = `一组曲目数达到 ${n} 首时才为它新建目录；`
        + '未达门槛且库内无对应目录的组保留原位。';
    }

    ctl.body.querySelectorAll('#aw-by .chip').forEach((b) => {
      b.onclick = () => {
        groupBy = b.dataset.by;
        ctl.body.querySelectorAll('#aw-by .chip')
          .forEach((x) => x.classList.toggle('on', x === b));
        paintHints();
        remember();
        compute();
      };
    });
    ctl.body.querySelectorAll('#aw-struct .chip').forEach((b) => {
      b.onclick = () => {
        if (b.disabled) return;
        structure = b.dataset.st;
        ctl.body.querySelectorAll('#aw-struct .chip')
          .forEach((x) => x.classList.toggle('on', x === b));
        paintHints();
        remember();
        compute();
      };
    });
    minBox.onchange = () => { paintHints(); remember(); compute(); };

    /** 用（可能来自记忆的）当前状态把控件刷成一致的样子。 */
    function syncControls() {
      ctl.body.querySelectorAll('#aw-by .chip')
        .forEach((x) => x.classList.toggle('on', x.dataset.by === groupBy));
      ctl.body.querySelectorAll('#aw-struct .chip')
        .forEach((x) => x.classList.toggle('on', x.dataset.st === structure));
      ctl.body.querySelectorAll('input[name=aw-small]')
        .forEach((r) => { r.checked = r.value === smallPolicy; });
      if (pref && pref.minGroup) minBox.value = pref.minGroup;
      paintHints();
      paintSinkHint();
      if (pref) {
        const bits = [];
        if (pref.ignored && pref.ignored.length) bits.push(`忽略 ${pref.ignored.length} 个目录`);
        if (pref.sinks && pref.sinks.length) bits.push(`收纳区 ${pref.sinks.length} 个`);
        if (bits.length) {
          UI.toast(`已恢复上次的设置：${bits.join(' · ')}`
            + '（可在「高级设置」里修改）', 'ok', 6000);
        }
      }
    }

    // ---------------- ① 范围 ----------------
    async function loadScope() {
      try {
        preset = await API.organizePreview(sid, 300);
      } catch (e) {
        scopeBox.innerHTML = `<span style="color:var(--danger)">${U.esc(e.message)}</span>`;
        return;
      }
      const list = (preset.top || []).filter((f) => f.folder);
      scopeBox.innerHTML = list.map((f) => `
        <label data-folder="${U.esc(f.folder)}">
          <input type="checkbox" data-scope="${U.esc(f.folder)}"
                 ${ignored.has(f.folder) ? 'checked' : ''}>
          <span class="ell" style="flex:1" title="${U.esc(f.folder)}">${U.esc(f.folder)}</span>
          <span class="muted">${f.count} 首</span>
          ${f.sink ? '<span class="tag">收纳区</span>' : ''}
        </label>`).join('') || '<span class="muted">没有目录</span>';
      scopeBox.querySelectorAll('[data-scope]').forEach((cb) => {
        cb.onchange = () => {
          if (cb.checked) ignored.add(cb.dataset.scope);
          else ignored.delete(cb.dataset.scope);
          paintScopeInfo();
          remember();
          compute();
        };
      });
      // 收纳区候选：和「忽略范围」用同一套复选列表（勾选更直观，也能搜索）
      const auto = preset.sinks || [];
      auto.forEach((f) => sinks.add(f));
      const opts = preset.sink_options || [];
      // 兜底：老接口只给了名字数组时也能显示
      const sinkList = opts.length ? opts
        : Array.from(new Set([...(preset.sink_candidates || []), ...sinks]))
          .map((f) => ({ folder: f, count: null, sink: auto.includes(f) }));
      sinkBox.innerHTML = sinkList.map((o) => `
        <label data-sinkrow="${U.esc(o.folder)}">
          <input type="checkbox" data-sink="${U.esc(o.folder)}"
                 ${sinks.has(o.folder) ? 'checked' : ''}>
          <span class="ell" style="flex:1" title="${U.esc(o.folder)}">${U.esc(o.folder)}</span>
          <span class="muted">${o.count == null ? '' : o.count + ' 首'}</span>
          ${o.sink ? '<span class="tag">已识别</span>' : ''}
        </label>`).join('') || '<span class="muted">未识别到收纳区</span>';
      sinkBox.querySelectorAll('[data-sink]').forEach((cb) => {
        cb.onchange = () => {
          if (cb.checked) sinks.add(cb.dataset.sink);
          else sinks.delete(cb.dataset.sink);
          paintSinkHint();
          remember();
          compute();
        };
      });
      paintScopeInfo();
      paintSinkHint();
    }

    /** 收纳区当前生效的是哪一个（有多个时按层级最浅/曲目最多挑一个）。 */
    function effectiveSink() {
      const arr = Array.from(sinks);
      if (!arr.length) return '';
      const meta = new Map((preset && preset.sink_options || [])
        .map((o) => [o.folder, o.count || 0]));
      return arr.sort((a, b) => (a.split('/').length - b.split('/').length)
        || ((meta.get(b) || 0) - (meta.get(a) || 0)))[0];
    }

    function paintSinkHint() {
      const hint = ctl.body.querySelector('#aw-sink-hint');
      const body = ctl.body.querySelector('#aw-sink-body');
      if (!hint || !body) return;
      const off = smallPolicy !== 'sink';
      // 选了"保留原位"时，**下面那块**收纳区列表用不上 —— 只灰它，
      // 单选按钮本身必须一直可点（踩过：把整个 field 灰掉，
      // 结果"保留原位"自己也被灰、点不动，就再也切不回"归入收纳区"了）。
      body.classList.toggle('dim', off);
      sinkBox.querySelectorAll('input[data-sink]').forEach((cb) => {
        cb.disabled = off;
      });
      hint.textContent = off
        ? '当前策略为「保留原位」，收纳区不参与整理。选择「归入收纳区」后此项才生效。'
        : (sinks.size
          ? `共勾选 ${sinks.size} 个；实际归入「${effectiveSink()}」`
          : '尚未勾选收纳区。未达门槛的组将无处可归，会保留在原位。');
    }

    function paintScopeInfo() {
      ctl.body.querySelector('#aw-scopeinfo').textContent = ignored.size
        ? `将忽略 ${ignored.size} 个目录（含子目录）`
        : '当前：整理全部目录';
    }

    ctl.body.querySelector('#aw-none').onclick = () => {
      ignored.clear();
      scopeBox.querySelectorAll('[data-scope]').forEach((cb) => { cb.checked = false; });
      paintScopeInfo();
      remember();
      compute();
    };
    ctl.body.querySelector('#aw-only-root').onclick = () => {
      // "只忽略根目录"=把最外层目录都勾上
      const tops = new Set();
      scopeBox.querySelectorAll('[data-scope]').forEach((cb) => {
        const f = cb.dataset.scope;
        if (f && !f.includes('/')) tops.add(f);
      });
      ignored = tops;
      scopeBox.querySelectorAll('[data-scope]').forEach((cb) => {
        cb.checked = ignored.has(cb.dataset.scope);
      });
      paintScopeInfo();
      remember();
      compute();
    };

    ctl.body.querySelectorAll('input[name=aw-small]').forEach((r) => {
      r.onchange = () => { smallPolicy = r.value; paintSinkHint(); remember(); compute(); };
    });
    ctl.body.querySelector('#aw-recalc').onclick = compute;
    ctl.body.querySelector('#aw-rules').onclick = rulesDialog;

    /** 显示已保存的规则条数，让用户知道"记忆里有没有东西"。 */
    async function paintRulesInfo() {
      const box = ctl.body.querySelector('#aw-rules-info');
      if (!box) return;
      try {
        const r = await API.mappings(sid);
        const n = Object.keys(r.rules || {}).filter((k) => k !== '__sink__').length;
        box.textContent = n ? `已保存 ${n} 条规则` : '还没有规则';
      } catch (e) {
        box.textContent = '';
      }
    }

    // ---------------- ② 看方案 ----------------
    async function compute() {
      planBox.innerHTML = '<div class="pad muted">正在按专辑分析…</div>';
      yesBtn.disabled = true; dryBtn.disabled = true;
      try {
        const started = await API.organizeAlbumPlan(body());
        // ★ 后端默认**后台跑**(真实库按专辑分组实测要 6 秒,同步等会让界面
        //   只有一个转圈、不知道进度也取消不了)。所以这里要接住两种返回:
        //   后台 → {job, async:true} 走**内嵌**进度(见 `watchInline`);
        //   同步 → 直接就是方案(老后端 / wait:true 的调用)。
        plan = started && started.async ? await watchInline(started.job) : started;
        progressReset();
        if (!plan || plan.cancelled) {
          planBox.innerHTML = '<div class="pad muted">已取消</div>';
          yesBtn.disabled = false; dryBtn.disabled = false;
          return;
        }
        only = null;
        paintPlan();
      } catch (e) {
        progressReset();
        planBox.innerHTML = `<div class="pad" style="color:var(--danger)">${U.esc(e.message)}</div>`;
        yesBtn.disabled = false; dryBtn.disabled = false;
      }
    }
    /** 一组对应的**全部**分组键（同一目标目录可能由多个键合并而来）。 */
    const keysOf = (a) => (a.keys && a.keys.length ? a.keys : [a.key]);

    /** 以"分组键"为单位列出所有可选项（合并后的组要展开成原来的键）。 */
    const allKeysOf = (acts) => {
      const out = [];
      acts.forEach((a) => keysOf(a).forEach((k) => { if (!out.includes(k)) out.push(k); }));
      return out;
    };

    const chosenKeys = () => {
      if (!plan) return [];
      const all = (plan.actions || []).filter((a) => a.count > 0);
      if (!only) return allKeysOf(all);
      const out = [];
      all.forEach((a) => keysOf(a).forEach((k) => {
        if (only.has(k) && !out.includes(k)) out.push(k);
      }));
      return out;
    };

    // ---------------------------------------------------- 组内单曲勾选
    /**
     * 用户在某组里**逐首**取消勾选的曲目（path -> true 表示不要它）。
     *
     * 为什么需要：按专辑/作者分组后，"这一组"往往比用户想动的范围大 ——
     * 比如一张合集里只想归位其中 3 首，剩下的想留在原地。
     * 原来只能整组要或整组不要，用户要么被迫接受全部，要么干脆不做。
     *
     * 用 `path` 而不是下标做键：重新规划后 items 顺序可能变，
     * 下标会错位到别的曲目上（那会让用户"取消了一首结果动了另一首"）。
     */
    const excluded = new Set();

    /** 这一组是否算被勾选：合并组里**任一**键被勾上就算。 */
    /** 这一组是否算被勾选：合并组里**任一**键被勾上就算。
     *  注意这里自己算一遍 key 集合，不要依赖 paintPlan 里的局部变量。 */
    const isChosen = (a) => {
      if (!only) return true;                      // 没动过 = 全选
      const ks = keysOf(a);
      return ks.some((k) => only.has(k));
    };

    /** 目标目录汇总：先让用户看清"会整理成什么结构"，再决定要不要执行。 */
    function folderSummary(acts) {
      const byDir = new Map();
      acts.forEach((a) => {
        byDir.set(a.dst, (byDir.get(a.dst) || 0) + a.count);
      });
      const dirs = Array.from(byDir.entries()).sort((x, y) => y[1] - x[1]);
      if (!dirs.length) return '';
      return `
        <div class="og-folders">
          <div class="og-folders-hd">
            <b>目标目录 ${dirs.length} 个</b>
            <span class="muted">整理后这些目录将获得曲目（括号内为数量）</span>
          </div>
          <div class="og-folders-list">
            ${dirs.slice(0, 24).map(([d, n]) =>
              `<span class="og-folder-tag">${U.esc(d)}<b>${n}</b></span>`).join('')}
            ${dirs.length > 24 ? `<span class="muted">… 共 ${dirs.length} 个</span>` : ''}
          </div>
        </div>`;
    }

    /**
     * 一组的曲目明细，**直接铺在分组行下方**。
     *
     * 原来是藏在「曲目」按钮的二级弹窗里：看不到每组到底有哪些歌，
     * 得一个个点开；而且弹窗一关就没了，比较两组要反复开关。
     * 现在按需展开，就在原地看，还能一眼看出"哪些已经在目标目录了"。
     */
    function trackListHtml(a) {
      const items = a.items || [];
      if (!items.length) return '<div class="pad muted">这一组没有曲目</div>';
      const cap = 300;
      // items 里只给了 path，所在目录得自己截出来
      const folderOf = (t) => String(t.path || '').replace(/\/[^/]*$/, '');
      const isHere = (t) => folderOf(t) === String(a.dst || '');
      const rows = items.slice(0, cap).map((t) => {
        const here = isHere(t);
        const folder = folderOf(t);
        const p = String(t.path || '');
        const off = excluded.has(p);
        return `<tr${here ? ' class="og-here"' : ''}${off ? ' style="opacity:.45"' : ''}>
          <td><input type="checkbox" data-ogtrk="${U.esc(p)}"
                ${off ? '' : 'checked'}
                title="${here ? '这首歌已在目标目录，勾不勾都不动它'
                              : (off ? '已取消：这一首不会被移动' : '这一首会被移动')}"></td>
          <td>${UI.coverHtml(t, 'cover xs')}</td>
          <td class="ell" style="max-width:290px" title="${U.esc(t.path)}">
            ${U.esc(t.name)}</td>
          <td class="ell" style="max-width:150px"
              title="${U.esc(t.artist || '')}">${U.esc(t.artist || '-')}</td>
          <td class="ell" style="max-width:170px" title="${U.esc(t.album || '')}">
            ${U.esc(t.album || '-')}</td>
          <td class="ell muted" style="max-width:230px;font-size:11px"
              title="${U.esc(folder)}">${U.esc(folder)}</td>
          <td class="num">${U.fmtBytes(t.size || 0)}</td>
          <td class="nowrap">${here
            ? '<span class="tag ll">已在目标目录</span>'
            : (off ? '<span class="tag">已取消</span>'
                   : '<span class="tag">将移动</span>')}</td>
        </tr>`;
      }).join('');
      const movable = items.filter((t) => !isHere(t));
      const moved = movable.filter(
        (t) => !excluded.has(String(t.path || ''))).length;
      const off = movable.length - moved;
      const already = items.length - movable.length;
      return `
        <div class="og-items-hd">
          共 <b>${items.length}</b> 首${already
            ? ` · <span style="color:var(--accent)">${moved} 首将移动</span>
               · ${already} 首已在目标目录`
            : ` · <span style="color:var(--accent)">${moved} 首将移动</span>`}${
            off ? ` · <span style="color:var(--warn)">${off} 首已取消</span>` : ''}
          <span class="grow"></span>
          <button class="btn sm ghost" data-ogtrkall="${U.esc(a.key)}"
            title="把这一组里所有曲目重新勾上">组内全选</button>
          <button class="btn sm ghost" data-ogtrknone="${U.esc(a.key)}"
            title="把这一组里所有曲目取消（等于这一组不动）">组内全不选</button>
          <span class="muted">勾选决定哪几首会被移动</span>
        </div>
        <div class="tbl-wrap" style="max-height:26vh">
          <table class="data og-items-tbl"><thead><tr>
            <th style="width:36px"></th><th></th><th>文件名</th><th>作者</th>
            <th>专辑</th><th>当前目录</th><th class="num">体积</th><th>处理</th>
          </tr></thead><tbody>${rows}</tbody></table>
        </div>
        ${items.length > cap ? `<div class="muted" style="font-size:11.5px;margin-top:4px">
          仅显示前 ${cap} 首，该组共 ${items.length} 首</div>` : ''}`;
    }

    function paintPlan() {
      if (!plan) return;
      const s = plan.summary || {};
      const acts = (plan.actions || []).filter((a) => a.count > 0);
      const picked = acts.filter((a) => isChosen(a));
      const bytes = picked.reduce((x, a) => x + (a.bytes || 0), 0);

      planBox.innerHTML = `
        <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(118px,1fr));margin-bottom:10px">
          ${UI.stat('曲目总数', s.total)}
          ${UI.stat('分组数', s.albums)}
          ${UI.stat('需移动', s.move, `涉及 ${acts.length} 组`)}
          ${UI.stat('保留原位', s.keep,
            s.small_kept ? `含 ${s.small_kept} 组未达门槛` : '')}
          ${UI.stat('新建目录', s.new_dirs)}
          ${s.manual ? UI.stat('无元数据·手工', s.manual,
            `共 ${U.fmtBytes(s.manual_bytes || 0)}`) : ''}
        </div>
        ${s.manual ? `<div class="exp-warn" style="margin-bottom:10px">
          <b>${U.icon('warn')} ${s.manual} 首没有元数据，不参与智能整理</b>
          <div>这些曲目<b>没有 artist / album 标签</b>，无法判断该归到哪个作者或专辑，
            所以智能整理<b>不会移动它们</b> —— 强行归类只会把它们丢进错误的目录。
            它们保持原位，需要你手工处理。</div>
          <div class="exp-tip">处理办法：在「文件浏览」里选中它们 →「编辑标签」
            补上作者/专辑，之后再跑一次智能整理就会自动归位。</div>
        </div>` : ''}
        ${folderSummary(acts)}
        ${s.ignored && s.ignored.length ? `<div class="muted" style="font-size:12px;margin-bottom:6px">
          已忽略 ${s.ignored.length} 个目录：${s.ignored.slice(0, 6).map(
            (x) => `<span class="tag">${U.esc(x)}</span>`).join(' ')}
          ${s.ignored.length > 6 ? `… 等 ${s.ignored.length} 个` : ''}</div>` : ''}
        ${acts.length ? `
          <div class="row" style="margin-bottom:6px">
            <button class="btn sm ghost" id="aw-all">全选</button>
            <button class="btn sm ghost" id="aw-none-all">全不选</button>
            <span class="muted" style="font-size:12px">
              按专辑分组，可只勾选需要处理的行；每行均可单独调整目标目录或建立映射</span>
          </div>
          <div class="tbl-wrap" style="max-height:40vh">
            <table class="data og-plan"><thead><tr>
              <th style="width:34px"></th><th style="width:26px"></th><th>分组</th>
              <th>目标目录</th><th>判定依据</th><th class="num">曲目</th>
              <th></th></tr></thead><tbody>
            ${acts.slice(0, 400).map((a) => {
              const [lbl, tone, tip] = KIND_INFO[a.kind] || [a.kind, '', ''];
              const c = isChosen(a) ? '' : 'style="opacity:.5"';
              // 组内被单独取消的曲目数 → 行上给个明确徽标。
              // 否则用户取消了 3 首之后，这一行看上去还是"整组都要动"。
              const offN = (a.items || []).filter(
                (t) => excluded.has(String(t.path || ''))).length;
              return `<tr ${c} data-key="${U.esc(a.key)}">
                <td><input type="checkbox" data-akey="${U.esc(a.key)}"
                     data-keys="${U.esc(keysOf(a).join('\x1f'))}"
                     ${isChosen(a) ? 'checked' : ''}></td>
                <td><button class="btn icon sm ghost og-exp" data-aexp="${U.esc(a.key)}"
                    title="展开该组的曲目明细（就在本行下方）"
                    ><span class="oga" aria-hidden="true">▸</span></button></td>
                <td style="max-width:230px">
                  <div class="ell"><b>${U.esc(a.album || a.artist || a.key)}</b></div>
                  <div class="muted ell" style="font-size:11.5px">
                    ${U.esc(a.artist || '')}${a.already
                      ? ` · 另有 ${a.already} 首已就位` : ''}
                    ${a.from_folders.length > 1
                      ? ` · 来自 ${a.from_folders.length} 个目录` : ''}
                    ${a.merged_from ? ` · 由 ${a.merged_from} 个分组归并` : ''}</div>
                </td>
                <td style="max-width:230px">
                  <div class="ell"><b>${U.esc(a.dst || '（根目录）')}</b></div>
                  <div><span class="badge ${tone}" title="${U.esc(tip)}">${U.esc(lbl)}</span></div>
                </td>
                <td style="max-width:200px">
                  <div class="muted" style="font-size:11px">${U.esc(a.why)}</div></td>
                <td class="num">${a.count}${
                  offN ? `<div class="muted" style="font-size:11px;color:var(--warn)"
                    title="这一组里有 ${offN} 首被单独取消了勾选">−${offN}</div>` : ''}</td>
                <td class="num" style="white-space:nowrap">
                  <button class="btn sm ghost" data-adst="${U.esc(a.key)}"
                    title="为这一组指定其他目标目录">改目录</button>
                  <button class="btn sm ghost" data-amap="${U.esc(a.key)}"
                    title="为该专辑或作者建立映射，此后自动归入指定目录">映射</button>
                </td></tr>
              <tr class="og-items" data-items-for="${U.esc(a.key)}" hidden>
                <td></td><td colspan="6">${trackListHtml(a)}</td>
              </tr>`;
            }).join('')}
            </tbody></table>
          </div>
          ${acts.length > 400 ? `<div class="muted" style="font-size:12px;margin-top:4px">
            只显示前 400 组，实际共 ${acts.length} 组</div>` : ''}`
          : `<div class="empty" style="padding:26px">
              <div class="big">${U.icon('check', 'ico')}</div>
              <div>按当前规则不需要移动任何文件</div>
              <div class="muted" style="font-size:12px;margin-top:6px">
                可适当调低"达到几首才单独建目录"，或检查忽略范围设置</div>
             </div>`}
        <details style="margin-top:8px">
          <summary class="muted" style="cursor:pointer;font-size:12px">判定顺序（目标目录的确定方式）</summary>
          <ol class="muted" style="font-size:12px;line-height:1.8;margin:8px 0 0;padding-left:20px">
            <li>自定义映射（优先级最高）</li>
            <li>库内已有<b>同名专辑</b>目录 → 直接归入</li>
            <li>库内已有<b>同名作者</b>目录 → 两层结构且该组达门槛时，在其下建立专辑子目录；
                否则保留于作者目录</li>
            <li>库内名称<b>最相似</b>的既有目录（相似度 ≥88%）→ 建议并入</li>
            <li>该组曲目数达到 <b>${s.min_group}</b> 首 → 新建目录</li>
            <li>以上均不满足 → ${s.small_policy === 'sink'
                 ? '归入收纳区' : '<b>保留原位</b>（不改动既有合集目录）'}</li>
            <li>已加入"忽略范围"的目录及其全部子目录均不参与</li>
          </ol>
        </details>`;

      sumBox.innerHTML = `已选 <b>${picked.length}</b> / ${acts.length} 组 · `
        + `${picked.reduce((x, a) => x + a.count, 0)} 个文件 · ${U.fmtBytes(bytes)}`;
      yesBtn.disabled = dryBtn.disabled = !picked.length;

      planBox.querySelectorAll('[data-akey]').forEach((cb) => {
        cb.onchange = () => {
          const ks = (cb.dataset.keys || cb.dataset.akey).split('\x1f');
          if (!only) only = new Set(allKeysOf(acts));
          // 合并组可能对应多个键，要一起勾/一起取消 ——
          // 只动当前那个键的话，"取消勾选"之后它还会被同组别的键带回来。
          ks.forEach((k) => {
            if (cb.checked) only.add(k); else only.delete(k);
          });
          paintPlan();
        };
      });
      const allBtn = planBox.querySelector('#aw-all');
      if (allBtn) allBtn.onclick = () => { only = null; paintPlan(); };
      const noneBtn = planBox.querySelector('#aw-none-all');
      if (noneBtn) noneBtn.onclick = () => { only = new Set(); paintPlan(); };

      // ---- 组内单曲勾选 ----
      // 每首一个复选框：取消某一首 = 这一首不参与移动，同组其它的照常。
      planBox.querySelectorAll('[data-ogtrk]').forEach((cb) => {
        cb.onchange = () => {
          const p = cb.dataset.ogtrk;
          if (cb.checked) excluded.delete(p); else excluded.add(p);
          paintPlan();
        };
      });
      planBox.querySelectorAll('[data-ogtrkall]').forEach((b) => {
        b.onclick = () => {
          const a = acts.find((x) => x.key === b.dataset.ogtrkall);
          (a && a.items || []).forEach(
            (t) => excluded.delete(String(t.path || '')));
          paintPlan();
        };
      });
      planBox.querySelectorAll('[data-ogtrknone]').forEach((b) => {
        b.onclick = () => {
          const a = acts.find((x) => x.key === b.dataset.ogtrknone);
          (a && a.items || []).forEach(
            (t) => excluded.add(String(t.path || '')));
          paintPlan();
        };
      });
      planBox.querySelectorAll('[data-adst]').forEach((b) => {
        b.onclick = async () => {
          const a = acts.find((x) => x.key === b.dataset.adst);
          const picked = await FolderPicker.open({
            source: sid, start: a.dst || '',
            title: `为「${a.album || a.artist || a.key}」指定目标目录`,
          });
          if (picked === null) return;
          a.dst = picked; a.kind = 'custom'; a.why = '手动指定的目标目录';
          // 手动改过的直接落到 plan.moves 里
          (plan.moves || []).forEach((mv) => {
            if ((a.items || []).some((i) => i.path === mv.path)) {
              mv.dst_dir = picked;
              mv.dst_path = `${picked}/${mv.name}`.replace(/^\/+/, '');
              mv.reason = '手动指定的目标目录';
            }
          });
          paintPlan();
        };
      });
      planBox.querySelectorAll('[data-amap]').forEach((b) => {
        b.onclick = () => mapDialog(acts.find((x) => x.key === b.dataset.amap));
      });
      // 展开 / 收起曲目明细（就在本行下方，不开二级弹窗）
      //
      // ★ 这里原来是"给 SVG 图标加 .open 类 → CSS 旋转 90°"。
      //   用户反复反馈这个箭头会"从左往右滑过来"，而且**失焦时**出现；
      //   前几轮把过渡一条条去掉都没根治（`.btn` 的 transform 过渡、
      //   `.og-exp .ico` 的 transition:none、`!important` 钉死……）。
      //   旋转这条路一直有残留，索性**换成纯文本箭头直接替换字符** ——
      //   没有 transform、没有 transition，动画在物理上就不可能存在。
      //   代价是少了那 150ms 的转动过程（用户明确不要），方向照样一眼可辨。
      planBox.querySelectorAll('[data-aexp]').forEach((b) => {
        b.onclick = () => {
          const tr = planBox.querySelector(
            `tr[data-items-for="${cssEsc(b.dataset.aexp)}"]`);
          if (!tr) return;
          const open = !tr.hidden;
          tr.hidden = open;
          const arrow = b.querySelector('.oga');
          if (arrow) arrow.textContent = open ? '▸' : '▾';
          b.title = open ? '展开该组的曲目明细（就在本行下方）' : '收起曲目明细';
        };
      });

      // ★ `<details>` 也用**文本箭头**，别用浏览器原生三角。
      //   原生 disclosure 三角由浏览器绘制，**展开/收起时浏览器自己做动画**，
      //   页面 CSS 管不到（`list-style:none` 只能藏掉它，换不来"瞬变"）。
      //   这里给它塞一个字符 span，在 toggle 事件里直接换字符。
      planBox.querySelectorAll('details').forEach((d) => {
        const sm = d.querySelector('summary');
        if (!sm || sm.querySelector('.oga')) return;
        const sp = document.createElement('span');
        sp.className = 'oga';
        sp.setAttribute('aria-hidden', 'true');
        sp.textContent = d.open ? '▾' : '▸';
        sm.insertBefore(sp, sm.firstChild);
        d.addEventListener('toggle', () => { sp.textContent = d.open ? '▾' : '▸'; });
      });
    }

    /** 属性选择器里的值要转义，键里可能带引号或反斜杠。 */
    function cssEsc(s) {
      return String(s).replace(/["\\]/g, '\\$&');
    }

    /** 自定义映射：把专辑/作者固定到自建目录。 */
    function mapDialog(a) {
      const keys = [a.album, a.artist].filter(Boolean);
      if (!keys.length) return UI.toast('这组没有专辑/作者信息', 'warn');
      const ctl2 = UI.modal({
        title: '自定义映射',
        size: 'sm',
        body: `<div class="muted" style="margin-bottom:8px;font-size:12px">
            以后凡是这个标签的曲目，自动建议进指定的目录（优先级最高）。</div>
          ${keys.map((k, i) => `<label class="check" style="display:flex;margin:5px 0">
            <input type="radio" name="awkey" value="${U.esc(k)}" ${i === 0 ? 'checked' : ''}>
            ${U.esc(k)}</label>`).join('')}
          <div class="field" style="margin-top:8px">
            <label>固定进这个目录</label>
            <div class="mv-dest-box">
              <div class="mono mv-dest-path" id="awm-dst">${U.esc(a.dst || '')}</div>
              <button class="btn sm" id="awm-pick">选目录…</button>
            </div>
          </div>`,
        footer: `<button class="btn ghost" data-act="no">取消</button>
                 <button class="btn primary" data-act="yes">保存并重新计算</button>`,
      });
      let dst = a.dst || '';
      ctl2.body.querySelector('#awm-pick').onclick = async () => {
        const picked = await FolderPicker.open({ source: sid, start: dst,
                                                 title: '固定进哪个目录' });
        if (picked === null) return;
        dst = picked;
        ctl2.body.querySelector('#awm-dst').textContent = dst;
      };
      ctl2.footer.querySelector('[data-act=no]').onclick = () => ctl2.close();
      ctl2.footer.querySelector('[data-act=yes]').onclick = async () => {
        const key = (ctl2.body.querySelector('input[name=awkey]:checked') || {}).value;
        if (!key || !dst) return UI.toast('还没选好', 'warn');
        custom[key] = dst;
        try {
          // **存到后端**，不只是这次会话的内存 —— 否则关掉窗口就没了，
          // 用户每次都得重建，映射功能等于不存在。
          await API.mappingOne({ source: sid, key, dst });
        } catch (e) {
          UI.toast('规则已在本机生效，但保存失败：' + e.message, 'warn', 7000);
        }
        ctl2.close();
        UI.toast(`已保存规则：${key} → ${dst}`, 'ok', 4000);
        compute();
      };
    }

    /**
     * 映射规则管理器：查看全部规则、逐条编辑 / 删除、清空。
     *
     * 之前映射只能"在建的时候顺便加一条"，加完就看不见了 —— 想改一条
     * 得先找到触发它的那张专辑，等于没法维护。现在有独立入口。
     */
    async function rulesDialog() {
      let rules = {};
      try {
        const r = await API.mappings(sid);
        rules = r.rules || {};
      } catch (e) {
        return UI.toast('读取映射规则失败：' + e.message, 'err', 7000);
      }
      const ctl2 = UI.modal({
        title: '自定义映射规则',
        size: 'lg',
        body: `
          <div class="muted" style="font-size:12px;margin-bottom:10px">
            映射的优先级最高：凡曲目的<b>专辑名</b>或<b>作者名</b>命中左侧规则，
            一律归入右侧目录，不再走其他判定。
            <br>规则会保存下来，下次打开智能整理仍然生效。
          </div>
          <div class="row" style="margin-bottom:8px">
            <button class="btn sm" id="mp-add">${U.icon('plus')}<span>新增规则</span></button>
            <div class="grow"></div>
            <span class="muted" id="mp-count"></span>
          </div>
          <div class="tbl-wrap" style="max-height:48vh">
            <table class="data"><thead><tr>
              <th>命中（专辑名 / 作者名）</th><th>归入目录</th>
              <th class="num">操作</th></tr></thead><tbody id="mp-body"></tbody>
            </table>
          </div>`,
        footer: `<button class="btn ghost danger" id="mp-clear">清空全部规则</button>
                 <div class="grow"></div>
                 <button class="btn ghost" data-act="no">关闭</button>`,
      });
      const bodyBox = ctl2.body.querySelector('#mp-body');
      const cntBox = ctl2.body.querySelector('#mp-count');

      function paintRules() {
        const keys = Object.keys(rules).filter((k) => k !== '__sink__');
        cntBox.textContent = keys.length ? `共 ${keys.length} 条` : '';
        bodyBox.innerHTML = keys.length ? keys.map((k) => `
          <tr data-k="${U.esc(k)}">
            <td class="ell" style="max-width:230px"><b>${U.esc(k)}</b></td>
            <td class="ell mono" style="max-width:300px" title="${U.esc(rules[k])}">
              ${U.esc(rules[k])}</td>
            <td class="num nowrap">
              <button class="btn sm ghost" data-mpedit="${U.esc(k)}">改目录</button>
              <button class="btn sm ghost" data-mpkey="${U.esc(k)}">改命中</button>
              <button class="btn sm ghost dangerish" data-mpdel="${U.esc(k)}"
                title="删除这条规则">✕</button>
            </td></tr>`).join('')
          : `<tr><td colspan="3" class="pad muted">
               还没有规则。可以在方案里对某一组点「映射」，或点上面的「新增规则」。</td></tr>`;

        bodyBox.querySelectorAll('[data-mpedit]').forEach((b) => {
          b.onclick = async () => {
            const k = b.dataset.mpedit;
            const picked = await FolderPicker.open({
              source: sid, start: rules[k] || '',
              title: `规则「${k}」归入哪个目录`,
            });
            if (picked === null) return;
            rules[k] = picked;
            await API.mappingOne({ source: sid, key: k, dst: picked });
            paintRules();
          };
        });
        bodyBox.querySelectorAll('[data-mpkey]').forEach((b) => {
          b.onclick = async () => {
            const k = b.dataset.mpkey;
            const nk = await UI.prompt('修改命中标签', '专辑名或作者名', k);
            if (!nk || nk === k) return;
            const dst = rules[k];
            delete rules[k];
            rules[nk] = dst;
            // 改键 = 删旧 + 加新
            await API.mappingOne({ source: sid, key: k, dst: '' });
            await API.mappingOne({ source: sid, key: nk, dst });
            paintRules();
          };
        });
        bodyBox.querySelectorAll('[data-mpdel]').forEach((b) => {
          b.onclick = async () => {
            const k = b.dataset.mpdel;
            delete rules[k];
            await API.mappingOne({ source: sid, key: k, dst: '' });
            paintRules();
            UI.toast(`已删除规则：${k}`, 'ok', 3000);
          };
        });
      }

      ctl2.body.querySelector('#mp-add').onclick = async () => {
        const k = await UI.prompt('新增规则', '命中哪个专辑名或作者名', '');
        if (!k) return;
        const picked = await FolderPicker.open({
          source: sid, start: '', title: `「${k}」归入哪个目录`,
        });
        if (picked === null) return;
        rules[k] = picked;
        await API.mappingOne({ source: sid, key: k, dst: picked });
        paintRules();
      };
      ctl2.footer.querySelector('#mp-clear').onclick = async () => {
        const n = Object.keys(rules).length;
        if (!n) return;
        const ok = await UI.confirm(`清空全部 <b>${n}</b> 条映射规则？`,
          { title: '清空规则', okText: '清空', danger: true });
        if (!ok) return;
        await API.mappingsSave({ source: sid, rules: {} });
        rules = {};
        paintRules();
        UI.toast('已清空全部规则', 'ok', 3000);
      };
      ctl2.footer.querySelector('[data-act=no]').onclick = async () => {
        ctl2.close();
        await compute();           // 规则可能变了，重算方案
      };
      paintRules();
    }

    // （曲目明细现在是分组行下方就地展开的 trackListHtml，
    //   不再需要单独的 itemsDialog 二级弹窗）

    // ---------------- ③ 执行 ----------------
    const run = async (execute) => {
      const keys = chosenKeys();
      if (!keys.length) return UI.toast('没有勾选任何专辑', 'warn');
      try {
        // ★ 组内单曲勾选：把被取消的曲目从**计划**里剔掉。
        //
        //   只在界面上灰掉是不够的 —— 后端拿的是整份 plan，
        //   不剔掉的话"我明明取消了这 3 首"照样会被搬走。
        //   这里是"用户看到什么就执行什么"的最后保证。
        const effPlan = plan;
        if (excluded.size && plan && Array.isArray(plan.moves)) {
          const keep = [];
          let dropped = 0;
          plan.moves.forEach((mv) => {
            if (excluded.has(String(mv.path || ''))) { dropped++; return; }
            keep.push(mv);
          });
          if (dropped) {
            effPlan = { ...plan, moves: keep };
            // actions 里的 items 也要同步，否则后端按 items 重算会把它们带回来
            effPlan.actions = (plan.actions || []).map((a) => ({
              ...a,
              items: (a.items || []).filter(
                (t) => !excluded.has(String(t.path || ''))),
            }));
          }
        }
        // 写操作必须走独占入口：
        //   · 多页面/多标签同时整理会基于过期的文件状态移动文件
        //   · 登记后别的页面能看到"这边在忙"，会禁用冲突操作
        const call = () => API.organizeAlbumApply({
          ...body(), only: keys, execute, confirm: execute, policy: 'skip',
          plan: effPlan, plan_hash: effPlan && effPlan.hashes,
        });
        const r = execute
          ? await App.runExclusive('organize', call)
          : await call();          // 预演是只读的，不占位
        const s = r.summary || {};
        if (!execute) {
          UI.toast(`预演：将移动 ${s.planned || s.done || 0} 首，`
            + `跳过 ${s.skipped || 0}，错误 ${s.error || 0}`, 'info', 9000);
          return;
        }
        ctl.close();
        const wait = UI.modal({
          title: '整理完成', size: 'md',
          body: `
            <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(110px,1fr))">
              ${UI.stat('已移动', r.moved || 0)}
              ${UI.stat('跳过', r.skipped || 0)}
              ${UI.stat('失败', r.failed || 0)}
              ${UI.stat('搬动体积', U.fmtBytes(s.bytes_moved || 0))}
            </div>
            ${(r.target_dirs || []).length ? `<div class="muted" style="font-size:12px;margin-top:8px">
              涉及 <b>${r.target_dirs.length}</b> 个目标目录</div>` : ''}
            ${r.failed ? `<div class="filtnote" style="margin-top:10px">
              ${r.failed} 首没搬动，通常是目标已有同名文件（策略=跳过），不会覆盖。</div>` : ''}`,
          footer: '<button class="btn primary">知道了</button>',
        });
        wait.footer.querySelector('button').onclick = () => wait.close();
        await App.loadSources();
        App.loadSystem && App.loadSystem();
      } catch (e) {
        UI.toast('整理失败：' + U.esc(e.message), 'err', 9000);
      }
    };
    dryBtn.onclick = () => run(false);
    yesBtn.onclick = async () => {
      const acts = (plan.actions || []).filter((a) => a.count > 0);
      const picked = acts.filter((a) => isChosen(a));
      const n = picked.reduce((x, a) => x + a.count, 0);
      const ok = await UI.confirm(
        `把 <b>${n}</b> 个文件按 <b>${picked.length}</b> 组归类？<br>
         <span class="muted">同名冲突按"跳过"处理（绝不覆盖）。只移动、不删除。
         新建的目录会按需创建。</span>`,
        { title: '执行整理', okText: '开始整理' });
      if (ok) run(true);
    };
    ctl.footer.querySelector('[data-act=no]').onclick = () => ctl.close();

    await loadScope();
    syncControls();          // 把记忆里的选择刷到控件上（含忽略范围/收纳区）
    await paintRulesInfo();
    await compute();
  }

  /* ============================================================
     搜索 + 分析：曲目应归入哪个目录

     按关键词检索曲目，再在**库内既有的文件夹**之间给出最合适的位置及依据。
     不新建目录、不重排整库，只回答"应归入何处"这一个问题。
     ============================================================ */
  async function analyze(sid, opt = {}) {
    const ctl = UI.modal({
      title: '查找曲目并分析目标目录',
      size: 'lg',
      body: `
        <div class="row" style="gap:8px;margin-bottom:10px">
          <input type="text" id="oa-q" placeholder="搜索歌名 / 作者 / 专辑 / 文件名（支持错别字与拼音缩写）"
                 style="flex:1" value="${U.esc(opt.q || '')}">
          <button class="btn primary sm" id="oa-go">${U.icon('search')}<span>分析</span></button>
        </div>
        <div id="oa-out"><div class="pad muted">
          输入关键词后回车。系统会在<b>库内既有的文件夹</b>中检索最合适的位置，
          并列出判定依据（目录名一致、目录内已有同专辑曲目等）。
        </div></div>`,
      footer: `<span class="muted grow" id="oa-sum"></span>
               <button class="btn ghost" data-act="no">关闭</button>`,
    });
    const qBox = ctl.body.querySelector('#oa-q');
    const out = ctl.body.querySelector('#oa-out');
    const sumBox = ctl.footer.querySelector('#oa-sum');
    ctl.footer.querySelector('[data-act=no]').onclick = () => ctl.close();

    const run = async () => {
      const q = qBox.value.trim();
      if (!q) return;
      out.innerHTML = '<div class="pad muted">分析中…</div>';
      let d;
      try {
        d = await API.organizeAnalyze(sid, { q });
      } catch (e) {
        out.innerHTML = `<div class="pad" style="color:var(--danger)">${U.esc(e.message)}</div>`;
        return;
      }
      paint(d);
    };

    function paint(d) {
      const t = d.track;
      const v = d.verdict || {};
      const tone = v.state === 'ok' ? 'ok' : (v.state === 'misplaced' ? 'warn' : '');
      out.innerHTML = `
        <div class="filtnote" style="margin-bottom:10px">
          <div class="row" style="gap:8px">
            <span class="badge ${tone}">${U.esc(v.label || '')}</span>
            <b class="ell">${U.esc(t.name)}</b>
          </div>
          <div class="muted" style="font-size:12px;margin-top:6px">
            ${U.esc(t.artist || '（无作者）')}${t.album ? ' · ' + U.esc(t.album) : ''}
            · ${U.esc(t.human_size || '')}
            ${t.is_derivative ? ' · <span class="tag">派生作品</span>' : ''}
          </div>
          <div class="mono" style="font-size:11.5px;margin-top:4px">
            现在在：${U.esc(t.folder || '根目录')}</div>
          <div class="muted" style="font-size:12px;margin-top:6px">${U.esc(v.detail || '')}</div>
          ${v.conflict ? `
            <div class="filtnote" style="margin-top:8px;border-color:var(--warn,#b8860b)">
              <b>⚠ 信息冲突</b>：${U.esc(v.conflict.detail)}
              <div class="muted" style="font-size:12px;margin-top:4px">
                更匹配的目录：<span class="mono">${U.esc(v.conflict.folder)}</span>
                —— ${v.conflict.why.map((w) => U.esc(w)).join('；')}
              </div>
            </div>` : ''}
        </div>
        ${(d.candidates || []).length ? `
          <div class="muted" style="font-size:12px;margin-bottom:6px">
            库里现有的候选位置（按证据强度排序，点「放到这里」即可搬过去）：</div>
          <div class="tbl-wrap" style="max-height:44vh">
            <table class="data"><thead><tr>
              <th>候选目录</th><th class="num">曲目</th><th>依据</th><th></th>
            </tr></thead><tbody>
            ${d.candidates.map((c) => `
              <tr>
                <td class="ell" style="max-width:230px" title="${U.esc(c.folder)}">
                  <b>${U.esc(c.folder || '根目录')}</b>
                  ${c.is_sink ? '<span class="tag">收纳区</span>' : ''}
                  <div class="muted" style="font-size:11px">
                    ${U.esc((c.sample || []).slice(0, 2).join('、'))}</div>
                </td>
                <td class="num">${c.count}</td>
                <td class="muted" style="font-size:12px">${c.why.map(
                  (w) => U.esc(w)).join('；')}</td>
                <td class="num">
                  <button class="btn sm" data-move="${U.esc(c.folder)}"
                    ${t.folder === c.folder ? 'disabled' : ''}>放到这里</button>
                </td>
              </tr>`).join('')}
            </tbody></table>
          </div>` : `
          <div class="empty" style="padding:24px">
            <div>库里没有找到合适的现成目录</div>
            <div class="muted" style="font-size:12px;margin-top:6px">
              这首歌的作者/专辑在库里还没有对应的文件夹。可以先手工建一个，
              或者用「智能整理」里的"收进收纳区"策略把它归拢起来。</div>
          </div>`}
        ${(d.also_matched || []).length ? `
          <div class="muted" style="font-size:12px;margin-top:8px">
            也匹配到（点一下改看这首）：
            ${d.also_matched.map((o) => `<button class="btn sm ghost" data-alt="${U.esc(o.path)}"
              >${U.esc((o.name || '').slice(0, 34))}</button>`).join(' ')}
          </div>` : ''}`;
      sumBox.textContent = `找到 ${(d.candidates || []).length} 个候选位置`;

      out.querySelectorAll('[data-alt]').forEach((b) => {
        b.onclick = async () => {
          out.innerHTML = '<div class="pad muted">分析中…</div>';
          try { paint(await API.organizeAnalyze(sid, { path: b.dataset.alt })); }
          catch (e) {
            out.innerHTML = `<div class="pad" style="color:var(--danger)">${U.esc(e.message)}</div>`;
          }
        };
      });

      out.querySelectorAll('[data-move]').forEach((b) => {
        b.onclick = async () => {
          const dst = b.dataset.move;
          const ok = await UI.confirm(
            `把 <b>${U.esc(t.name)}</b> 移动到 <span class="mono">${U.esc(dst || '根目录')}</span>？<br>
             <span class="muted">同名冲突按"跳过"处理，绝不覆盖已有文件；只移动、不删除。</span>`,
            { title: '确认移动', okText: '移动' });
          if (!ok) return;
          try {
            const rel = dst ? `${dst}/${t.name}` : t.name;
            // 同样走独占入口（这是一次真实的移动）
            const r = await App.runExclusive('organize', () => API.organizeApply({
              source: sid, execute: true, confirm: true, policy: 'skip',
              plan: {
                moves: [{
                  path: t.path, name: t.name, dst_dir: dst, dst_path: rel,
                  size: t.size || 0, from_dir: t.folder,
                }],
                summary: { total: 1 },
              },
            }));
            const s = r.summary || {};
            if (r.failed) {
              UI.toast(`没能移动：${(r.results || [])[0]?.detail || '目标已有同名文件'}`,
                'warn', 8000);
            } else {
              UI.toast(`已移动到「${dst || '根目录'}」`
                + (s.skipped ? `（跳过 ${s.skipped}）` : ''), 'ok', 6000);
            }
            await App.loadSources();
            App.loadSystem && App.loadSystem();
            run();
          } catch (e) {
            UI.toast('移动失败：' + U.esc(e.message), 'err', 9000);
          }
        };
      });
    }

    ctl.body.querySelector('#oa-go').onclick = run;
    qBox.onkeydown = (e) => { if (e.key === 'Enter') run(); };
    if (opt.q) run();
  }

  async function open(opt = {}) {
    const sid = opt.source || State.activeSource;
    if (!sid) return UI.toast('先选一个数据源', 'warn');
    const scoped = opt.path || '';

    let plan = null;
    let sinks = [];             // 用户勾选的收纳区
    let fallbackSink = '';      // 兜底目录
    let only = null;            // null = 全选；否则是 path 集合

    const ctl = UI.modal({
      title: scoped ? `整理文件夹「${opt.name || ''}」` : '智能整理：把放错位置的单曲归位',
      size: 'lg',
      body: `
        <div class="filtnote" style="margin-bottom:12px">
          <b>只修放错位置的歌，不重建你的目录结构。</b>
          精选集之类的"收纳文件夹"、以及已经放对的歌都不会被碰；
          这里只搬动、不删除任何文件。
        </div>

        <details class="og-sec" open>
          <summary>① 哪些文件夹是"故意混放"的收纳区（里面的歌一律不动）</summary>
          <div id="og-sinks" class="og-chips"></div>
          <div class="hint">
            自动按名字识别（单曲 / 精选集 / 合集 / 未分类 …），也可以自己勾。
            放错位置又找不到合适去处的歌，会优先收进这些目录里。
          </div>
        </details>

        <details class="og-sec" open>
          <summary>② 找不到合适去处的歌怎么办</summary>
          <div class="row wrap" style="gap:14px;align-items:center">
            <label class="inline"><input type="radio" name="og-orph" value="keep" checked>
              原地不动<span class="muted">（最保守，推荐）</span></label>
            <label class="inline"><input type="radio" name="og-orph" value="sink">
              收进收纳区</label>
            <label class="inline"><input type="radio" name="og-orph" value="new">
              够
              <input type="number" id="og-min" value="5" min="1" max="100"
                     style="width:62px;display:inline-block;vertical-align:middle">
              首就新建目录</label>
          </div>
          <div class="row" style="margin-top:6px">
            <label class="check"><input type="checkbox" id="og-gather" checked>
              顺带把"同一张专辑被拆到好几个目录"合并起来</label>
          </div>
        </details>

        <div class="row" style="margin:10px 0 6px">
          <button class="btn sm" id="og-refresh">${U.icon('refresh')}<span>重新计算</span></button>
          <div class="grow"></div>
          <span class="muted" id="og-progress"></span>
        </div>
        <div id="og-merges"></div>
        <div id="og-plan"></div>`,
      footer: `<span class="muted grow" id="og-sum"></span>
               <button class="btn ghost" data-act="no">关闭</button>
               <button class="btn" data-act="dry">${U.icon('eye')}<span>预演</span></button>
               <button class="btn primary" data-act="yes" data-exclusive disabled>
                 ${U.icon('check')}<span>执行整理</span></button>`,
    });

    const sinkBox = ctl.body.querySelector('#og-sinks');
    const planBox = ctl.body.querySelector('#og-plan');
    const sumBox = ctl.footer.querySelector('#og-sum');
    const progBox = ctl.body.querySelector('#og-progress');
    const yesBtn = ctl.footer.querySelector('[data-act=yes]');
    const dryBtn = ctl.footer.querySelector('[data-act=dry]');

    const body = () => {
      const orphan = ctl.body.querySelector('input[name=og-orph]:checked').value;
      return {
        source: sid, sinks, fallback_sink: fallbackSink,
        min_group: Number(ctl.body.querySelector('#og-min').value) || 5,
        orphan_policy: orphan,
        include_gather: ctl.body.querySelector('#og-gather').checked,
        ...(scoped ? { path: scoped } : {}),
      };
    };

    async function loadSinks() {
      try {
        const p = await API.organizePreview(sid, 200);
        const cands = p.sink_candidates || [];
        sinks = (p.sinks || []).slice();
        fallbackSink = sinks[0] || '';
        const merges = p.merge_suggestions || [];
        sinkBox.innerHTML = cands.length
          ? cands.map((f) => `<label class="chip ${sinks.includes(f) ? 'on' : ''}"
               data-sink="${U.esc(f)}"
               title="勾选=当成收纳区，里面的歌不会被移动">
               ${U.esc(f)}</label>`).join('')
            + `<div class="hint" style="flex-basis:100%">
                 点一下切换。兜底目录：<b>${U.esc(fallbackSink || '（自动）')}</b>
                 —— 需要收拢的歌会进这里。</div>`
          : '<span class="muted">未识别到收纳区</span>';
        sinkBox.querySelectorAll('[data-sink]').forEach((c) => {
          c.onclick = () => {
            const f = c.dataset.sink;
            if (sinks.includes(f)) {
              sinks = sinks.filter((x) => x !== f);
              if (fallbackSink === f) fallbackSink = sinks[0] || '';
            } else {
              sinks = sinks.concat([f]);
              if (!fallbackSink) fallbackSink = f;
            }
            c.classList.toggle('on', sinks.includes(f));
            compute();
          };
        });
        // 重复目录提示（≥5 首合并真正该管的场景）
        const box = ctl.body.querySelector('#og-merges');
        if (box) {
          box.innerHTML = merges.length
            ? `<div class="filtnote" style="margin-bottom:8px">
                 <b>发现 ${merges.length} 组"疑似重复目录"</b>
                 —— 名字只差一点、内容也重合，很可能是同一个作者被拼成了两种写法。
                 属于这一类的话，建议手工合并（或用「集合成文件夹」把歌并过去），
                 而不是让整理逻辑在两者之间来回搬。
               </div>
               <div class="tbl-wrap" style="max-height:22vh;margin-bottom:10px">
                 <table class="data"><thead><tr>
                   <th>建议保留</th><th>建议并入</th><th class="num">各自曲目</th>
                   <th></th></tr></thead><tbody>
                 ${merges.map((mg) => `<tr>
                   <td><b>${U.esc(mg.keep)}</b> <span class="muted">${mg.keep_count} 首</span></td>
                   <td>${U.esc(mg.drop)} <span class="muted">${mg.drop_count} 首</span></td>
                   <td class="num">${(mg.name_similarity * 100).toFixed(0)}% 像</td>
                   <td class="num"><button class="btn sm ghost" data-mergeshow="${U.esc(mg.drop)}"
                     >查看「${mg.drop}」</button></td>
                 </tr>`).join('')}
                 </tbody></table></div>`
            : '';
          box.querySelectorAll('[data-mergeshow]').forEach((b) => {
            b.onclick = () => {
              ctl.close();
              if (App.goFiles) App.goFiles(b.dataset.mergeshow);
            };
          });
        }
      } catch (e) {
        sinkBox.innerHTML = `<span style="color:var(--danger)">${U.esc(e.message)}</span>`;
      }
    }

    async function compute() {
      planBox.innerHTML = '<div class="pad muted">正在计算方案…</div>';
      yesBtn.disabled = true;
      sumBox.textContent = '';
      try {
        const r = scoped
          ? (await API.organizeFolder({ ...body(), execute: false })).plan
          : await API.organizePlan(body());
        plan = r;
        only = null;
        paint();
      } catch (e) {
        planBox.innerHTML = `<div class="pad" style="color:var(--danger)">${U.esc(e.message)}</div>`;
      }
    }

    const chosen = () => {
      if (!plan) return [];
      return only ? plan.moves.filter((m) => only.has(m.path)) : plan.moves;
    };

    function paint() {
      if (!plan) return;
      const s = plan.summary;
      const picked = chosen();
      const bytes = picked.reduce((a, m) => a + (m.size || 0), 0);
      progBox.textContent = '';

      if (!plan.moves.length) {
        planBox.innerHTML = `
          <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(130px,1fr));margin-bottom:10px">
            ${UI.stat('总曲目', s.total)}
            ${UI.stat('目录数', s.folders_now, `收纳区 ${s.sinks.length} 个`)}
            ${UI.stat('发现放错', 0)}
          </div>
          <div class="empty" style="padding:26px">
            <div class="big">${U.icon('check', 'ico')}</div>
            <div>没发现放错位置的歌 —— 你的目录结构已经很整齐了</div>
            <div class="muted" style="font-size:12px;margin-top:6px">
              工具只在"能证明放错了、而且有明确该去的地方"时才动手</div>
          </div>`;
        sumBox.textContent = '';
        yesBtn.disabled = true;
        return;
      }

      // 按"从哪来到哪去"分组，方便批量取舍
      const groups = new Map();
      plan.moves.forEach((m) => {
        const k = m.from_dir + '\u0000' + m.dst_dir;
        if (!groups.has(k)) groups.set(k, []);
        groups.get(k).push(m);
      });

      planBox.innerHTML = `
        <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(130px,1fr));margin-bottom:10px">
          ${UI.stat('总曲目', s.total)}
          ${UI.stat('发现放错', s.misplaced, '目录名对不上的歌')}
          ${UI.stat('建议处理', s.move, '能确定正确位置的')}
          ${UI.stat('原地不动', s.keep, '收纳区里的不参与')}
          ${UI.stat('搬动体积', U.fmtBytes(s.move_bytes))}
        </div>
        ${s.sinks.length ? `<div class="muted" style="font-size:12px;margin-bottom:8px">
          收纳区（不参与整理）：${s.sinks.map((x) =>
            `<span class="tag">${U.esc(x)}</span>`).join(' ')}</div>` : ''}
        <div class="row" style="margin-bottom:6px">
          <button class="btn sm ghost" id="og-all">全选 / 全不选</button>
          <span class="muted" style="font-size:12px">
            按「从哪来 → 到哪去」分组，可以只处理其中几组</span>
        </div>
        <div class="tbl-wrap" style="max-height:42vh">
          <table class="data"><thead><tr>
            <th style="width:38px"></th><th>从</th><th></th><th>到</th>
            <th class="num">曲目</th><th>示例</th></tr></thead><tbody>
          ${Array.from(groups.entries()).map(([k, items]) => {
            const [src, dst] = k.split('\u0000');
            const allSel = items.every((m) => picked.includes(m));
            return `<tr class="og-grp">
              <td><input type="checkbox" data-grp="${U.esc(k)}" ${allSel ? 'checked' : ''}></td>
              <td class="ell" style="max-width:190px" title="${U.esc(src)}">
                ${U.esc(src.split('/').pop() || '根目录')}</td>
              <td class="muted">→</td>
              <td><b>${U.esc(dst || '根目录')}</b></td>
              <td class="num">${items.length}</td>
              <td class="muted ell" style="max-width:330px"
                  title="${U.esc(items.map((m) => m.name).join('\n'))}">
                ${U.esc(items.slice(0, 2).map((m) => m.name).join('、'))}${
                  items.length > 2 ? ` … 等 ${items.length} 首` : ''}</td>
            </tr>`;
          }).join('')}
          </tbody></table>
        </div>
        <details style="margin-top:8px">
          <summary class="muted" style="cursor:pointer;font-size:12px">
            判定规则（凭什么说它放错了）</summary>
          <ul class="muted" style="font-size:12px;line-height:1.8;margin:8px 0 0;padding-left:18px">
            <li>收纳区里的歌一律不动（你可以自己勾哪些算收纳区）</li>
            <li>目录名跟作者/专辑对得上 → 位置正确，不动</li>
            <li>目录里大多数歌属于同一个作者/专辑，这首不是 → 走错门了</li>
            <li>目录里只有一两首、看不出主流 → 没有证据，不动</li>
            <li>找不到"名字对得上"的现成目录 → 按上面的策略处理或原地不动</li>
          </ul>
        </details>`;

      sumBox.innerHTML = `将处理 <b>${picked.length}</b>/${plan.moves.length} 首 · ${U.fmtBytes(bytes)}`;
      yesBtn.disabled = !picked.length;

      planBox.querySelectorAll('[data-grp]').forEach((cb) => {
        cb.onchange = () => {
          const items = groups.get(cb.dataset.grp) || [];
          const set = only ? new Set(only) : new Set(plan.moves.map((m) => m.path));
          items.forEach((m) => { if (cb.checked) set.add(m.path); else set.delete(m.path); });
          only = set;
          paint();
        };
      });
      planBox.querySelector('#og-all').onclick = () => {
        only = (chosen().length === plan.moves.length) ? new Set() : null;
        paint();
      };
    }

    const run = async (execute) => {
      const moves = chosen();
      if (!moves.length) return UI.toast('没有勾选任何要搬动的歌', 'warn');
      const api = scoped ? API.organizeFolder : API.organizeApply;
      const payload = {
        ...body(),
        only: moves.map((m) => m.path),
        execute, confirm: execute, policy: 'skip',
      };
      if (!scoped) { payload.plan = plan; if (plan.hashes) payload.plan_hash = plan.hashes; }
      try {
        const r = await api(payload);
        const s = r.summary || {};
        if (!execute) {
          UI.toast(`预演：将移动 ${s.planned || moves.length} 首，跳过 ${s.skipped || 0}，`
            + `错误 ${s.error || 0}`, 'info', 9000);
          return;
        }
        ctl.close();
        const wait = UI.modal({
          title: '整理完成', size: 'md',
          body: `
            <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(110px,1fr))">
              ${UI.stat('已移动', r.moved || 0)}
              ${UI.stat('跳过', r.skipped || 0)}
              ${UI.stat('失败', r.failed || 0)}
              ${UI.stat('搬动体积', U.fmtBytes(s.bytes_moved || 0))}
            </div>
            ${r.failed ? `<div class="filtnote" style="margin-top:10px">
              ${r.failed} 首没搬动，通常是目标已有同名文件（策略=跳过），
              同名文件不会被覆盖。</div>` : ''}`,
          footer: '<button class="btn primary">知道了</button>',
        });
        wait.footer.querySelector('button').onclick = () => wait.close();
        await App.loadSources();
        App.loadSystem && App.loadSystem();
        // 文件浏览页刷新一下，让用户马上看到新结构
        if (App.refreshFiles) App.refreshFiles();
      } catch (e) {
        UI.toast('整理失败：' + U.esc(e.message), 'err', 9000);
      }
    };

    ctl.body.querySelectorAll('input[name=og-orph]').forEach((r) => {
      r.onchange = compute;
    });
    ctl.body.querySelector('#og-min').onchange = compute;
    ctl.body.querySelector('#og-gather').onchange = compute;
    ctl.body.querySelector('#og-refresh').onclick = compute;
    ctl.footer.querySelector('[data-act=no]').onclick = () => ctl.close();
    dryBtn.onclick = () => run(false);
    yesBtn.onclick = async () => {
      const ok = await UI.confirm(
        `确认搬动 <b>${chosen().length}</b> 首歌到它们该去的目录？<br>
         <span class="muted">同名冲突按"跳过"处理（绝不覆盖已有文件）。
         只移动、不删除。</span>`,
        { title: '执行整理', okText: '开始整理' });
      if (ok) run(true);
    };

    await loadSinks();
    await compute();
  }

  return { open, analyze, albumWizard };
})();
