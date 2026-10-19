/* ============================================================
   文件夹选择器（可搜索）

   为什么单独抽出来：移动文件要在几十上百个目录里挑目标，
   纯靠一层层双击太累。所以这里给两种找法——
     · 搜索框：输入片段直接跳到目标目录（走索引，不用逐层点）
     · 浏览：仍然可以像资源管理器那样点进去，选中某层再点"用此目录"
   移动对话框和数据源的"浏览目录"都用它。
   ============================================================ */
const FolderPicker = (() => {

  /**
   * @param {{source:string, start?:string, title?:string,
   *          onPick:(absPath:string)=>void}} opt
   * @returns {Promise<string|null>} 选中的绝对路径；取消返回 null
   */
  /**
   * 打开目录选择器。
   *
   * @param {object} opt
   *   source  数据源 id
   *   start   起始目录
   *   title   标题
   *   multiple 允许多选（默认 false）。开启后每行带勾选框，
   *            顶部提供全选 / 全不选 / 反选，确认时返回**路径数组**；
   *            单选时返回单个路径字符串。
   *   minCount 多选时至少选几个才能确认（默认 1）
   */
  function open(opt) {
    const sid = opt.source;
    const multiple = !!opt.multiple;
    const minCount = Math.max(1, Number(opt.minCount) || 1);
    let cur = opt.start || '';
    let query = '';
    let seq = 0;              // 防止搜索的乱序返回覆盖新结果
    /** 多选时已勾选的路径（跨浏览/搜索保持） */
    const picked = new Set();

    return new Promise((resolve) => {
      const ctl = UI.modal({
        title: opt.title || (multiple ? '选择目录（可多选）' : '选择目标文件夹'),
        size: 'lg',
        body: `
          <div class="row" style="margin-bottom:10px">
            <input type="text" id="fp-q" placeholder="搜索文件夹名（例如：精选、OST、雪原）">
            <button class="btn ghost sm" id="fp-clear" style="display:none">清除</button>
          </div>
          <div class="row" style="margin-bottom:8px">
            <button class="btn ghost sm" id="fp-up" title="上级目录">${U.icon('prev')}<span>上级</span></button>
            <input type="text" id="fp-path" class="mono" style="flex:1" readonly value="">
          </div>
          ${multiple ? `
          <div class="row fp-bulk">
            <label class="check"><input type="checkbox" id="fp-all"> 全选当前列表</label>
            <button class="btn sm ghost" id="fp-none">全不选</button>
            <button class="btn sm ghost" id="fp-invert">反选</button>
            <span class="sep-v"></span>
            <button class="btn sm ghost" id="fp-here">勾选当前目录</button>
            <div class="grow"></div>
            <span class="muted" id="fp-count2"></span>
          </div>` : ''}
          <div class="tbl-wrap" style="max-height:46vh" id="fp-list"></div>
          <div class="muted" style="font-size:11.5px;margin-top:8px" id="fp-hint">
            ${multiple
              ? '勾选需要的目录 · 双击行进入下级 · 顶部可全选 / 全不选 / 反选（作用范围是当前列表）'
              : '单击选中目录 · 双击进入目录 · 也可以直接搜索后点结果'}
            <br>搜索基于已扫描的索引（未扫描过的目录可直接浏览进入）。
          </div>`,
        footer: `<span class="muted grow" id="fp-sel"></span>
                 <button class="btn ghost" data-act="no">取消</button>
                 <button class="btn primary" data-act="yes">${multiple ? '用这些目录' : '用此目录'}</button>`,
      });

      const qBox = ctl.body.querySelector('#fp-q');
      const pathBox = ctl.body.querySelector('#fp-path');
      const listBox = ctl.body.querySelector('#fp-list');
      const selInfo = ctl.footer.querySelector('#fp-sel');
      const clearBtn = ctl.body.querySelector('#fp-clear');
      const yesBtn = ctl.footer.querySelector('[data-act=yes]');
      const allBox = ctl.body.querySelector('#fp-all');
      const cnt2 = ctl.body.querySelector('#fp-count2');

      const finish = (v) => { ctl.close(); resolve(v); };
      const say = () => {
        if (multiple) {
          const n = picked.size;
          selInfo.innerHTML = n
            ? `已选 <b>${n}</b> 个目录`
            : `未选择目录（至少选 ${minCount} 个）`;
          if (cnt2) cnt2.textContent = n ? `已选 ${n} 个` : '';
          yesBtn.disabled = n < minCount;
        } else {
          selInfo.innerHTML = cur
            ? `已选：<span class="mono">${U.esc(cur)}</span>`
            : '未选择目录';
        }
      };

      /** 当前列表里的所有候选路径（浏览模式=子目录，搜索模式=结果）。 */
      const listedPaths = () => Array.from(listBox.querySelectorAll('[data-p]'))
        .map((tr) => tr.dataset.p).filter(Boolean);

      /** 刷新"全选"勾选框的状态（全选/半选）。 */
      function syncAllBox() {
        if (!allBox) return;
        const all = listedPaths();
        const on = all.filter((p) => picked.has(p)).length;
        allBox.checked = all.length > 0 && on === all.length;
        allBox.indeterminate = on > 0 && on < all.length;
      }

      /** 把 picked 状态画到每一行上。 */
      function paintPicked() {
        listBox.querySelectorAll('[data-p]').forEach((tr) => {
          const cb = tr.querySelector('input[data-pick]');
          const on = picked.has(tr.dataset.p);
          if (cb) cb.checked = on;
          tr.classList.toggle('picked', multiple && on);
        });
        syncAllBox();
        say();
      }

      function bindRows() {
        listBox.querySelectorAll('tr[data-p]').forEach((tr) => {
          tr.onclick = (e) => {
            const p = tr.dataset.p;
            if (multiple) {
              // 多选：点行 = 切换勾选；双击才进入下级
              if (e.target.closest('input[type=checkbox]')) return;
              togglePick(p);
              return;
            }
            cur = p; pathBox.value = cur; say(); markRow(tr);
          };
          tr.ondblclick = () => {
            if (multiple) { browse(tr.dataset.p); return; }
            browse(tr.dataset.p);
          };
          const cb = tr.querySelector('input[data-pick]');
          if (cb) {
            cb.onclick = (e) => e.stopPropagation();
            cb.onchange = () => togglePick(tr.dataset.p);
          }
        });
      }

      function togglePick(p) {
        if (picked.has(p)) picked.delete(p); else picked.add(p);
        paintPicked();
      }

      async function browse(p) {
        cur = p || '';
        pathBox.value = cur;
        say();
        listBox.innerHTML = '<div class="pad muted">读取中…</div>';
        try {
          const d = await API.fileList(sid, cur, 'count', 'desc');
          const rows = (d.dirs || []).map((x) => `
            <tr data-p="${U.esc(x.path)}" data-name="${U.esc(x.name)}">
              ${multiple ? `<td class="fp-c"><input type="checkbox" data-pick
                 ${picked.has(x.path) ? 'checked' : ''}></td>` : ''}
              <td class="ell"><svg class="ico"><use href="#i-folder"></use></svg>
                ${U.esc(x.name)}</td>
              <td class="num">${x.count} 首</td>
              <td class="num muted">${x.indexed === false ? '未索引' : ''}</td>
            </tr>`).join('');
          listBox.innerHTML = `<table class="data"><thead><tr>
              ${multiple ? '<th class="fp-c"></th>' : ''}
              <th>子目录</th><th class="num">曲目数</th><th class="num">备注</th>
            </tr></thead><tbody>${rows
              || `<tr><td colspan="${multiple ? 4 : 3}" class="pad muted">
                    这个目录下没有子目录${multiple ? '；可用上方「勾选当前目录」选中它本身' : ''}
                  </td></tr>`}</tbody></table>`;
          bindRows();
          paintPicked();
        } catch (e) {
          listBox.innerHTML = `<div class="pad" style="color:var(--danger)">${U.esc(e.message)}</div>`;
        }
      }

      function markRow(tr) {
        listBox.querySelectorAll('tr').forEach((x) => x.classList.remove('sel'));
        if (tr) tr.classList.add('sel');
      }

      async function doSearch(q) {
        query = q;
        clearBtn.style.display = q ? '' : 'none';
        if (!q) { browse(cur); return; }
        const mine = ++seq;
        listBox.innerHTML = '<div class="pad muted">搜索中…</div>';
        try {
          const r = await API.fileSearch(sid, q, 80);
          if (mine !== seq) return;                 // 有新搜索了，丢弃这次结果
          if (!r.total) {
            listBox.innerHTML = `<div class="pad muted">未匹配到「${U.esc(q)}」的目录。
              换个关键词，或清空搜索框用浏览方式查找。</div>`;
            paintPicked();
            return;
          }
          listBox.innerHTML = `
            <div class="muted" style="font-size:11.5px;margin-bottom:6px">
              找到 ${r.total} 个目录${r.total > r.folders.length ? `（显示前 ${r.folders.length} 个）` : ''}
            </div>
            <table class="data"><thead><tr>
              ${multiple ? '<th class="fp-c"></th>' : ''}
              <th>目录</th><th class="num">曲目数</th></tr></thead><tbody>
            ${r.folders.map((f) => `
              <tr data-p="${U.esc(f.path)}" data-rel="${U.esc(f.rel)}">
                ${multiple ? `<td class="fp-c"><input type="checkbox" data-pick
                   ${picked.has(f.path) ? 'checked' : ''}></td>` : ''}
                <td class="ell"><svg class="ico"><use href="#i-folder"></use></svg>
                  ${U.esc(f.rel)}</td>
                <td class="num">${f.count_all}</td>
              </tr>`).join('')}</tbody></table>`;
          bindRows();
          paintPicked();
        } catch (e) {
          if (mine === seq) {
            listBox.innerHTML = `<div class="pad" style="color:var(--danger)">${U.esc(e.message)}</div>`;
          }
        }
      }

      let t = null;
      qBox.oninput = () => {
        clearTimeout(t);
        const v = qBox.value.trim();
        t = setTimeout(() => doSearch(v), 220);      // 防抖，别每敲一个字就请求
      };
      qBox.onkeydown = (e) => {
        if (e.key === 'Escape') { qBox.value = ''; doSearch(''); }
        if (e.key === 'Enter') { clearTimeout(t); doSearch(qBox.value.trim()); }
      };
      clearBtn.onclick = () => { qBox.value = ''; doSearch(''); qBox.focus(); };

      // 全选 / 全不选 / 反选：作用范围是**当前列表**，不会把别的目录里勾过的清掉
      if (allBox) {
        allBox.onchange = () => {
          const all = listedPaths();
          if (allBox.checked) all.forEach((p) => picked.add(p));
          else all.forEach((p) => picked.delete(p));
          paintPicked();
        };
      }
      const noneBtn = ctl.body.querySelector('#fp-none');
      if (noneBtn) noneBtn.onclick = () => {
        listedPaths().forEach((p) => picked.delete(p));
        paintPicked();
      };
      const invBtn = ctl.body.querySelector('#fp-invert');
      if (invBtn) invBtn.onclick = () => {
        listedPaths().forEach((p) => {
          if (picked.has(p)) picked.delete(p); else picked.add(p);
        });
        paintPicked();
      };
      const hereBtn = ctl.body.querySelector('#fp-here');
      if (hereBtn) hereBtn.onclick = () => {
        if (!cur) return UI.toast('当前就是数据源根目录', 'warn');
        togglePick(cur);
      };

      ctl.body.querySelector('#fp-up').onclick = () => {
        if (query) { qBox.value = ''; doSearch(''); return; }
        const p = cur.replace(/\/+$/, '');
        const i = p.lastIndexOf('/');
        browse(i > 0 ? p.slice(0, i) : '');
      };
      ctl.footer.querySelector('[data-act=no]').onclick = () => finish(null);
      yesBtn.onclick = () => {
        if (multiple) {
          if (picked.size < minCount) return;
          return finish(Array.from(picked));
        }
        finish(cur);
      };

      say();
      browse(cur);
      setTimeout(() => qBox.focus(), 60);
    });
  }

  return { open };
})();
