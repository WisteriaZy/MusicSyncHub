/* ============================================================
   视图：设置（算法参数 + 系统状态）
   ============================================================ */
(() => {
  let el = null;
  let settings = null;

  const FIELDS = [
    { k: 'scan_workers', label: '扫描并发线程', t: 'number', hint: '本地磁盘建议 8~16，机械盘可从 4 起试' },
    { k: 'hash_workers', label: '哈希并发线程', t: 'number', hint: '计算校验和时的并发数' },
    { k: 'hash_size_limit_mb', label: '哈希体积上限 (MB)', t: 'number', hint: '超过这个体积只算抽样哈希，避免跑满流量' },
    { k: 'quick_hash_probe_kb', label: '抽样哈希长度 (KB)', t: 'number', hint: '头尾各取这么多字节参与哈希' },
    { k: 'duration_tolerance_sec', label: '时长容差 (秒)', t: 'number', step: '0.1', hint: '两首歌时长差在此范围内视为同一录音' },
    { k: 'name_similarity', label: '模糊匹配阈值', t: 'number', step: '0.01', hint: '0.86 较稳；调低会更激进地配对' },
    { k: 'max_compare_tracks', label: '单次对比上限 (首)', t: 'number', hint: '每侧最多取多少条参与对比/查重，超出会截断并提示；超大库建议按目录分批' },
    { k: 'trash_dir', label: '回收站目录', t: 'text', hint: '留空 = 各数据源根目录下的 .music-sync-trash/日期/' },
    { k: 'default_keep_policy', label: '默认保留策略', t: 'select', options: [['best_quality', '音质最好的'], ['largest', '体积最大的'], ['oldest', '最早的'], ['newest', '最新的']] },
  ];

  /** 中转站 / 上传暂存区：位置和容量都可自定义。 */
  const AREA_FIELDS = [
    { k: 'station_dir', label: '中转站目录', t: 'text',
      hint: '留空 = 程序目录下的 station/。放"我还没决定放哪"的文件，可长期存放' },
    { k: 'station_max_gb', label: '中转站容量上限 (GB)', t: 'number', step: '0.5',
      hint: '0 = 不限制。满了会拒绝继续放入，避免把磁盘写满' },
    { k: 'staging_dir', label: '上传暂存区目录', t: 'text',
      hint: '留空 = 应用数据目录下的 uploads/。只放"这次上传还没走完流程"的文件' },
    { k: 'staging_max_gb', label: '暂存区容量上限 (GB)', t: 'number', step: '0.5',
      hint: '0 = 不限制。它只是过路区，建议比中转站小；满了会拒绝新上传' },
    { k: 'staging_stale_hours', label: '批次滞留提醒 (小时)', t: 'number',
      hint: '批次放这么久还没处理就在「中转站」页标出来；0 = 不提醒' },
  ];

  const ALL_FIELDS = FIELDS.concat(AREA_FIELDS);

  function mount(root) { el = root; }

  async function render() {
    el.innerHTML = '<div class="muted pad">加载设置…</div>';
    let sys = null;
    try { sys = await API.system(); } catch (e) { /* ignore */ }
    try {
      const r = await API.settings();
      settings = r.settings;
    } catch (e) { el.innerHTML = `<div class="pad" style="color:#ff9daa">${U.esc(e.message)}</div>`; return; }

    el.innerHTML = `
      <div class="toolbar">
        <b>设置</b>
        <span class="muted mono">${U.esc((sys && sys.data_dir) || '')}/config.toml</span>
        <div class="grow"></div>
        <button class="btn ghost sm" id="st-reload">从文件重载配置</button>
        <button class="btn primary sm" id="st-save">保存设置</button>
      </div>
      <div class="grid c2">
        <div class="card">
          <h3>算法与性能</h3>
          ${FIELDS.map(fieldHtml).join('')}
          <label class="check" style="margin-top:6px">
            <input type="checkbox" id="st-ignored-show"> 查看忽略的文件名
          </label>
          <div id="st-ignored" class="hidden field" style="margin-top:8px">
            <label>忽略名单（逗号分隔）</label>
            <input type="text" id="st-ignored-input" value="${U.esc((settings.ignored_names || []).join(', '))}">
          </div>
        </div>
        <div class="card">
          <h3>中转站与上传暂存区</h3>
          <div class="muted" style="font-size:12px;margin-bottom:10px;line-height:1.8">
            <b>中转站</b>：我还没决定放哪的文件，可长期存放，也能把库里的文件临时挪进来。<br>
            <b>上传暂存区</b>：只放"这次上传还没走完入库流程"的文件，属于过路区。
          </div>
          ${AREA_FIELDS.map(fieldHtml).join('')}
          <div id="st-areas-usage" class="muted" style="font-size:12px;margin-top:10px"></div>
        </div>
        <div class="card">
          <h3>界面偏好记忆</h3>
          <div class="muted" style="font-size:12px;margin-bottom:8px;line-height:1.8">
            这些是"你上次怎么看的"，会记在浏览器里，下次打开自动恢复：
          </div>
          <div id="st-prefs" class="pref-list"></div>
          <button class="btn sm ghost" id="st-prefs-reset" style="margin-top:10px">
            ${U.icon('refresh')}<span>清除全部偏好（回到默认）</span></button>
        </div>
      </div>
      <div class="grid c2">
        <div class="card">
          <h3>运行环境</h3>
          <div class="kv">
            <div class="k">版本</div><div>${U.esc(sys ? sys.app + ' ' + sys.version : '-')}</div>
            <div class="k">数据目录</div><div class="mono" style="word-break:break-all">${U.esc(sys ? sys.data_dir : '-')}</div>
            <div class="k">索引数据库</div><div class="mono" style="word-break:break-all">${U.esc(sys ? sys.db : '-')}</div>
            <div class="k">数据库大小</div><div>${U.fmtBytes(sys ? sys.db_size : 0)}</div>
            <div class="k">数据源</div><div>${sys ? sys.source_count : 0} 个</div>
            <div class="k">元数据引擎</div><div>${sys && sys.capabilities.mutagen ? '<span class="badge ok">mutagen 可用</span>' : '<span class="badge err">缺少 mutagen</span>'}</div>
            <div class="k">图片处理</div><div>${sys && sys.capabilities.pillow ? '<span class="badge ok">Pillow 可用</span>' : '<span class="badge warn">缺少 Pillow（封面不缩放）</span>'}</div>
          </div>
          <hr class="sep">
          <h3>后台日志</h3>
          <div class="muted" style="margin-bottom:6px">
            出问题时先看这里。日志同时写到
            <span class="mono" style="word-break:break-all" id="log-path">-</span>
          </div>
          <div class="row wrap" style="margin-bottom:8px">
            <button class="btn sm" id="log-refresh">刷新</button>
            <select id="log-level" style="width:auto">
              <option value="">全部级别</option>
              <option value="ERROR">只看 ERROR</option>
              <option value="WARNING">只看 WARNING</option>
              <option value="INFO">只看 INFO</option>
            </select>
            <button class="btn sm ghost" id="log-clear">清空（便于复现）</button>
            <button class="btn sm ghost" id="log-copy">复制全部</button>
            <span class="muted" id="log-count"></span>
          </div>
          <pre class="log-box mono" id="log-box">（还没有日志）</pre>
          <hr class="sep">
          <h3>索引统计</h3>
          <div class="kv">
            ${Object.entries((sys && sys.counts) || {}).map(([k, v]) => {
              const s = App.sourceById(k);
              return `<div class="k">${U.esc(s ? s.name : k)}</div><div>${v} 首</div>`;
            }).join('') || '<div class="k">空</div><div>-</div>'}
          </div>
          <hr class="sep">
          <h3>使用提示</h3>
          <ul class="muted" style="margin:0;padding-left:18px;line-height:1.9;font-size:12px">
            <li>写入永远是「先临时文件、后改名」，断电/断网不会留下半截文件。</li>
            <li>删除永远先进回收站；只有勾选「永久删除」才会真的删掉。</li>
            <li>对源库建议勾选「只读」，避免误操作反向覆盖。</li>
            <li>两侧元数据不一致没关系：匹配靠文件名 + 时长 + 内容哈希。</li>
          </ul>
        </div>
      </div>`;

    el.querySelector('#st-save').onclick = save;
    el.querySelector('#st-reload').onclick = async () => { await API.reload(); render(); };

    // ---------------------------------------------------------- 后台日志
    // 出问题时先看这个：不用去翻控制台（窗口一关就没了）。
    const logBox = el.querySelector('#log-box');
    const logCount = el.querySelector('#log-count');
    const logLevel = el.querySelector('#log-level');
    const loadLogs = async () => {
      if (!logBox) return;
      const lvl = (logLevel && logLevel.value) || '';
      logBox.textContent = '正在读取…';
      try {
        const r = await API.logs({ limit: 200, full: true, lines: 300, level: lvl });
        const p = el.querySelector('#log-path');
        if (p && r.path) p.textContent = r.path;
        // 文件里的行更全（含启动日志），优先显示；没有就退回内存条目
        let lines = Array.isArray(r.lines) ? r.lines.slice() : [];
        if (!lines.length && Array.isArray(r.memory)) {
          lines = r.memory.map((x) => {
            const ts = new Date((x.t || 0) * 1000).toLocaleString();
            return `${ts} ${x.level || ''} [${x.name || ''}] ${x.msg || ''}`;
          });
        }
        logBox.textContent = lines.length ? lines.join('\n') : '（还没有日志）';
        if (logCount) {
          logCount.textContent = `共 ${lines.length} 行`
            + (lvl ? `（已过滤 ${lvl}）` : '');
        }
        logBox.scrollTop = logBox.scrollHeight;
      } catch (e) {
        logBox.textContent = '读取日志失败：' + (e && e.message);
      }
    };
    el.querySelector('#log-refresh').onclick = loadLogs;
    if (logLevel) logLevel.onchange = loadLogs;
    el.querySelector('#log-clear').onclick = async () => {
      try {
        await API.clearLogs();
        UI.toast('已清空内存日志缓冲（日志文件保留）。'
          + '现在去复现问题，之后看到的就全是这次操作的记录。', 'ok', 7000);
        loadLogs();
      } catch (e) {
        UI.toast('清空失败：' + U.esc(e.message), 'err');
      }
    };
    el.querySelector('#log-copy').onclick = async () => {
      const txt = (logBox && logBox.textContent) || '';
      try {
        await navigator.clipboard.writeText(txt);
        UI.toast(`已复制 ${txt.split('\n').length} 行日志到剪贴板`, 'ok');
      } catch (e) {
        UI.toast('浏览器不允许自动复制，请手动选中日志内容', 'warn', 6000);
      }
    };
    loadLogs();

    const show = el.querySelector('#st-ignored-show');
    show.onchange = () => el.querySelector('#st-ignored').classList.toggle('hidden', !show.checked);
    // 显示两个区域的实际占用，改完上限能立刻看到效果
    try {
      const [st, up] = await Promise.all([API.station(), API.uploadBatches()]);
      const line = (name, u) => `${name}：已用 ${U.fmtBytes(u.bytes)}`
        + (u.cap_bytes ? ` / ${(u.cap_bytes / 1024 ** 3).toFixed(1)} GB（${u.percent}%）`
                       : '（未设上限）')
        + `　<span class="mono">${U.esc(u.dir)}</span>`;
      const box = el.querySelector('#st-areas-usage');
      if (box) {
        box.innerHTML = line('中转站', st) + '<br>' + line('上传暂存区', up);
      }
    } catch (e) { /* 显示不出来不影响改设置 */ }

    // 偏好记忆总览：让"记住了什么"变成看得见的东西。
    // 以前哪些记住、哪些没记住只能靠试，用户根本不知道。
    try {
      const box = el.querySelector('#st-prefs');
      const vals = Prefs.all();
      const fmt = (v) => {
        if (v === undefined || v === null || v === '') return '<span class="muted">未设置</span>';
        if (typeof v === 'object') return U.esc(JSON.stringify(v).slice(0, 90));
        return U.esc(String(v));
      };
      if (box) {
        box.innerHTML = Object.keys(Prefs.KNOWN).map((k) => `
          <div class="pref-row">
            <span class="pref-k">${U.esc(Prefs.KNOWN[k].d)}</span>
            <span class="pref-v mono">${fmt(vals[k])}</span>
          </div>`).join('');
      }
      const rb = el.querySelector('#st-prefs-reset');
      if (rb) rb.onclick = async () => {
        const ok = await UI.confirm(
          '清除全部界面偏好？<br><span class="muted">只会重置"看的方式"'
          + '（筛选、排序、列、音量…），不会动任何音乐文件或设置项。</span>',
          { title: '清除偏好', okText: '清除', danger: true });
        if (!ok) return;
        const n = Prefs.clear();
        UI.toast(`已清除 ${n} 项偏好，界面将回到默认`, 'ok', 5000);
        render();
      };
    } catch (e) { /* ignore */ }
  }

  /** 数值字段的输入范围（和后端 validate_setting 的限值保持一致）。 */
  const NUM_BOUNDS = {
    scan_workers: [1, 64], hash_workers: [1, 64],
    hash_size_limit_mb: [0, 1048576], quick_hash_probe_kb: [1, 65536],
    duration_tolerance_sec: [0, 600], name_similarity: [0, 1],
    cover_cache_size: [0, 200000], max_compare_tracks: [10, 2000000],
    station_max_gb: [0, 1048576], staging_max_gb: [0, 1048576],
    staging_stale_hours: [0, 87600],
  };

  function fieldHtml(f) {
    const v = settings[f.k];
    if (f.t === 'select') {
      return `<div class="field"><label>${f.label}</label>
        <select data-k="${f.k}">${f.options.map(([val, lab]) =>
          `<option value="${val}" ${String(v) === val ? 'selected' : ''}>${lab}</option>`).join('')}</select>
        <div class="hint">${f.hint || ''}</div></div>`;
    }
    // 数值框带上 min/max：前端先拦一道，用户不会填完才被告知越界。
    // 后端仍然会校验（前端能被绕过，而且配置文件也可能被手改）。
    const b = f.t === 'number' ? (NUM_BOUNDS[f.k] || null) : null;
    const bounds = b ? ` min="${b[0]}" max="${b[1]}"` : '';
    const hint = b ? `${f.hint || ''}　（${b[0]} ~ ${b[1]}）` : (f.hint || '');
    return `<div class="field"><label>${f.label}</label>
      <input type="${f.t}" data-k="${f.k}" value="${U.esc(v)}"
        ${f.step ? `step="${f.step}"` : ''}${bounds}>
      <div class="hint">${hint}</div></div>`;
  }

  async function save() {
    const payload = {};
    const bad = [];
    el.querySelectorAll('[data-k]').forEach((n) => {
      const k = n.dataset.k;
      const f = ALL_FIELDS.find((x) => x.k === k);
      if (f && f.t === 'number') {
        const raw = String(n.value).trim();
        const num = Number(raw);
        const b = NUM_BOUNDS[k];
        // 空串按 0 处理（"不限额"就是这么填的），但真填了垃圾要拦住
        if (raw !== '' && !Number.isFinite(num)) {
          bad.push(`${f.label}：需要填数字`);
          return;
        }
        if (b && raw !== '' && (num < b[0] || num > b[1])) {
          bad.push(`${f.label}：要在 ${b[0]} ~ ${b[1]} 之间`);
          return;
        }
        payload[k] = raw === '' ? 0 : num;
        return;
      }
      payload[k] = n.value;
    });
    if (bad.length) {
      return UI.confirm(`这些填写有问题：<br><br>${bad.map(U.esc).join('<br>')}`,
        { title: '请先修正', okText: '知道了', danger: true });
    }
    const ign = el.querySelector('#st-ignored-input');
    if (ign) payload.ignored_names = ign.value.split(',').map((s) => s.trim()).filter(Boolean);
    try {
      const r = await API.saveSettings(payload);
      settings = r.settings;
      if (r.errors && Object.keys(r.errors).length) {
        // 部分没存上：必须说出来，否则用户以为改好了，下次启动才发现
        UI.modal({
          title: '部分设置未保存',
          size: 'sm',
          body: `<div style="line-height:1.7">下面这些值不合法，已保持原样：</div>
            <div class="hard-list" style="margin-top:10px">
              ${Object.entries(r.errors).map(([k, v]) =>
                `<div><b class="mono">${U.esc(k)}</b>：${U.esc(v)}</div>`).join('')}
            </div>`,
          footer: '<button class="btn primary">知道了</button>',
        });
      } else {
        UI.toast('设置已保存', 'ok');
      }
      render();
    } catch (e) {
      // 后端会带上"哪个字段为什么不合法"，直接显示给用户，
      // 别只说一句"保存失败"
      UI.toast('保存失败：' + U.esc(e.message), 'err', 9000);
    }
  }

  App.registerView('settings', { mount, render });
})();
