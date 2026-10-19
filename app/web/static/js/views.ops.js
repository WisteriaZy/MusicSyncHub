/* ============================================================
   视图：操作记录 + 任务中心

   两件事放一起，因为它们回答的是同一个问题："我做过什么、现在在做什么"。
   ============================================================ */
(() => {
  let el = null;
  let expanded = null;
  let timer = null;

  function mount(root) { el = root; }

  function render() {
    el.innerHTML = `
      <div class="toolbar">
        <b>操作与任务</b>
        <span class="muted" id="op-count">本次会话 ${State.opLog.length} 次操作</span>
        <div class="grow"></div>
        <button class="btn ghost sm" id="op-refresh">${U.icon('refresh')}<span>刷新</span></button>
        <button class="btn ghost sm" id="op-clear-finished"
        title="把所有已结束（完成/失败/取消）的任务从记录里删掉">清理已结束任务</button>
        <button class="btn ghost sm" id="op-clear">清空操作记录</button>
        <button class="btn ghost sm" id="op-jobs">打开任务中心</button>
      </div>
      <div id="op-body"></div>`;
    el.querySelector('#op-refresh').onclick = async () => {
      await App.refreshJobs(); paint();
    };
    el.querySelector('#op-clear-finished').onclick = async () => {
      const n = (State.jobs || []).filter(
        (j) => ['done', 'error', 'cancelled'].includes(j.state)).length;
      if (!n) return UI.toast('没有已结束的任务', 'info', 3000);
      const ok2 = await UI.confirm(
        `清理这 <b>${n}</b> 条已结束的任务记录？<br>` +
        `<span class="muted">只删记录，不影响任何文件；正在运行的任务不会被动。</span>`,
        { title: '清理任务记录', okText: '清理' });
      if (!ok2) return;
      try {
        const r = await API.clearFinishedJobs();
        UI.toast(`已清理 ${r.removed} 条任务记录`, 'ok', 4000);
        await App.refreshJobs();
        paint();
      } catch (e) {
        UI.toast('清理失败：' + e.message, 'err', 6000);
      }
    };
    el.querySelector('#op-clear').onclick = () => {
      UI.confirm(`清空这 ${State.opLog.length} 条操作记录？<br>
        <span class="muted">只清界面上的记录，不会影响任何文件。</span>`,
        { title: '清空记录', okText: '清空', danger: true })
        .then((ok) => { if (ok) { State.opLog = []; render(); } });
    };
    el.querySelector('#op-jobs').onclick = () => App.showJobs();
    paint();
    // 自动刷新：任务进度不该要用户手点
    if (timer) clearInterval(timer);
    timer = setInterval(async () => {
      if (!document.body.contains(el)) { clearInterval(timer); timer = null; return; }
      if (document.hidden) return;
      await App.refreshJobs();
      if (State.view === 'ops') paint();
    }, 1500);
  }

  function stateBadge(j) {
    if (j.paused) return '<span class="badge warn">已暂停</span>';
    return {
      done: '<span class="badge ok">已完成</span>',
      error: '<span class="badge err">失败</span>',
      cancelled: '<span class="badge">已取消</span>',
      running: '<span class="badge warn">进行中</span>',
    }[j.state] || `<span class="badge">${U.esc(j.state)}</span>`;
  }

  function paint() {
    const box = el.querySelector('#op-body');
    const log = State.opLog;
    const jobs = (State.jobs || []).slice(0, 15);
    const stale = jobs.filter((j) => j.stale);
    box.innerHTML = `
      ${stale.length ? `<div class="busy-bar" style="border-radius:var(--r);margin-bottom:12px">
        ${U.icon('alert')}<span>有 ${stale.length} 个任务心跳停止（可能已中断）。
        如果确认没在跑，可以取消它释放独占位。</span></div>` : ''}
      <div class="card" style="margin-bottom:14px">
        <h3>任务</h3>
        ${jobs.length ? `<div class="tbl-wrap" style="max-height:320px"><table class="data">
          <thead><tr><th>类型</th><th>状态</th><th>进度</th><th>信息</th>
            <th class="right">耗时</th><th>开始</th><th></th></tr></thead>
          <tbody>${jobs.map((j) => `<tr>
            <td>${U.esc(App.jobKindName(j.kind))}</td>
            <td>${stateBadge(j)}${j.stale ? ' <span class="badge err">心跳停止</span>' : ''}</td>
            <td class="num">${j.done}/${j.total || '-'}${
              j.percent != null ? ` <span class="muted">${j.percent}%</span>` : ''}</td>
            <td class="muted" style="font-size:11.5px">${
              U.esc(j.error || j.message || '')}${
              j.skipped_count ? `　<span class="badge warn">跳过 ${j.skipped_count}</span>` : ''}</td>
            <td class="num">${j.elapsed ? j.elapsed + 's' : '-'}${
              j.eta ? ` <span class="muted">剩${Math.round(j.eta)}s</span>` : ''}</td>
            <td class="muted">${U.fmtAgo(j.started)}</td>
            <td class="num nowrap">
              ${j.can_pause && !j.paused
                ? `<button class="btn icon sm ghost" data-pause="${j.id}" title="暂停">${U.icon('pause')}</button>` : ''}
              ${j.paused
                ? `<button class="btn icon sm" data-resume="${j.id}" title="继续">${U.icon('resume')}</button>` : ''}
              ${j.can_cancel
                ? `<button class="btn icon sm ghost" data-cancel="${j.id}" title="取消">${U.icon('close')}</button>` : ''}
              ${j.recoverable && !j.acked
                ? `<button class="btn icon sm" data-recover="${j.id}"
                     title="接着做完（用跳过同名策略重放，不会覆盖或重复）">${U.icon('rollback')}</button>` : ''}
              ${['done', 'error', 'cancelled'].includes(j.state)
                ? `<button class="btn icon sm ghost" data-dismiss="${j.id}"
                     title="忽略：从记录里删掉（不再出现在这里，重启也不会回来）">${U.icon('trash')}</button>` : ''}
            </td>
          </tr>`).join('')}</tbody></table></div>` : '<div class="muted">暂无任务记录</div>'}
        <div class="muted" style="font-size:11.5px;margin-top:8px">
          同一时刻只允许一个写操作用量在执行 —— 同时跑多个会基于过期的文件状态操作。
        </div>
      </div>
      ${log.length ? log.map((entry, i) => entryHtml(entry, i)).join('') :
        `<div class="empty"><div class="big">${U.icon('history', 'ico')}</div>
          <div>本次会话还没有执行过同步 / 清理操作</div>
          <div class="muted">去「双栏对比」或「查重」页执行一次就会记录在这里</div></div>`}`;

    bindJobActions(box);
    box.querySelectorAll('[data-expand]').forEach((b) => {
      b.onclick = () => { expanded = expanded === b.dataset.expand ? null : b.dataset.expand; paint(); };
    });
    // 重试：只挑"重试有意义"的那些步骤重跑。
    // 文件不存在 / 目标已存在这类不进来 —— 重试也是白搭，
    // 该做的是先扫描或改策略。
    //
    // `entry.retry()` 只返回"要重跑哪几步"，执行在这里做，而且
    // **必须走独占入口**（它会真的再改一次文件）。
    box.querySelectorAll('[data-retry]').forEach((b) => {
      b.onclick = async () => {
        const i = Number(String(b.dataset.retry).slice(2));
        const entry = (State.opLog || [])[i];
        if (!entry || !entry.retry) {
          return UI.toast('这次操作的明细没有保留足够信息，无法自动重试。'
            + '可以回到对应页面重新执行一次。', 'warn', 8000);
        }
        const again = entry.retry();
        if (!again || !again.length) {
          return UI.toast('没有可重试的步骤。', 'warn', 5000);
        }
        // ★ 二次确认。重试本质上就是**再把文件操作执行一遍**，
        //   而用户是在"操作记录"这个看起来只读的页面里点的 ——
        //   不给确认就容易变成"随手一点又跑了一次"。
        const okGo = await UI.confirmHard({
          title: `重试 ${again.length} 个步骤？`,
          phrase: '确认重试',
          okText: '重试',
          details: again.slice(0, 8).map(
            (s) => `${s.op || '?'}  ${s.src_path || s.path || ''}`),
        });
        if (!okGo) return;
        b.disabled = true;
        b.textContent = '重试中…';
        try {
          const r = await App.runExclusive('sync', () => API.execute({
            steps: again, execute: true, confirm: true, policy: 'skip',
            client: (typeof TaskSync !== 'undefined' ? TaskSync.ME : ''),
          }));
          const s = (r && r.summary) || {};
          UI.toast(`已重试 ${again.length} 个步骤：成功 ${s.done || 0}，`
            + `跳过 ${s.skipped || 0}，失败 ${s.error || 0}`,
            s.error ? 'warn' : 'ok', 7000);
          if (r) App.logOp('重试失败步骤', r, again);
          await App.refreshJobs();
        } catch (e) {
          UI.toast('重试失败：' + e.message, 'err', 8000);
        } finally {
          b.disabled = false;
          b.textContent = '重试';
          paint();
        }
      };
    });
  }

  /** 暂停 / 继续 / 取消 / 接着做完 —— 任务中心里的那几个动作。 */
  function bindJobActions(box) {
    box.querySelectorAll('[data-pause]').forEach((b) => {
      b.onclick = async () => {
        await API.pauseJob(b.dataset.pause);
        UI.toast('已暂停；已完成的步骤不会回退，点「继续」接着做', 'info', 5000);
        await App.refreshJobs(); paint();
      };
    });
    box.querySelectorAll('[data-resume]').forEach((b) => {
      b.onclick = async () => {
        await API.resumeJob(b.dataset.resume);
        await App.refreshJobs(); paint();
      };
    });
    box.querySelectorAll('[data-cancel]').forEach((b) => {
      b.onclick = async () => {
        await API.cancelJob(b.dataset.cancel);
        await App.refreshJobs(); paint();
      };
    });
    box.querySelectorAll('[data-dismiss]').forEach((b) => {
      b.onclick = async () => {
        b.disabled = true;
        try {
          const r = await API.dismissJob(b.dataset.dismiss);
          if (!r.ok) UI.toast('这个任务还在运行，不能忽略', 'warn', 4000);
          await App.refreshJobs(); paint();
        } catch (e) {
          b.disabled = false;
          UI.toast('忽略失败：' + e.message, 'err', 6000);
        }
      };
    });
    box.querySelectorAll('[data-recover]').forEach((b) => {
      b.onclick = async () => {
        const ok = await UI.confirm(
          '接着做完这个被中断的任务？<br>'
          + '<span class="muted">会用「跳过同名」策略重放剩下的步骤：'
          + '已经完成的会因源文件不存在被跳过，不会覆盖、不会重复。</span>',
          { title: '接着做完', okText: '开始' });
        if (!ok) return;
        try {
          await App.runExclusive('sync', () => API.recoverJob({
            job: b.dataset.recover,
            client: (typeof TaskSync !== 'undefined' ? TaskSync.ME : ''),
          }));
          UI.toast('恢复完成', 'ok', 6000);
        } catch (e) {
          UI.toast('恢复失败：' + e.message, 'err', 8000);
        }
        await App.refreshJobs(); paint();
      };
    });
  }

  function entryHtml(entry, i) {
    const r = entry.result || {};
    const s = r.summary || {};
    const id = 'op' + i;
    const isOpen = expanded === id;
    const rows = (r.results || []).slice(0, 400);
    // 错误分类：告诉用户"该怎么处理"，而不只是报个"失败"
    const kinds = s.error_kinds || {};
    const labels = s.error_labels || {};
    const retryable = s.retryable_steps || [];
    const kindHtml = Object.keys(kinds).map((k) => `
      <span class="badge ${k === 'conflict' ? 'warn' : 'err'}"
            title="共 ${kinds[k]} 个">${U.esc(labels[k] || k)} ${kinds[k]}</span>`).join('');
    return `<div class="card" style="margin-bottom:12px">
      <div class="row" style="justify-content:space-between">
        <div>
          <b>${U.esc(entry.plan)}</b>
          <span class="muted"> · ${U.fmtAgo(entry.at / 1000)}</span>
        </div>
        <div class="row wrap">
          <span class="badge ok">成功 ${s.done || 0}</span>
          ${s.conflict ? `<span class="badge warn">冲突 ${s.conflict}</span>` : ''}
          ${s.skipped ? `<span class="badge warn">跳过 ${s.skipped}</span>` : ''}
          ${s.blocked ? `<span class="badge err">阻断 ${s.blocked}</span>` : ''}
          ${s.error ? `<span class="badge err">失败 ${s.error}</span>` : ''}
          <span class="badge">搬运 ${U.fmtBytes(s.bytes_moved || 0)}</span>
          ${s.bytes_trashed ? `<span class="badge">清理 ${U.fmtBytes(s.bytes_trashed)}</span>` : ''}
          <button class="btn sm ghost" data-expand="${id}">${isOpen ? '收起' : '查看明细'}</button>
        </div>
      </div>
      ${kindHtml ? `<div class="row wrap" style="margin-top:8px;gap:6px">
        <span class="muted" style="font-size:12px">失败原因：</span>${kindHtml}
        ${retryable.length ? `<div class="grow"></div>
          <button class="btn sm" data-retry="${id}"
            title="只重试那些"重试有意义"的（被占用、空间不足这类）；
                   文件不存在、目标已存在的不在其中">重试这 ${retryable.length} 个</button>` : ''}
      </div>` : ''}
      ${isOpen ? `<div class="tbl-wrap" style="max-height:340px;margin-top:10px">
        <table class="data"><thead><tr><th>操作</th><th>对象</th><th>结果</th><th class="right">耗时</th></tr></thead>
        <tbody>${rows.map((x) => `<tr>
          <td>${U.esc(x.op)}</td>
          <td class="mono ell" title="${U.esc(x.dst || x.src || '')}">${U.esc(x.dst || x.src || '')}</td>
          <td>${x.state === 'done' ? '<span class="badge ok">完成</span>'
              : `<span class="badge ${x.state === 'conflict' ? 'warn' : 'err'}">${U.esc(x.state)}</span>`}
              ${x.error_kind ? `<span class="tag">${U.esc(labels[x.error_kind] || x.error_kind)}</span>` : ''}
              <span class="muted">${U.esc(x.detail || '')}${x.trash ? ' → ' + U.esc(x.trash) : ''}</span></td>
          <td class="num">${x.seconds || '-'}</td>
        </tr>`).join('')}</tbody></table></div>
        <div class="muted" style="font-size:11.5px;margin-top:6px">
          提示：删除的文件都在各数据源根目录的
          <span class="mono">.music-sync-trash/日期/</span> 下，可直接恢复。
        </div>` : ''}
    </div>`;
  }

  App.registerView('ops', { mount, render });
})();
