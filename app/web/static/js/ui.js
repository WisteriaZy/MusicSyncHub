/* ============================================================
   UI 基础组件：toast / modal / confirm / 进度
   ============================================================ */
const UI = (() => {
  /**
   * 右下角通知（Win10 操作中心样式）。
   *
   * 结构固定成三行：应用名 / 标题 / 正文，左侧一个状态图标。
   * 这样和系统通知观感一致，用户一眼就知道"哪来的、什么级别、要干什么"。
   *
   * 为了**不改所有调用点**，`msg` 仍然接受纯文本或 HTML 片段；
   * 想指定标题就传 `{title, msg}`。老调用（`toast('已保存','ok')`）
   * 完全照旧 —— 只加能力，不改契约。
   */
  const ICO = { ok: '✓', err: '!', warn: '!', info: 'i' };
  const APP = 'Music Sync Hub';

  function toast(msg, type = 'info', ms = 4200) {
    const root = document.getElementById('toast-root');
    if (!root) return null;
    const t = (type === 'ok' || type === 'err' || type === 'warn') ? type : 'info';
    let title = '';
    let body = msg;
    if (msg && typeof msg === 'object' && !(msg instanceof Node)) {
      title = String(msg.title || '');
      body = (msg.msg !== undefined) ? msg.msg : '';
    }
    const el = document.createElement('div');
    el.className = 'toast ' + t;
    // 标题 + 关闭按钮。正文沿用调用方给的 HTML（调用点一直在做转义）。
    el.innerHTML =
      `<span class="t-ico">${ICO[t] || 'i'}</span>`
      + '<div class="t-body">'
      + `<div class="t-app">${APP}</div>`
      // ★ 一律转义。`Toast` 的文案绝大多数来自服务端的 `detail`，
      //   而 detail 里常内嵌**用户可控的文件名/目录名** —— 直接 innerHTML
      //   等于把文件名当 HTML 执行（构造一个叫 `<img onerror=...>` 的文件即可）。
      //   全项目 204 处 toast 调用**没有一处**传 HTML 标签，所以默认转义不会
      //   破坏任何现有用法。
      + (title ? `<div class="t-title">${U.esc(title)}</div>` : '')
      + `<div class="t-msg">${body == null ? '' : U.esc(body)}</div>`
      + '</div>'
      + '<span class="t-x" title="关闭">✕</span>';
    root.appendChild(el);

    let gone = false;
    const close = () => {
      if (gone) return;
      gone = true;
      el.style.transition = 'opacity .18s, transform .18s';
      el.style.opacity = '0';
      el.style.transform = 'translateX(18px)';
      setTimeout(() => el.remove(), 200);
    };
    const x = el.querySelector('.t-x');
    if (x) x.onclick = close;
    // 鼠标停在上面就别自动消失 —— 长消息（比如失败清单）用户要看
    let timer = setTimeout(close, ms);
    el.addEventListener('mouseenter', () => { clearTimeout(timer); });
    el.addEventListener('mouseleave', () => { timer = setTimeout(close, 1800); });
    return el;
  }

  /**
   * 打开弹窗。
   * @param {{title:string, body:string|HTMLElement, footer?:string|HTMLElement,
   *          size?:string, onMount?:Function, closeOnMask?:boolean}} opt
   */
  function modal(opt) {
    const root = document.getElementById('modal-root');
    const mask = document.createElement('div');
    mask.className = 'modal-mask';
    const m = document.createElement('div');
    m.className = 'modal ' + (opt.size || 'md');

    const hd = document.createElement('div');
    hd.className = 'modal-hd';
    hd.innerHTML = `<h2>${U.esc(opt.title || '')}</h2>`;
    const x = document.createElement('button');
    x.className = 'x'; x.innerHTML = U.icon('close'); x.title = '关闭';
    hd.appendChild(x);

    const bd = document.createElement('div');
    bd.className = 'modal-bd';
    if (opt.body instanceof HTMLElement) bd.appendChild(opt.body);
    else bd.innerHTML = opt.body || '';

    m.appendChild(hd); m.appendChild(bd);

    let ft = null;
    if (opt.footer) {
      ft = document.createElement('div');
      ft.className = 'modal-ft';
      if (opt.footer instanceof HTMLElement) ft.appendChild(opt.footer);
      else ft.innerHTML = opt.footer;
      m.appendChild(ft);
    }
    mask.appendChild(m);
    root.appendChild(mask);

    const ctl = {
      el: m, body: bd, footer: ft, mask,
      close() { mask.remove(); document.removeEventListener('keydown', onKey); },
      setBusy(on) {
        m.querySelectorAll('button,input,select,textarea').forEach((b) => {
          if (on) { b.dataset.prevDis = b.disabled; b.disabled = true; }
          else if (b.dataset.prevDis === 'false' || b.dataset.prevDis === undefined) b.disabled = false;
        });
      },
    };
    function onKey(e) { if (e.key === 'Escape') ctl.close(); }
    document.addEventListener('keydown', onKey);
    x.onclick = () => ctl.close();
    mask.addEventListener('mousedown', (e) => {
      if (e.target === mask && opt.closeOnMask !== false) ctl.close();
    });
    if (opt.onMount) opt.onMount(ctl);
    const focusable = m.querySelector('input,select,textarea,button.primary');
    if (focusable) setTimeout(() => focusable.focus(), 30);
    return ctl;
  }

  /**
   * **高风险操作的二次确认**：要用户亲手把词打对才放行。
   *
   * 为什么不满足于普通 confirm：`confirm` 只要点一下，连着做几次操作时
   * 很容易条件反射点过去。而"永久删除""覆盖"这类操作**不可逆**，
   * 值得多花两秒 —— 让手指离开鼠标去打字，是打断肌肉记忆的有效办法。
   *
   * @param {object} o
   *   · title / message   —— 说清后果（尤其是"不可恢复"）
   *   · phrase            —— 要用户原样输入的词，默认「确认删除」
   *   · okText / danger   —— 按钮文案与配色
   *   · details           —— 受影响的对象清单（文件名等），让后果可见
   */
  function confirmHard(o = {}) {
    const title = o.title || '高风险操作确认';
    const phrase = o.phrase || '确认删除';
    const okText = o.okText || '执行';
    const details = Array.isArray(o.details) ? o.details : [];
    return new Promise((resolve) => {
      const shown = details.slice(0, 8);
      const more = details.length - shown.length;
      const ctl = modal({
        title,
        size: 'sm',
        body: `
          <div style="line-height:1.7;font-size:13px">${o.message || ''}</div>
          ${details.length ? `
            <div class="hard-list">
              ${shown.map((x) => `<div class="mono ell" title="${U.esc(x)}">${U.esc(x)}</div>`).join('')}
              ${more > 0 ? `<div class="muted">…还有 ${more} 项</div>` : ''}
            </div>` : ''}
          <div class="field" style="margin-top:12px">
            <label>请输入 <b class="mono">${U.esc(phrase)}</b> 以继续</label>
            <input type="text" id="hard-phrase" autocomplete="off" spellcheck="false"
                   placeholder="${U.esc(phrase)}">
          </div>`,
        footer: `<button class="btn ghost" data-act="no">取消</button>
                 <button class="btn ${o.danger === false ? 'primary' : 'danger'}" data-act="yes" disabled>${U.esc(okText)}</button>`,
        onMount(c) {
          const inp = c.body.querySelector('#hard-phrase');
          const yes = c.footer.querySelector('[data-act=yes]');
          const upd = () => { yes.disabled = inp.value.trim() !== phrase; };
          inp.addEventListener('input', upd);
          inp.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !yes.disabled) { c.close(); resolve(true); }
          });
          upd();
          setTimeout(() => inp.focus(), 40);
          c.footer.querySelector('[data-act=no]').onclick = () => { c.close(); resolve(false); };
          yes.onclick = () => { c.close(); resolve(true); };
        },
      });
      void ctl;
    });
  }

  function confirm(message, { title = '请确认', okText = '确定', okClass = 'primary', danger = false } = {}) {
    return new Promise((resolve) => {
      const ctl = modal({
        title,
        size: 'sm',
        body: `<div style="line-height:1.7;font-size:13px">${message}</div>`,
        footer: `<button class="btn ghost" data-act="no">取消</button>
                 <button class="btn ${danger ? 'danger' : okClass}" data-act="yes">${U.esc(okText)}</button>`,
        onMount(c) {
          c.body.dataset.role = 'confirm-body';
          c.footer.querySelector('[data-act=no]').onclick = () => { c.close(); resolve(false); };
          c.footer.querySelector('[data-act=yes]').onclick = () => { c.close(); resolve(true); };
        },
      });
      void ctl;
    });
  }

  /** 带输入框的确认（例如输入新名称） */
  function prompt(title, label, value = '') {
    return new Promise((resolve) => {
      modal({
        title, size: 'sm',
        body: `<div class="field"><label>${U.esc(label)}</label>
               <input type="text" id="prompt-input" value="${U.esc(value)}"></div>`,
        footer: `<button class="btn ghost" data-act="no">取消</button>
                 <button class="btn primary" data-act="yes">确定</button>`,
        onMount(c) {
          const inp = c.body.querySelector('#prompt-input');
          inp.select();
          const done = (v) => { c.close(); resolve(v); };
          c.footer.querySelector('[data-act=no]').onclick = () => done(null);
          c.footer.querySelector('[data-act=yes]').onclick = () => done(inp.value.trim());
          inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') done(inp.value.trim()); });
        },
      });
    });
  }

  function coverHtml(t, cls = 'cover') {
    const url = t.cover_url || (t.has_cover ? `/api/cover/${t.id}?size=96` : null);
    const ph = `<div class="${cls} cover ph">${U.icon('music')}</div>`;
    if (url) return `<img class="${cls}" loading="lazy" src="${url}" alt="" onerror='this.outerHTML=${JSON.stringify(ph)}'>`;
    return ph;
  }

  /** 统计块。`k` 允许传 HTML（仅限调用方自己写的标签），`v`/`sub` 会被转义。
   *  `sub` 放在数值下面单独一行——放在同一行时较长的说明会挤成一团。 */
  function stat(k, v, sub = '') {
    return `<div class="stat"><div class="k">${k}</div>` +
      `<div class="v">${U.esc(v)}</div>` +
      (sub ? `<div class="sub">${U.esc(sub)}</div>` : '') +
      '</div>';
  }

  /** 右键菜单。items: [{label, icon?, onPick, danger?, disabled?, sep?}]
   *  返回一个 close() 以便调用方主动关掉。 */
  function contextMenu(x, y, items) {
    document.querySelectorAll('.ctx-menu').forEach((n) => n.remove());
    const m = document.createElement('div');
    m.className = 'ctx-menu';
    m.innerHTML = items.map((it, i) => {
      if (it.sep) return '<div class="ctx-sep"></div>';
      return `<button class="ctx-item ${it.danger ? 'danger' : ''}" data-i="${i}"
        ${it.disabled ? 'disabled' : ''}>${it.icon ? U.icon(it.icon) : ''}
        <span>${U.esc(it.label)}</span></button>`;
    }).join('');
    document.body.appendChild(m);
    // 贴着鼠标放，但别超出窗口
    const r = m.getBoundingClientRect();
    const left = Math.min(x, window.innerWidth - r.width - 8);
    const top = Math.min(y, window.innerHeight - r.height - 8);
    m.style.left = Math.max(4, left) + 'px';
    m.style.top = Math.max(4, top) + 'px';

    const close = () => {
      m.remove();
      document.removeEventListener('mousedown', onDoc, true);
      document.removeEventListener('keydown', onKey, true);
      window.removeEventListener('blur', close);
    };
    function onDoc(e) { if (!m.contains(e.target)) close(); }
    function onKey(e) { if (e.key === 'Escape') { e.stopPropagation(); close(); } }
    m.querySelectorAll('.ctx-item').forEach((b) => {
      b.onclick = () => {
        const it = items[Number(b.dataset.i)];
        close();
        if (it && it.onPick) it.onPick();
      };
    });
    setTimeout(() => {
      document.addEventListener('mousedown', onDoc, true);
      document.addEventListener('keydown', onKey, true);
      window.addEventListener('blur', close);
    }, 0);
    const first = m.querySelector('.ctx-item:not([disabled])');
    if (first) first.focus();
    return { close };
  }

  /** 让一行（或任意元素）支持右键菜单 + 键盘菜单键 */
  function attachMenu(node, buildItems) {
    node.oncontextmenu = (e) => {
      e.preventDefault();
      e.stopPropagation();
      const items = buildItems();
      if (items && items.length) contextMenu(e.clientX, e.clientY, items);
    };
  }

  /** 轮询任务直到结束，回调进度 */
  async function watchJob(jobId, onTick) {
    if (onTick && typeof onTick === 'object') {
      // 兼容 `watchJob(id, {onProgress})` 这种写法
      const o = onTick;
      onTick = (j) => { if (o.onProgress) o.onProgress(j); };
    }
    for (;;) {
      let j;
      try { j = await API.job(jobId); } catch (e) { return null; }
      if (onTick) onTick(j);
      if (j.state !== 'running' && j.state !== 'paused') {
        // 后台任务的结果放在 result 里；直接返回它，调用方就不用再拆一层
        const r = j.result || {};
        if (r && typeof r === 'object') { r._job = j; }
        return r;
      }
      await new Promise((r) => setTimeout(r, 700));
    }
  }

  /**
   * 跑一个后台任务并显示**带进度、可暂停、可取消**的对话框。
   *
   * 相比"转圈等结果"：用户能看到"正在校验文件内容 120/430"、
   * 知道还要多久、能中途取消或暂停 —— 不会因为"没反应"就刷新甚至强杀。
   *
   * @returns 任务结果（取消/失败时返回 null 或 {cancelled:true}）
   */
  function watchJobDialog(jobId, opts = {}) {
    const title = opts.title || '正在处理';
    return new Promise((resolve) => {
      const ctl = modal({
        title,
        size: 'sm',
        body: `
          <div class="muted" id="wj-msg">正在开始…</div>
          <div class="progress" style="margin:8px 0 4px">
            <i id="wj-bar" style="width:0%"></i></div>
          <div class="muted" id="wj-num" style="font-size:11.5px"></div>`,
        footer: `<div class="grow"></div>
          <button class="btn sm ghost" id="wj-pause">暂停</button>
          <button class="btn sm ghost" id="wj-cancel">取消</button>`,
      });
      const msg = ctl.body.querySelector('#wj-msg');
      const bar = ctl.body.querySelector('#wj-bar');
      const num = ctl.body.querySelector('#wj-num');
      const bp = ctl.footer.querySelector('#wj-pause');
      const bc = ctl.footer.querySelector('#wj-cancel');
      let paused = false;
      let done = false;

      bp.onclick = async () => {
        try {
          if (paused) { await API.resumeJob(jobId); paused = false; bp.textContent = '暂停'; }
          else { await API.pauseJob(jobId); paused = true; bp.textContent = '继续'; }
        } catch (e) { toast('操作失败：' + e.message, 'err'); }
      };
      bc.onclick = async () => {
        try { await API.cancelJob(jobId); } catch (e) { /* ignore */ }
        msg.textContent = '正在取消…（已经做完的部分不会回退）';
        bc.disabled = true; bp.disabled = true;
      };

      (async () => {
        const res = await watchJob(jobId, {
          onProgress: (j) => {
            if (done) return;
            msg.textContent = j.message || (j.paused ? '已暂停' : '处理中…');
            if (j.percent != null) bar.style.width = `${j.percent}%`;
            num.textContent = j.total
              ? `${j.done} / ${j.total}${j.eta ? `　约剩 ${j.eta}s` : ''}`
              : (j.elapsed ? `已用 ${j.elapsed}s` : '');
            paused = !!j.paused;
            bp.textContent = paused ? '继续' : '暂停';
            bp.disabled = j.state !== 'running' && j.state !== 'paused';
            if (j.state !== 'running' && j.state !== 'paused') {
              bc.disabled = true; bp.disabled = true;
            }
          },
        });
        done = true;
        ctl.close();
        const j = (res && res._job) || {};
        if (j.state === 'cancelled') { resolve({ cancelled: true, _job: j }); return; }
        if (j.state === 'error') {
          toast((opts.title || '任务') + '失败：' + (j.error || '未知错误'), 'err', 8000);
          resolve(null); return;
        }
        // 去掉内部字段，别让调用方拿到 _job
        if (res && typeof res === 'object' && '_job' in res) delete res._job;
        resolve(res);
      })();
    });
  }

  return { toast, modal, confirm, confirmHard, prompt, coverHtml, stat, watchJob,
           watchJobDialog, contextMenu, attachMenu };
})();
