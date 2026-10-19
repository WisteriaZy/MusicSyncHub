/* ============================================================
   元数据编辑（标签 / 封面）

   两件事跟"搬文件"不一样，所以界面也做得更小心：
     · 目录搬错了还能搬回来，标签覆盖了原值就没地方找 —— 写入前会把原始标签
       备份成 JSON，结果弹窗中可直接撤销；
     · 批量编辑<b>只修改已勾选的字段</b>。为多首曲目补充"专辑"时，
       不会同时清空它们的标题等未勾选字段。
   ============================================================ */
const TagEditor = (() => {

  const FIELDS = [
    ['title', '标题'], ['artist', '作者'], ['album', '专辑'],
    ['albumartist', '专辑作者'], ['year', '年份'], ['genre', '流派'],
    ['track', '音轨号'], ['disc', '碟号'], ['comment', '备注'],
  ];
  const PLACEHOLDER = {
    title: '留空=不改（批量时请勾选左侧方框）',
    artist: '多位作者用 / 或 , 分隔',
    album: '同一张专辑的所有歌要一致',
    albumartist: '合辑常用「Various Artists」',
    year: '如 2024',
    genre: '如 Electronic',
    track: '如 3 或 3/12',
    disc: '如 1',
    comment: '任意备注',
  };

  /**
   * @param {object} opt {source, paths:[]}
   * paths 只给一个文件时是"单文件编辑"（显示当前值、封面可替换）。
   */
  async function open(opt = {}) {
    const sid = opt.source || State.activeSource;
    const paths = (opt.paths || []).filter(Boolean);
    if (!sid) return UI.toast('先选一个数据源', 'warn');
    if (!paths.length) return UI.toast('没有选中文件', 'warn');

    const many = paths.length > 1;
    let info = null;
    let changed = {};                  // 字段 → 新值（只放被勾选的）
    let picked = new Set();            // 批量时勾选了哪些字段
    let coverFile = null;              // 待写入的封面
    let coverDataUrl = null;
    let removeCover = false;

    const ctl = UI.modal({
      title: many ? `批量编辑标签（${paths.length} 个文件）` : '编辑标签',
      size: 'md',
      body: `<div id="te-out"><div class="pad muted">正在读取标签…</div></div>`,
      footer: `<span class="muted grow" id="te-sum"></span>
               <button class="btn ghost" data-act="no">取消</button>
               <button class="btn" data-act="bak">${U.icon('history')}<span>备份</span></button>
               <button class="btn primary" data-act="yes" disabled>
                 ${U.icon('check')}<span>写入</span></button>`,
    });
    const out = ctl.body.querySelector('#te-out');
    const sumBox = ctl.footer.querySelector('#te-sum');
    const yesBtn = ctl.footer.querySelector('[data-act=yes]');

    ctl.footer.querySelector('[data-act=no]').onclick = () => ctl.close();
    ctl.footer.querySelector('[data-act=bak]').onclick = showBackups;

    // ---------------- 读取 ----------------
    try {
      const r = await API.tagsRead(sid, paths[0]);
      info = r;
      if (r.cover_thumb) coverDataUrl = r.cover_thumb;
    } catch (e) {
      out.innerHTML = `<div class="pad" style="color:var(--danger)">${U.esc(e.message)}</div>`;
      return;
    }
    paint();

    function fieldRow(key, label) {
      const cur = info && info.fields ? (info.fields[key] || '') : '';
      const isPicked = picked.has(key);
      return `
        <div class="te-row ${many && isPicked ? 'on' : ''}" data-field="${key}">
          ${many ? `<label class="te-pick" title="勾上=要改这个字段">
            <input type="checkbox" data-pick="${key}" ${isPicked ? 'checked' : ''}></label>`
        : '<span class="te-pick"></span>'}
          <label class="te-label" for="te-${key}">${U.esc(label)}</label>
          <div class="te-input">
            <input type="text" id="te-${key}" data-in="${key}"
                   value="${many ? '' : U.esc(cur)}"
                   placeholder="${U.esc(many ? '勾选后可填写新值' : (PLACEHOLDER[key] || ''))}"
                   ${many && !isPicked ? 'disabled' : ''}>
            ${!many && cur ? `<span class="te-cur muted" title="当前值">${U.esc(cur)}</span>` : ''}
          </div>
        </div>`;
    }

    function paint() {
      const cur = info.fields || {};
      out.innerHTML = `
        <div class="te-head">
          ${coverDataUrl || (cur.cover && !removeCover)
            ? `<img class="te-cover" id="te-coverimg"
                 src="${U.esc(removeCover ? '' : coverDataUrl || '')}" alt="">`
            : '<div class="te-cover ph" id="te-coverimg"></div>'}
          <div style="min-width:0">
            <div class="ell"><b>${U.esc(many ? `${paths.length} 个文件` : info.name)}</b></div>
            <div class="muted" style="font-size:11.5px">
              ${U.esc(info.codec || '')} · ${U.fmtBytes(info.size)}
              ${info.duration ? ' · ' + U.fmtDur(info.duration) : ''}
              ${info.bitrate ? ' · ' + Math.round(info.bitrate / 1000) + ' kbps' : ''}
            </div>
            ${many ? `<div class="muted ell" style="font-size:11.5px;margin-top:4px"
                title="${U.esc(paths.join('\n'))}">
                勾选左侧方框的字段才会被写入；没勾的一律不动。</div>` : ''}
          </div>
          <div class="grow"></div>
          <div class="te-cover-act">
            <button class="btn sm ghost" id="te-pickcover">
              ${U.icon('upload')}<span>换封面</span></button>
            ${!many ? `<button class="btn sm ghost" id="te-delcover"
              ${cur.cover ? '' : 'disabled'} title="移除封面">✕</button>` : ''}
            <input type="file" id="te-coverfile" accept="image/*" style="display:none">
          </div>
        </div>
        <div class="te-grid">
          ${FIELDS.map(([k, l]) => fieldRow(k, l)).join('')}
        </div>`;

      // 勾选字段
      out.querySelectorAll('[data-pick]').forEach((cb) => {
        cb.onchange = () => {
          const k = cb.dataset.pick;
          if (cb.checked) picked.add(k); else picked.delete(k);
          const row = out.querySelector(`.te-row[data-field="${k}"]`);
          if (row) row.classList.toggle('on', cb.checked);
          const inp = out.querySelector(`[data-in="${k}"]`);
          if (inp) { inp.disabled = !cb.checked; if (cb.checked) inp.focus(); }
          sync();
        };
      });
      // 输入
      out.querySelectorAll('[data-in]').forEach((inp) => {
        inp.oninput = () => { changed[inp.dataset.in] = inp.value; sync(); };
      });
      // 封面
      const pf = out.querySelector('#te-pickcover');
      const file = out.querySelector('#te-coverfile');
      if (pf) pf.onclick = () => file.click();
      if (file) {
        file.onchange = () => {
          const f = file.files && file.files[0];
          if (!f) return;
          if (f.size > 12 * 1024 * 1024) {
            return UI.toast('封面图超过 12 MB，请先压缩', 'warn', 6000);
          }
          coverFile = f;
          removeCover = false;
          const rd = new FileReader();
          rd.onload = () => { coverDataUrl = String(rd.result); paint(); sync(); };
          rd.readAsDataURL(f);
        };
      }
      const dc = out.querySelector('#te-delcover');
      if (dc) {
        dc.onclick = () => {
          removeCover = true;
          coverDataUrl = null;
          coverFile = null;
          paint();
          sync();
        };
      }
      sync();
    }

    /** 汇总将要写入的内容。 */
    function payload() {
      const fields = many ? Array.from(picked) : FIELDS.map(([k]) => k);
      const changes = {};
      const list = many
        // 批量：只取勾选且填了值的字段
        ? fields.filter((k) => {
          const el = out.querySelector(`[data-in="${k}"]`);
          return el && el.value !== '';
        })
        : fields;
      list.forEach((k) => {
        const el = out.querySelector(`[data-in="${k}"]`);
        const v = el ? el.value : '';
        const cur = (info.fields || {})[k] || '';
        if (many) {
          if (v !== '') changes[k] = v;
        } else if (v !== cur) {
          changes[k] = v;
        }
      });
      return { fields: list, changes };
    }

    function sync() {
      const p = payload();
      const n = Object.keys(p.changes).length;
      const coverAct = coverFile ? '替换封面' : (removeCover ? '移除封面' : '');
      const parts = [];
      if (n) parts.push(`改 ${n} 个字段`);
      if (coverAct) parts.push(coverAct);
      sumBox.innerHTML = parts.length
        ? `将作用于 <b>${paths.length}</b> 个文件：${U.esc(parts.join(' + '))}`
        : '还没改动任何内容';
      yesBtn.disabled = !n && !coverAct;
    }

    // ---------------- 写入 ----------------
    yesBtn.onclick = async () => {
      const p = payload();
      if (!Object.keys(p.changes).length && !coverFile && !removeCover) {
        return UI.toast('没有要改的内容', 'warn');
      }
      // 先给一份"从什么变成什么"的预览，确认了再写
      let prev = null;
      try {
        prev = await API.tagsPreview({ source: sid, paths, changes: p.changes });
      } catch (e) { /* 预览失败也给写，但提示一下 */ }
      const rows = prev && prev.items
        ? prev.items.filter((i) => i.will_change).slice(0, 30).map((i) => {
          const ds = Object.entries(i.diff || {})
            .filter(([, d]) => d.changed)
            .map(([k, d]) => {
              const lbl = (FIELDS.find(([x]) => x === k) || [, k])[1];
              return `<div class="te-diff"><span class="muted">${U.esc(lbl)}</span>
                <s>${U.esc(d.from || '（空）')}</s> → <b>${U.esc(d.to || '（清空）')}</b></div>`;
            }).join('');
          return `<tr><td class="ell" style="max-width:230px" title="${U.esc(i.path)}">
            ${U.esc(i.name)}</td><td>${ds}</td></tr>`;
        }).join('') : '';
      const ok = await UI.confirm(
        `把改动写入 <b>${paths.length}</b> 个文件？
         ${prev ? `<div class="muted" style="font-size:12px;margin-top:4px">
           实际有 <b>${prev.to_change}</b> 个文件会变化
           ${prev.cannot ? `，${prev.cannot} 个无法处理` : ''}</div>` : ''}
         ${coverFile ? '<div class="muted" style="font-size:12px">含封面替换</div>' : ''}
         ${removeCover ? '<div class="muted" style="font-size:12px">移除封面</div>' : ''}
         <div class="muted" style="font-size:12px;margin-top:6px">
           写入前会自动备份原始标签，出问题可以一键撤销。</div>
         ${rows ? `<div class="tbl-wrap" style="max-height:34vh;margin-top:8px">
           <table class="data"><thead><tr><th>文件</th><th>变化</th></tr></thead>
           <tbody>${rows}</tbody></table></div>` : ''}`,
        { title: '确认写入标签', okText: '写入', danger: removeCover });
      if (!ok) return;

      const wait = UI.modal({
        title: '正在写入标签', size: 'sm',
        body: '<div class="muted">备份 → 写入 → 回读校验…</div>',
        footer: '<button class="btn ghost" disabled>关闭</button>',
      });
      try {
        const body = {
          source: sid, paths, fields: p.fields, changes: p.changes,
          confirm: true, remove_cover: removeCover,
        };
        if (coverFile) {
          body.cover_base64 = await new Promise((res, rej) => {
            const rd = new FileReader();
            rd.onload = () => res(String(rd.result));
            rd.onerror = rej;
            rd.readAsDataURL(coverFile);
          });
        }
        // 写标签是**改文件**（写 ID3/Vorbis 头），也要走独占入口：
        // 和移动/整理同时跑会互相打架（一个在挪文件、一个在改它的头）
        const r = await App.runExclusive('tags', () => API.tagsWrite(body));
        const fails = (r.results || []).filter((x) => x.state === 'error');
        const backups = (r.results || []).map((x) => x.backup).filter(Boolean);
        wait.body.innerHTML = `
          <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(110px,1fr))">
            ${UI.stat('已写入', r.ok || 0)}
            ${UI.stat('失败', r.failed || 0)}
            ${UI.stat('备份', backups.length)}
          </div>
          ${fails.length ? `<div class="tbl-wrap" style="max-height:180px;margin-top:10px">
            <table class="data"><thead><tr><th>文件</th><th>问题</th></tr></thead><tbody>
            ${fails.slice(0, 20).map((x) => `<tr>
              <td class="mono ell">${U.esc(x.path)}</td>
              <td><span class="badge err">${U.esc(x.detail || '')}</span></td></tr>`).join('')}
            </tbody></table></div>` : ''}`;
        wait.footer.innerHTML = `
          ${backups.length ? `<button class="btn" id="te-undo">撤销这次修改</button>` : ''}
          <button class="btn primary" id="te-done">完成</button>`;
        const done = wait.footer.querySelector('#te-done');
        done.disabled = false;
        done.onclick = () => {
          wait.close();
          ctl.close();
          App.loadSystem && App.loadSystem();
          // App.views 不存在（app.js 只导出 viewApi），原来这段永远为假：
          // 改完标签后文件视图从不刷新，还被 try/catch 吞掉。
          const fv = App.viewApi && App.viewApi('files');
          if (fv && fv.reload) {
            try { fv.reload(); } catch (e) { /* ignore */ }
          }
        };
        const undo = wait.footer.querySelector('#te-undo');
        if (undo) {
          undo.onclick = async () => {
            const yes = await UI.confirm(
              `把 <b>${backups.length}</b> 个文件的标签还原成写入之前的样子？`,
              { title: '撤销修改', okText: '还原' });
            if (!yes) return;
            let okN = 0; let badN = 0;
            for (const name of backups) {
              try {
                await API.tagsRestore({ name, source: sid, confirm: true });
                okN++;
              } catch (e) { badN++; }
            }
            UI.toast(`已还原 ${okN} 个文件` + (badN ? `，${badN} 个失败` : ''),
              badN ? 'warn' : 'ok', 7000);
            wait.close();
            ctl.close();
          };
        }
        UI.toast(`标签已写入 ${r.ok || 0} 个文件`, r.failed ? 'warn' : 'ok', 6000);
      } catch (e) {
        wait.body.innerHTML = `<div style="color:var(--danger)">${U.esc(e.message)}</div>`;
        wait.footer.querySelector('button').disabled = false;
        wait.footer.querySelector('button').onclick = () => wait.close();
      }
    };

    // ---------------- 备份列表 ----------------
    async function showBackups() {
      const ctl2 = UI.modal({
        title: '标签备份', size: 'md',
        body: '<div class="pad muted">读取中…</div>',
        footer: '<button class="btn ghost">关闭</button>',
      });
      ctl2.footer.querySelector('button').onclick = () => ctl2.close();
      let data;
      try {
        data = await API.tagsBackups(200);
      } catch (e) {
        ctl2.body.innerHTML = `<div class="pad" style="color:var(--danger)">${U.esc(e.message)}</div>`;
        return;
      }
      const list = data.backups || [];
      ctl2.body.innerHTML = list.length ? `
        <div class="muted" style="font-size:12px;margin-bottom:8px">
          每次写入标签前都会存一份原始值。点「还原」把某个文件恢复成当时的样子。</div>
        <div class="tbl-wrap" style="max-height:52vh">
          <table class="data"><thead><tr>
            <th>文件</th><th>当时的值</th><th></th></tr></thead><tbody>
          ${list.map((b) => `<tr>
            <td class="ell" style="max-width:230px" title="${U.esc(b.path || '')}">
              ${U.esc((b.path || '').split(/[\\/]/).pop() || '')}
              <div class="muted" style="font-size:11px">
                ${U.fmtDate ? U.fmtDate((b.when || 0) * 1000) : ''}</div></td>
            <td class="muted" style="font-size:11.5px">
              ${Object.entries(b.tags || {}).slice(0, 3).map(([k, v]) =>
                `${U.esc(k)}=${U.esc(String(v).slice(0, 20))}`).join(' · ') || '（无标签）'}
            </td>
            <td class="num"><button class="btn sm ghost"
              data-bak="${U.esc(b.name)}">还原</button></td></tr>`).join('')}
          </tbody></table>
        </div>` : '<div class="pad muted">还没有备份。写入标签时会自动生成。</div>';
      ctl2.body.querySelectorAll('[data-bak]').forEach((b) => {
        b.onclick = async () => {
          const yes = await UI.confirm('把这个文件的标签还原成备份里的样子？',
            { title: '还原标签', okText: '还原' });
          if (!yes) return;
          try {
            await API.tagsRestore({ name: b.dataset.bak, source: sid, confirm: true });
            UI.toast('已还原', 'ok', 4000);
            ctl2.close();
          } catch (e) {
            UI.toast('还原失败：' + U.esc(e.message), 'err', 7000);
          }
        };
      });
    }
  }

  return { open, FIELDS };
})();
