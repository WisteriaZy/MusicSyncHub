/* ============================================================
   上传入库：把散装文件按现有音乐库结构归位

   三步走，和"智能整理"一个套路 —— 先看方案再动手：
     1. 选文件 / 拖进来       → 落到暂存区（不碰音乐库）
     2. 分析                 → 读标签、跟库里的目录比对、给出建议去向
     3. 确认                 → 才搬进音乐库（同名跳过、绝不覆盖）

   为什么要暂存：上传的文件名/标签常常是乱的，直接写进库里就是制造垃圾。
   ============================================================ */
const Uploader = (() => {

  const KIND_LABEL = {
    custom: ['自定义映射', 'ok'],
    album_exact: ['库里已有该专辑目录', 'ok'],
    artist_exact: ['库里已有该作者目录', 'ok'],
    similar: ['库里找到相似目录', ''],
    new: ['建议新建目录', 'warn'],
    sink: ['收进收纳区', ''],
  };

  async function open(opt = {}) {
    const sid = opt.source || State.activeSource;
    if (!sid) return UI.toast('先选一个数据源（文件要入到哪个库）', 'warn');

    let batch = null;
    let items = [];
    let custom = {};          // 专辑/作者 → 自建目录
    let dedupBusy = false;    // 查重任务进行中
    let dedupDone = false;    // 本次会话是否已查过重

    const ctl = UI.modal({
      title: '上传并自动分配文件夹',
      size: 'lg',
      body: `
        <div class="wiz-steps">
          <span class="wiz-step on" id="up-s1"><b>1</b> 选文件</span>
          <span class="muted">›</span>
          <span class="wiz-step" id="up-s2"><b>2</b> 分析去向</span>
          <span class="muted">›</span>
          <span class="wiz-step" id="up-s3"><b>3</b> 确认入库</span>
        </div>

        <div id="up-drop" class="up-drop">
          <div class="up-drop-in">
            ${U.icon('upload', 'ico lg')}
            <div><b>把音乐文件拖到这里</b>，或
              <button class="btn sm" id="up-pick">选择文件…</button></div>
            <div class="muted" style="font-size:12px;margin-top:6px">
              支持 flac / mp3 / m4a / wav / ogg / opus 等；文件先放暂存区，分析完才入库
            </div>
            <input type="file" id="up-input" multiple accept="audio/*,.flac,.mp3,.m4a,.wav,.ogg,.opus,.aiff,.ape,.wma"
                   style="display:none">
          </div>
        </div>

        <div id="up-batches"></div>
        <div id="up-out"></div>`,
      footer: `<span class="muted grow" id="up-sum"></span>
               <button class="btn ghost" data-act="no">关闭</button>
               <button class="btn primary" data-act="go" data-exclusive disabled>
                 ${U.icon('check')}<span>入库</span></button>`,
    });

    const dropBox = ctl.body.querySelector('#up-drop');
    const input = ctl.body.querySelector('#up-input');
    const outBox = ctl.body.querySelector('#up-out');
    const batchBox = ctl.body.querySelector('#up-batches');
    const sumBox = ctl.footer.querySelector('#up-sum');
    const goBtn = ctl.footer.querySelector('[data-act=go]');

    ctl.footer.querySelector('[data-act=no]').onclick = () => ctl.close();
    ctl.body.querySelector('#up-pick').onclick = () => input.click();
    input.onchange = () => { doUpload(Array.from(input.files || [])); input.value = ''; };

    // 拖放
    ['dragenter', 'dragover'].forEach((ev) => dropBox.addEventListener(ev, (e) => {
      e.preventDefault(); dropBox.classList.add('over');
    }));
    ['dragleave', 'drop'].forEach((ev) => dropBox.addEventListener(ev, (e) => {
      e.preventDefault(); dropBox.classList.remove('over');
    }));
    dropBox.addEventListener('drop', (e) => {
      const fs = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
      if (fs.length) doUpload(fs);
    });

    // 上传（按 10 个一批，避免单个请求太大）
    async function doUpload(files) {
      const audio = files.filter((f) => f.size > 0);
      if (!audio.length) return UI.toast('没有可上传的文件', 'warn');
      const prog = UI.modal({
        title: '正在上传', size: 'sm',
        body: `<div class="muted" id="up-prog">0 / ${audio.length}</div>
               <div class="up-bar"><i id="up-bar" style="width:0%"></i></div>
               <div class="muted" id="up-bytes" style="font-size:11.5px;margin-top:6px"></div>`,
        footer: '<button class="btn ghost" disabled>请稍候…</button>',
      });
      const bar = prog.body.querySelector('#up-bar');
      const txt = prog.body.querySelector('#up-prog');
      const bytesEl = prog.body.querySelector('#up-bytes');
      let done = 0;
      const CHUNK = 10;
      // 总字节数：用来显示"已经传了多少"。
      // 用户一次拖几十个文件时，光看"3 / 40"不够 —— 大文件要传很久，
      // 有字节数和速度才让人确信"它在动"。
      const totalBytes = audio.reduce((a, f) => a + (f.size || 0), 0);
      let sentBytes = 0;
      let t0 = Date.now();
      const paintBytes = () => {
        const secs = Math.max(0.5, (Date.now() - t0) / 1000);
        const rate = sentBytes / secs;
        bytesEl.textContent = totalBytes
          ? `${U.fmtBytes(sentBytes)} / ${U.fmtBytes(totalBytes)}`
            + (rate > 0 ? `　${U.fmtBytes(rate)}/s` : '')
            + (rate > 0 && sentBytes < totalBytes
                ? `　约剩 ${Math.ceil((totalBytes - sentBytes) / rate)} 秒` : '')
          : '';
      };
      try {
        for (let i = 0; i < audio.length; i += CHUNK) {
          const part = audio.slice(i, i + CHUNK);
          const partBytes = part.reduce((a, f) => a + (f.size || 0), 0);
          const fd = new FormData();
          part.forEach((f) => fd.append('files', f, f.name));
          fd.append('source', sid);
          const base = sentBytes;
          // 上传中的字节级进度（XMLHttpRequest 才能给）
          const r = await API.uploadFilesProgress(fd, (p) => {
            sentBytes = base + (p.sent || 0);
            txt.textContent = `${done} / ${audio.length} 个文件`
              + `（第 ${Math.min(i + Math.floor(p.percent / 100 * part.length) + 1,
                                  audio.length)} 个传输中 ${p.percent}%）`;
            bar.style.width = `${Math.round((base + (p.sent || 0))
              / Math.max(1, totalBytes) * 100)}%`;
            paintBytes();
          });
          batch = r.batch || batch;
          done += part.length;
          sentBytes = base + partBytes;
          txt.textContent = `${done} / ${audio.length} 个文件`;
          bar.style.width = `${Math.round(sentBytes / Math.max(1, totalBytes) * 100)}%`;
          paintBytes();
        }
        prog.close();
        UI.toast(`已收进暂存区：${done} 个文件（${U.fmtBytes(totalBytes)}）`, 'ok', 4000);
        await inspect();
      } catch (e) {
        prog.close();
        UI.toast('上传失败：' + U.esc(e.message), 'err', 9000);
      }
    }

    // 分析
    async function inspect() {
      if (!batch) return;
      ctl.body.querySelector('#up-s2').classList.add('on');
      outBox.innerHTML = '<div class="pad muted">正在分析标签并跟音乐库比对…</div>';
      try {
        const d = await API.uploadInspect(batch, sid, 5, JSON.stringify(custom));
        // 合并"已保存的精确查重结果"：分析阶段本身只做零成本粗筛，
        // 之前查过重的话这里把 exact 结果补上，不用重跑一遍。
        try {
          const saved = await API.uploadDups(batch);
          const dups = (saved && saved.dups) || {};
          if (Object.keys(dups).length) {
            dedupDone = true;
            (d.items || []).forEach((it) => {
              const hit = dups[it.rel];
              // exact 优先于粗筛出的 likely
              if (hit && (!it.dup || hit.level === 'exact')) it.dup = hit;
            });
          }
        } catch (e) { /* 没查过重就算了 */ }
        items = d.items || [];
        paint(d);
        batchBox.innerHTML = '';
      } catch (e) {
        outBox.innerHTML = `<div class="pad" style="color:var(--danger)">${U.esc(e.message)}</div>`;
      }
    }

    /**
     * 启动查重（后台任务）。
     *
     * 分析阶段只做了"同名同体积"的零成本粗筛；精确判定要把每个文件读一遍
     * 算内容哈希，几百首就是几 GB 的 IO —— 所以做成后台任务：
     * 有进度、可离开、可取消，不把界面卡住。
     */
    async function runDedup() {
      if (dedupBusy) return;
      dedupBusy = true;
      const start = Date.now();
      try {
        const r = await API.uploadDedupStart(batch, sid);
        UI.toast(`开始查重，共 ${r.total} 个文件（可在下方查看进度）`, 'ok', 4000);
        // 轮询进度
        let st = null;
        for (let i = 0; i < 3000; i++) {
          await new Promise((res) => setTimeout(res, 700));
          try {
            st = await API.uploadDedupStatus(r.job);
          } catch (e) { break; }
          paintDedupProgress(st);
          if (st.state !== 'running') break;
        }
        if (st && st.state === 'done') {
          const n = Object.keys((st.result || {}).dups || {}).length;
          UI.toast(n ? `查重完成：发现 ${n} 个疑似重复`
                     : '查重完成：未发现重复', 'ok', 6000);
        } else if (st && st.state === 'cancelled') {
          UI.toast('查重已取消', 'warn');
        } else if (st && st.state === 'error') {
          UI.toast('查重失败：' + st.error, 'err', 8000);
        }
        dedupDone = true;
        await inspect();                 // 重新分析以带上查重结果
      } catch (e) {
        UI.toast('查重失败：' + e.message, 'err', 8000);
      } finally {
        dedupBusy = false;
        void start;
      }
    }

    /** 查重进度（只在运行时显示）。 */
    function paintDedupProgress(st) {
      const box = outBox.querySelector('#up-dup-prog');
      if (!box) return;
      if (!st || st.state !== 'running') { box.innerHTML = ''; return; }
      const pct = st.percent == null ? 0 : st.percent;
      const eta = st.eta != null ? `，预计还需 ${Math.ceil(st.eta)} 秒` : '';
      box.innerHTML = `
        <div class="muted" style="font-size:11.5px;margin-bottom:3px">
          正在查重 ${st.done} / ${st.total}${eta}
          ${st.current ? '· ' + U.esc(String(st.current).slice(0, 40)) : ''}</div>
        <div class="pbar"><i style="width:${pct}%"></i></div>`;
    }

    function paint(d) {
      if (!items.length) {
        outBox.innerHTML = `<div class="empty" style="padding:24px">
          <div>暂存区里没有文件了</div></div>`;
        goBtn.disabled = true;
        sumBox.textContent = '';
        return;
      }
      // 按建议去向分列显示：与「候选区」同一套列组布局，
      // 每个去向一列、列头带颜色和完整路径，右侧是该去向下的文件。
      const groups = new Map();
      items.forEach((it, i) => {
        const k = it.dst || '';
        if (!groups.has(k)) groups.set(k, []);
        groups.get(k).push({ it, i });
      });
      const bytes = items.reduce((a, i) => a + (i.size || 0), 0);
      // 查重结果：只**提示**，不替用户决定 —— 勾选状态始终由用户掌握
      const dupList = items.map((it, i) => ({ it, i })).filter((x) => x.it.dup);
      const dupExact = dupList.filter((x) => x.it.dup.level === 'exact').length;
      const dupLikely = dupList.length - dupExact;
      const cols = Array.from(groups.entries());
      const colHtml = ([dst, list]) => {
        const c = Staging.colorOf(dst);
        const gbytes = list.reduce((a, x) => a + (x.it.size || 0), 0);
        const kinds = Array.from(new Set(list.map((x) => x.it.kind)));
        const label = kinds.length === 1
          ? (KIND_LABEL[kinds[0]] || [kinds[0], ''])[0] : '多种判定';
        const tone = kinds.length === 1 ? (KIND_LABEL[kinds[0]] || ['', ''])[1] : '';
        return `
          <section class="stg-col" style="--c:${c}">
            <header class="stg-col-hd">
              <span class="stg-dot" style="background:${c}"></span>
              <div class="stg-col-title" title="${U.esc(dst || '（数据源根目录）')}">
                <b>${U.esc(dst || '（根目录）')}</b>
                <span class="muted">${U.esc(dst || '数据源根目录')}</span>
              </div>
              <span class="stg-col-n">${list.length} 个 · ${U.fmtBytes(gbytes)}</span>
              <button class="btn icon sm ghost" data-upcolall="${U.esc(dst)}"
                title="勾选本列全部文件">☑</button>
            </header>
            <div class="stg-col-list">
              <div class="muted" style="font-size:11px;padding:2px 6px">
                <span class="badge ${tone}">${U.esc(label)}</span>
                ${U.esc(list[0].it.why || '')}</div>
              ${list.map(({ it, i }) => `
                <div class="stg-item" data-uidx="${i}" style="--c:${c}">
                  <input type="checkbox" data-upcheck="${i}" checked>
                  ${it.cover_url
                    ? `<img class="cover xs" src="${U.esc(it.cover_url)}" loading="lazy" alt="">`
                    : '<span class="cover xs ph"></span>'}
                  <div class="stg-item-main">
                    <div class="ell" title="${U.esc(it.name)}">${U.esc(it.name)}</div>
                    <div class="ell muted" style="font-size:11px">
                      ${U.esc(it.artist || '无作者')}${it.album ? ' · ' + U.esc(it.album) : ''}
                      · ${U.fmtBytes(it.size)}</div>
                    ${it.dup ? `<div class="dup-flag ${it.dup.level}"
                        title="${U.esc(it.dup.why)}">${U.icon('info')}
                      <span>${it.dup.level === 'exact' ? '库内已有相同内容'
                        : '疑似重复'}：${U.esc((it.dup.match || {}).name || '')}</span></div>` : ''}
                  </div>
                  <div class="stg-item-act">
                    <button class="btn icon sm ghost" data-updst="${i}"
                      title="更改去向">${U.icon('folder')}</button>
                    <button class="btn sm ghost" data-upmap="${i}"
                      title="给这类标签建映射，之后自动进入该目录">映射</button>
                  </div>
                </div>`).join('')}
            </div>
          </section>`;
      };

      outBox.innerHTML = `
        <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(120px,1fr));margin-bottom:10px">
          ${UI.stat('待入库', items.length, U.fmtBytes(bytes))}
          ${UI.stat('目标目录', cols.length, `库中现有 ${d.library_folders || 0} 个`)}
          ${UI.stat('疑似重复', dupList.length,
            dupExact ? `内容相同 ${dupExact} 个` : '需你确认')}
          ${UI.stat('已勾选', items.length, '取消勾选即可跳过')}
        </div>
        ${dupList.length ? `
          <div class="dup-banner">
            <div class="dup-banner-hd">
              ${U.icon('info')}
              <b>查重结果：${dupList.length} 个文件可能已存在于库中</b>
              <span class="muted">
                ${dupExact ? `其中 ${dupExact} 个内容完全相同` : ''}
                ${dupLikely ? `${dupExact ? '，' : ''}${dupLikely} 个仅文件名与体积相同` : ''}
              </span>
            </div>
            <div class="muted" style="font-size:12px;margin:4px 0 8px">
              是否入库由你决定。以下操作都不会改动你的勾选以外的文件。</div>
            <div class="row">
              <button class="btn sm" id="up-dup-uncheck">取消勾选这些重复项</button>
              <button class="btn sm ghost" id="up-dup-keep">仍然全部入库</button>
              <div class="grow"></div>
              <span class="muted" style="font-size:11.5px">
                单项也可在左侧手动勾选 / 取消</span>
            </div>
          </div>` : ''}
        <div class="row" style="margin-bottom:6px">
          <button class="btn sm" id="up-dedup"
                  title="逐文件比对库内曲目内容，判断是否重复。文件较多时需要一些时间，可后台进行">
            ${U.icon('search')}<span>${dedupDone ? '重新查重' : '检查是否重复'}</span></button>
          ${dedupDone && !dupList.length
            ? '<span class="badge ok">未发现重复</span>' : ''}
          <span class="muted" style="font-size:12px">
            分析结果按目标目录分列显示；可逐个更改去向，或为整类标签建立映射规则。</span>
          <div class="grow"></div>
          <button class="btn sm ghost" id="up-all">全选</button>
          <button class="btn sm ghost" id="up-none">全不选</button>
          <button class="btn sm ghost" id="up-reinspect">${U.icon('refresh')}<span>重新分析</span></button>
        </div>
        <div id="up-dup-prog" style="margin-bottom:6px"></div>
        <div class="stg-cols" style="max-height:42vh;padding:0">${cols.map(colHtml).join('')}</div>`;

      sumBox.innerHTML = `已选 <b>${items.length}</b> 个文件 · ${U.fmtBytes(bytes)}`;
      goBtn.disabled = false;
      ctl.body.querySelector('#up-s2').classList.add('done');
      ctl.body.querySelector('#up-s3').classList.add('on');

      outBox.querySelector('#up-reinspect').onclick = inspect;
      const ddBtn = outBox.querySelector('#up-dedup');
      if (ddBtn) {
        ddBtn.disabled = dedupBusy;
        ddBtn.onclick = runDedup;
      }
      const dupUncheck = outBox.querySelector('#up-dup-uncheck');
      if (dupUncheck) dupUncheck.onclick = () => {
        // 只动重复项，其余保持原样；用户随时可以再勾回来
        dupList.forEach(({ i }) => {
          const cb = outBox.querySelector(`[data-upcheck="${i}"]`);
          if (cb) cb.checked = false;
        });
        syncChecked();
        UI.toast(`已取消勾选 ${dupList.length} 个重复项，可随时改回`, 'ok', 5000);
      };
      const dupKeep = outBox.querySelector('#up-dup-keep');
      if (dupKeep) dupKeep.onclick = () => {
        outBox.querySelectorAll('[data-upcheck]').forEach((cb) => { cb.checked = true; });
        syncChecked();
        UI.toast('已全部勾选，重复项也会入库', 'ok', 4000);
      };
      outBox.querySelector('#up-all').onclick = () => {
        outBox.querySelectorAll('[data-upcheck]').forEach((cb) => { cb.checked = true; });
        syncChecked();
      };
      outBox.querySelector('#up-none').onclick = () => {
        outBox.querySelectorAll('[data-upcheck]').forEach((cb) => { cb.checked = false; });
        syncChecked();
      };
      outBox.querySelectorAll('[data-upcolall]').forEach((b) => {
        b.onclick = () => {
          const dst = b.dataset.upcolall;
          outBox.querySelectorAll('[data-upcheck]').forEach((cb) => {
            const it = items[Number(cb.dataset.upcheck)];
            if (it && (it.dst || '') === dst) cb.checked = true;
          });
          syncChecked();
        };
      });
      outBox.querySelectorAll('[data-upcheck]').forEach((cb) => {
        cb.onchange = () => syncChecked();
      });
      outBox.querySelectorAll('[data-updst]').forEach((b) => {
        b.onclick = async () => {
          const it = items[Number(b.dataset.updst)];
          const picked = await FolderPicker.open({
            source: sid, start: it.dst || '', title: `为「${it.name}」指定目标目录`,
          });
          if (picked === null) return;
          it.dst = picked;
          it.kind = 'custom';
          it.why = '手动指定的目标目录';
          paint(d);
        };
      });
      outBox.querySelectorAll('[data-upmap]').forEach((b) => {
        b.onclick = () => mappingDialog(items[Number(b.dataset.upmap)]);
      });
      syncChecked();
    }

    /** 按勾选状态更新底部汇总与入库按钮。 */
    function syncChecked() {
      const boxes = Array.from(outBox.querySelectorAll('[data-upcheck]'));
      const on = boxes.filter((cb) => cb.checked).length;
      const bytes = boxes.filter((cb) => cb.checked)
        .reduce((a, cb) => a + (items[Number(cb.dataset.upcheck)] || {}).size || 0, 0);
      sumBox.innerHTML = on
        ? `已勾选 <b>${on}</b> / ${items.length} 个文件 · ${U.fmtBytes(bytes)}`
        : '尚未勾选任何文件';
      goBtn.disabled = on === 0;
    }

    /** 自定义映射：把某个专辑/作者固定映射到你自建的目录。 */
    function mappingDialog(it) {
      const keys = [it.album, it.artist].filter(Boolean);
      if (!keys.length) return UI.toast('这个文件没有专辑/作者信息，没法建映射', 'warn');
      const ctl2 = UI.modal({
        title: '自定义映射',
        size: 'sm',
        body: `<div class="muted" style="margin-bottom:8px;font-size:12px">
            以后凡是这个标签的文件，自动建议进指定的目录。</div>
          ${keys.map((k, i) => `<label class="check" style="display:flex;margin:5px 0">
            <input type="radio" name="upkey" value="${U.esc(k)}" ${i === 0 ? 'checked' : ''}>
            ${U.esc(k)}</label>`).join('')}
          <div class="field" style="margin-top:8px">
            <label>固定进这个目录</label>
            <div class="mv-dest-box">
              <div class="mono mv-dest-path" id="upm-dst">${U.esc(it.dst || '')}</div>
              <button class="btn sm" id="upm-pick">选目录…</button>
            </div>
          </div>`,
        footer: `<button class="btn ghost" data-act="no">取消</button>
                 <button class="btn primary" data-act="yes">保存映射</button>`,
      });
      let dst = it.dst || '';
      ctl2.body.querySelector('#upm-pick').onclick = async () => {
        const picked = await FolderPicker.open({ source: sid, start: dst,
                                                 title: '这个标签固定进哪个目录' });
        if (picked === null) return;
        dst = picked;
        ctl2.body.querySelector('#upm-dst').textContent = dst;
      };
      ctl2.footer.querySelector('[data-act=no]').onclick = () => ctl2.close();
      ctl2.footer.querySelector('[data-act=yes]').onclick = () => {
        const key = (ctl2.body.querySelector('input[name=upkey]:checked') || {}).value;
        if (!key || !dst) return UI.toast('还没选好', 'warn');
        custom[key] = dst;
        ctl2.close();
        UI.toast(`已记住：${key} → ${dst}`, 'ok', 4000);
        inspect();
      };
    }

    // 入库
    goBtn.onclick = async () => {
      const picks = Array.from(outBox.querySelectorAll('[data-upcheck]'))
        .filter((cb) => cb.checked).map((cb) => Number(cb.dataset.upcheck));
      if (!picks.length) return UI.toast('尚未勾选任何文件', 'warn');
      const moves = picks.map((i) => ({ rel: items[i].rel, dst: items[i].dst || '' }));
      const byDst = new Map();
      moves.forEach((m) => byDst.set(m.dst, (byDst.get(m.dst) || 0) + 1));
      const bytes = picks.reduce((a, i) => a + (items[i].size || 0), 0);

      // ★ 入库前先确认**目标盘**装得下。
      //   暂存区够用不等于目标盘够用（可能是另一块盘／另一个挂载点），
      //   搬到一半失败会在库里留下一半文件，很难收拾。
      //   后端提交时还会再校验一次（这里只是提前把话说清楚）。
      let diskNote = '';
      let blockReason = '';
      try {
        const fl = await API.fileList(sid, '');
        const dk = fl.disk || {};
        if (dk.total) {
          const freeTxt = U.fmtBytes(dk.free);
          if (bytes > dk.free) {
            blockReason = `目标盘（${dk.dir}）只剩 ${freeTxt}，这一批需要 `
              + `${U.fmtBytes(bytes)}。请先清理磁盘，或少选一些分批入库。`;
          } else if (bytes > dk.free * 0.5) {
            diskNote = `<br><span style="color:var(--warn)">注意：这批占了目标盘剩余空间的 `
              + `${Math.round(bytes / dk.free * 100)}%`
              + `（${U.fmtBytes(bytes)} / 剩余 ${freeTxt}）。</span>`;
          } else {
            diskNote = `<br><span class="muted">目标盘剩余 ${freeTxt}，空间充足。</span>`;
          }
        }
      } catch (e) { /* 查不到就不拦，交给后端判定 */ }
      if (blockReason) {
        return UI.confirm(blockReason,
          { title: '目标盘空间不足', okText: '知道了', danger: true });
      }

      const ok = await UI.confirm(
        `把这 <b>${moves.length}</b> 个文件（${U.fmtBytes(bytes)}）搬进音乐库？<br>
         <span class="muted">目标 ${byDst.size} 个目录；同名冲突按"跳过"处理，
         绝不覆盖已有文件；搬完会自动刷新索引。</span>${diskNote}
         <div class="tbl-wrap" style="max-height:26vh;margin-top:8px">
           <table class="data"><thead><tr><th>目标目录</th><th class="num">文件数</th></tr></thead>
           <tbody>${Array.from(byDst.entries()).map(([k, v]) =>
             `<tr><td class="ell" style="max-width:300px">${U.esc(k || '（根目录）')}</td>
              <td class="num">${v}</td></tr>`).join('')}</tbody></table></div>`,
        { title: '确认入库', okText: '开始入库' });
      if (!ok) return;
      const wait = UI.modal({
        title: '正在入库', size: 'md',
        body: '<div class="muted">正在搬文件…</div>',
        footer: '<button class="btn ghost" disabled>关闭</button>',
      });
      try {
        // 入库会**真的搬文件进音乐库** → 必须走独占入口
        const r = await App.runExclusive('upload', () => API.uploadCommit({
          batch, source: sid, moves, confirm: true, policy: 'skip', scan: true,
        }));
        wait.body.innerHTML = `
          <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(110px,1fr))">
            ${UI.stat('已入库', r.moved || 0)}
            ${UI.stat('跳过', r.skipped || 0)}
            ${UI.stat('失败', r.failed || 0)}
            ${UI.stat('搬入体积', U.fmtBytes(r.bytes || 0))}
          </div>
          ${(r.target_dirs || []).length ? `<div class="muted" style="font-size:12px;margin-top:8px">
            落到了 <b>${r.target_dirs.length}</b> 个目录</div>` : ''}
          ${r.scan_started ? `<div class="muted" style="font-size:12px;margin-top:6px">
            已开始后台刷新索引，稍等一下就能在文件浏览里看到。</div>` : ''}
          ${r.remaining_in_batch ? `<div class="muted" style="font-size:12px;margin-top:6px">
            暂存区还剩 ${r.remaining_in_batch} 个文件（失败或被跳过的会留着）。</div>` : ''}`;
        wait.footer.querySelector('button').disabled = false;
        wait.footer.querySelector('button').onclick = () => {
          wait.close();
          ctl.close();
        };
        UI.toast(`已入库 ${r.moved || 0} 个文件`, r.failed ? 'warn' : 'ok', 7000);
        App.loadSources && App.loadSources();
        App.loadSystem && App.loadSystem();
      } catch (e) {
        wait.body.innerHTML = `<div style="color:var(--danger)">${U.esc(e.message)}</div>`;
        wait.footer.querySelector('button').disabled = false;
        wait.footer.querySelector('button').onclick = () => wait.close();
      }
    };

    // 列出暂存区里已有的批次，方便接着上次继续
    try {
      const b = await API.uploadBatches();
      const batches = b.batches || [];
      if (batches.length) {
        // ★ 非音频杂物要在这里说清楚。
        //   用户可能往暂存区目录里手扔过东西（说明.txt、截图…），
        //   它们不会出现在"待入库文件"里；不讲明白的话，
        //   用户会以为文件丢了，或者反复重传。
        const junk = batches.reduce((a, x) => a + (x.other_count || 0), 0);
        const junkNote = junk ? `
          <div class="card" style="margin:10px 0;border-color:var(--warn)">
            <b>${U.icon('info')}<span>暂存区里有 ${junk} 个非音频文件</span></b>
            <div class="muted" style="font-size:12px;margin-top:4px">
              它们不是音乐文件，<b>不会出现在入库列表里、也不会被入库</b>，
              但确实占着磁盘。想清掉的话去「中转站 → 上传暂存区」，
              那里能看到具体是哪些文件。</div>
          </div>` : '';
        batchBox.innerHTML = `
          ${junkNote}
          <div class="muted" style="font-size:12px;margin:10px 0 4px">
            暂存区里还有这些批次（点一下继续处理）：</div>
          <div class="og-chips">${batches.slice(0, 8).map((x) =>
            `<span class="chip" data-batch="${U.esc(x.batch)}">
               ${U.esc(x.batch)} · ${x.count} 个${x.other_count
                 ? `（另有 ${x.other_count} 个非音频）` : ''} · ${
                 U.esc(x.human_bytes)}${x.stale ? ' · <b>滞留</b>' : ''}</span>`
          ).join('')}</div>`;
        batchBox.querySelectorAll('[data-batch]').forEach((c) => {
          c.onclick = () => { batch = c.dataset.batch; inspect(); };
        });
        // 从「中转站 → 上传暂存区」点「继续入库」进来的：直接接管那个批次
        if (opt.batch && batches.some((x) => x.batch === opt.batch)) {
          batch = opt.batch;
          inspect();
        }
      } else if (opt.batch) {
        UI.toast('那个批次已经不在暂存区里了', 'warn', 5000);
      }
    } catch (e) { /* 没有批次就算了 */ }
  }

  return { open };
})();
