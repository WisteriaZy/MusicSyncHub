/* ============================================================
   API 封装 + 通用工具
   ============================================================ */
const API = (() => {
  async function req(path, { method = 'GET', body, raw = false } = {}) {
    const opt = { method, headers: {} };
    if (body !== undefined) {
      opt.headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(body);
    }
    const res = await fetch(path, opt);
    if (raw) return res;
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = { raw: text }; }
    if (!res.ok) {
      const msg = (data && (data.detail || data.message)) || res.statusText || ('HTTP ' + res.status);
      const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
      err.status = res.status;
      err.payload = data;
      throw err;
    }
    return data;
  }

  /**
   * 上传：body 是 FormData，**不能**设 Content-Type ——
   * 浏览器要自己带上 multipart 的 boundary，手动设了后端就解析不出来。
   */
  /**
   * 带**上传进度**的请求。
   *
   * 为什么不用 fetch：fetch 没有上传进度事件。用户一次拖几十个文件进来，
   * 界面要是一点反应都没有，就会以为卡死然后强杀进程 ——
   * 那才是最糟的结果（还会留下半截的暂存批次）。
   * XMLHttpRequest 的 `upload.onprogress` 能给出已发字节数。
   */
  function uploadReqProgress(path, fd, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', path);
      if (xhr.upload && onProgress) {
        xhr.upload.onprogress = (e) => {
          if (e.lengthComputable) {
            onProgress({ sent: e.loaded, total: e.total,
                         percent: Math.round(100 * e.loaded / e.total) });
          }
        };
      }
      xhr.onload = () => {
        let data = null;
        try { data = xhr.responseText ? JSON.parse(xhr.responseText) : null; }
        catch (e) { data = { raw: xhr.responseText }; }
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else {
          const msg = (data && (data.detail || data.message))
            || xhr.statusText || ('HTTP ' + xhr.status);
          reject(new Error(msg));
        }
      };
      xhr.onerror = () => reject(new Error('网络中断，上传失败'));
      xhr.ontimeout = () => reject(new Error('上传超时'));
      xhr.onabort = () => reject(new Error('上传已取消'));
      // 大文件慢盘：不要设短超时（默认 0 = 不限，靠 onerror 兜底）
      xhr.send(fd);
    });
  }

  async function uploadReq(path, fd) {
    const res = await fetch(path, { method: 'POST', body: fd });
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = { raw: text }; }
    if (!res.ok) {
      const msg = (data && (data.detail || data.message)) || res.statusText
        || ('HTTP ' + res.status);
      const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
      err.status = res.status;
      err.payload = data;
      throw err;
    }
    return data;
  }

  return {
    req,
    uploadReq,
    system: () => req('/api/system'),
    sourceTypes: () => req('/api/source-types'),
    sources: () => req('/api/sources'),
    addSource: (b) => req('/api/sources', { method: 'POST', body: b }),
    updateSource: (id, b) => req(`/api/sources/${id}`, { method: 'PUT', body: b }),
    deleteSource: (id, purge) => req(`/api/sources/${id}?purge=${purge ? 'true' : 'false'}`, { method: 'DELETE' }),
    testSource: (id) => req(`/api/sources/${id}/test`, { method: 'POST' }),
    /* 外置存储：列本机磁盘（不测速）/ 探测某个目录落在什么盘上（会测速） */
    drives: () => req('/api/drives'),
    deviceProbe: (path) => req('/api/device/probe?path=' + encodeURIComponent(path)),
    /* 跨库传输：execute=false 只做预检（两边权限 / 目标落点 / 同名冲突） */
    fileTransfer: (b) => req('/api/files/transfer', { method: 'POST', body: b }),
    /* 批量问"这些文件还在不在"（候选区用来标出过期项） */
    filesExists: (b) => req('/api/files/exists', { method: 'POST', body: b }),
    /* 后台日志：默认读内存里的最近若干条；full=true 读日志文件尾部 */
    logs: (opt) => {
      const o = opt || {};
      const q = [];
      if (o.limit) q.push('limit=' + o.limit);
      if (o.level) q.push('level=' + encodeURIComponent(o.level));
      if (o.full) q.push('full=true');
      if (o.lines) q.push('lines=' + o.lines);
      return req('/api/logs' + (q.length ? '?' + q.join('&') : ''));
    },
    clearLogs: () => req('/api/logs/clear', { method: 'POST', body: {} }),
    browse: (id, path) => req(`/api/sources/${id}/browse`, { method: 'POST', body: { path: path || '' } }),
    scan: (id, opts) => req(`/api/scan/${id}`, { method: 'POST', body: opts || {} }),
    jobs: () => req('/api/jobs'),
    job: (id) => req(`/api/jobs/${id}`),
    cancelJob: (id) => req(`/api/jobs/${id}/cancel`, { method: 'POST' }),
    /** 暂停 / 继续：已完成的步骤不回退，恢复后接着做。 */
    pauseJob: (id) => req(`/api/jobs/${id}/pause`, { method: 'POST' }),
    resumeJob: (id) => req(`/api/jobs/${id}/resume`, { method: 'POST' }),
    /** 上次运行没做完的任务（程序异常退出留下的）。 */
    unfinished: () => req('/api/unfinished'),
    /** 接着做完一个被中断的任务（用 skip 策略重放，不会覆盖/重复）。 */
    recoverJob: (b) => req('/api/unfinished/recover', { method: 'POST', body: b }),
    /** 当前有没有独占任务在跑（多页面共用，用来禁用冲突操作）。 */
    busy: () => req('/api/busy'),
    /** 任务结果已读：这是"一次只有一个流程"的收尾点。 */
    jobAck: (id) => req(`/api/jobs/${id}/ack`, { method: 'POST' }),
    dismissJob: (id) => req(`/api/jobs/${id}/dismiss`, { method: 'POST' }),
    clearFinishedJobs: () => req('/api/jobs/clear-finished', { method: 'POST' }),
    library: (id, q) => req(`/api/library/${id}?` + new URLSearchParams(q || {})),
    folders: (id) => req(`/api/folders/${id}`),
    track: (id) => req(`/api/track/${id}`),
    refreshTrack: (id) => req(`/api/track/${id}/refresh`, { method: 'POST' }),
    compare: (b) => req('/api/compare', { method: 'POST', body: b }),
    plan: (b) => req('/api/plan', { method: 'POST', body: b }),
    /**
     * 执行方案。
     *
     * ★ 统一在这里注入 `client`（本页面的 id）。
     *   后端拿它当任务的 owner，跨页面靠 owner 判断"这个任务是不是我发起的"。
     *   之前每个调用点都要自己记得传 —— 结果全都没传，owner 为空串，
     *   于是"别人在跑"恒为真、本页按钮被自己置灰。
     *   放在这一层就不可能漏。
     */
    execute: (b) => req('/api/execute', {
      method: 'POST',
      body: (b && typeof b === 'object' && !b.client
        && typeof TaskSync !== 'undefined')
        ? { ...b, client: TaskSync.ME } : b,
    }),
    duplicates: (b) => req('/api/duplicates', { method: 'POST', body: b }),
    keepAdvice: (b) => req('/api/duplicates/keep', { method: 'POST', body: b }),
    settings: () => req('/api/settings'),
    saveSettings: (b) => req('/api/settings', { method: 'PUT', body: b }),
    reload: () => req('/api/reload', { method: 'POST' }),
    /* ---- 文件浏览（资源管理器式）---- */
    fileFolders: (sid, path) => req('/api/files/folders?sid=' + encodeURIComponent(sid)
      + (path ? '&path=' + encodeURIComponent(path) : '')),
    fileList: (sid, path, sort, order) => req('/api/files/list?sid=' + encodeURIComponent(sid)
      + (path ? '&path=' + encodeURIComponent(path) : '')
      + (sort ? '&sort=' + encodeURIComponent(sort) : '')
      + (order ? '&order=' + encodeURIComponent(order) : '')),
    fileFacets: (sid, path, field, limit) => req('/api/files/facets?sid=' + encodeURIComponent(sid)
      + (path ? '&path=' + encodeURIComponent(path) : '')
      + '&field=' + encodeURIComponent(field || 'artist')
      + '&limit=' + encodeURIComponent(limit || 60)),
    fileSelect: (b) => req('/api/files/select', { method: 'POST', body: b }),
    fileMove: (b) => req('/api/files/move', { method: 'POST', body: b }),
    fileSearch: (sid, q, limit) => req('/api/files/search?sid=' + encodeURIComponent(sid)
      + '&q=' + encodeURIComponent(q || '') + '&limit=' + encodeURIComponent(limit || 60)),
    /* ---- 文件夹级操作 ---- */
    folderInfo: (sid, path) => req('/api/folders/info?sid=' + encodeURIComponent(sid)
      + '&path=' + encodeURIComponent(path || '')),
    folderCreate: (b) => req('/api/folders/create', { method: 'POST', body: b }),
    folderRename: (b) => req('/api/folders/rename', { method: 'POST', body: b }),
    folderDelete: (b) => req('/api/folders/delete', { method: 'POST', body: b }),
    folderFiles: (sid, path) => req('/api/folders/files?sid=' + encodeURIComponent(sid)
      + '&path=' + encodeURIComponent(path || '')),
    /** 一键清空空文件夹：先扫、再删（取代了原来的"解散文件夹"）。 */
    folderEmptyScan: (sid, path) => req('/api/folders/empty-scan?sid='
      + encodeURIComponent(sid) + '&path=' + encodeURIComponent(path || '')),
    folderEmptyRemove: (b) => req('/api/folders/empty-remove', { method: 'POST', body: b }),
    /* ---- 回收站 ---- */
    trash: (sid) => req('/api/trash?sid=' + encodeURIComponent(sid || '')),
    trashRestore: (b) => req('/api/trash/restore', { method: 'POST', body: b }),
    trashPurge: (b) => req('/api/trash/purge', { method: 'POST', body: b }),
    /** 中转站：暂存未分配的文件，带容量上限。 */
    station: () => req('/api/station'),
    /** 很轻的"数据变了没有"信号，供页面自动刷新用。 */
    state: (sid) => req('/api/state' + (sid ? '?sid=' + encodeURIComponent(sid) : '')),
    stationCheck: (addMb) => req('/api/station/check?add_mb=' + encodeURIComponent(addMb || 0)),
    stationMoveIn: (b) => req('/api/station/move-in', { method: 'POST', body: b }),
    stationMoveOut: (b) => req('/api/station/move-out', { method: 'POST', body: b }),
    stationRemove: (b) => req('/api/station/remove', { method: 'POST', body: b }),
    stationSuggest: (b) => req('/api/station/suggest', { method: 'POST', body: b }),
    stationRestore: (b) => req('/api/station/restore', { method: 'POST', body: b }),
    /** 列出若干目录（含子目录）下的全部已索引文件（目录树多选联动用）。 */
    filesUnder: (b) => req('/api/files/under', { method: 'POST', body: b }),
    /** 只为**指定的文件**推算去向（候选区/入库用，规则同智能整理）。 */
    organizePlanPaths: (b) => req('/api/organize/plan-paths', { method: 'POST', body: b }),
    mappings: (sid) => req('/api/mappings?sid=' + encodeURIComponent(sid || '')),
    mappingsAll: () => req('/api/mappings'),
    mappingsSave: (b) => req('/api/mappings', { method: 'PUT', body: b }),
    mappingOne: (b) => req('/api/mappings/one', { method: 'POST', body: b }),
    /* ---- 模糊搜索 / 集合 / 智能整理 ---- */
    search: (sid, q, opt = {}) => req('/api/search?sid=' + encodeURIComponent(sid)
      + '&q=' + encodeURIComponent(q || '')
      + '&kind=' + encodeURIComponent(opt.kind || 'track')
      + '&scope=' + encodeURIComponent(opt.scope || 'all')
      + (opt.path ? '&path=' + encodeURIComponent(opt.path) : '')
      + '&limit=' + encodeURIComponent(opt.limit || 60)),
    collectPreview: (b) => req('/api/collect/preview', { method: 'POST', body: b }),
    collectApply: (b) => req('/api/collect/apply', { method: 'POST', body: b }),
    organizePreview: (sid, limit) => req('/api/organize/preview?sid='
      + encodeURIComponent(sid) + '&limit=' + encodeURIComponent(limit || 60)),
    organizeAnalyze: (sid, opt = {}) => req('/api/organize/analyze?sid='
      + encodeURIComponent(sid)
      + (opt.path ? '&path=' + encodeURIComponent(opt.path) : '')
      + (opt.q ? '&q=' + encodeURIComponent(opt.q) : '')
      + '&limit=' + encodeURIComponent(opt.limit || 20)),
    organizePlan: (b) => req('/api/organize/plan', { method: 'POST', body: b }),
    organizeApply: (b) => req('/api/organize/apply', { method: 'POST', body: b }),
    organizeFolder: (b) => req('/api/organize/folder', { method: 'POST', body: b }),
    organizeAlbumPlan: (b) => req('/api/organize/album-plan', { method: 'POST', body: b }),
    organizeAlbumApply: (b) => req('/api/organize/album-apply', { method: 'POST', body: b }),
    /* ---- 上传入库 ---- */
    uploadFiles: (fd) => uploadReq('/api/upload', fd),
    /** 带进度回调的上传（onProgress 收到 {sent,total,percent}）。 */
    uploadFilesProgress: (fd, onProgress) => uploadReqProgress('/api/upload', fd, onProgress),
    uploadBatches: () => req('/api/upload/batches'),
    /** 暂存区管理：转进中转站 / 清理滞留批次与孤儿文件。 */
    uploadBatchToStation: (b) => req('/api/upload/batch-to-station', { method: 'POST', body: b }),
    uploadCleanup: (b) => req('/api/upload/cleanup', { method: 'POST', body: b }),
    uploadInspect: (batch, sid, minGroup, custom) => req('/api/upload/inspect?batch='
      + encodeURIComponent(batch) + '&source=' + encodeURIComponent(sid || '')
      + '&min_group=' + encodeURIComponent(minGroup || 5)
      + '&custom=' + encodeURIComponent(custom || '')),
    uploadCommit: (b) => req('/api/upload/commit', { method: 'POST', body: b }),
    /** 启动入库查重（后台任务：要读文件内容算哈希，可能较慢）。 */
    uploadDedupStart: (batch, sid) => req('/api/upload/dedup', {
      method: 'POST', body: { batch, source: sid || '' },
    }),
    /** 查重任务进度。 */
    uploadDedupStatus: (job) => req('/api/upload/dedup?job=' + encodeURIComponent(job)),
    /** 取某批次上次查重的结果。 */
    uploadDups: (batch) => req('/api/upload/dups?batch=' + encodeURIComponent(batch)),
    uploadDelete: (batch) => req('/api/upload/batch?batch=' + encodeURIComponent(batch),
      { method: 'DELETE' }),
    /* ---- 元数据（标签/封面）---- */
    tagsRead: (sid, path) => req('/api/tags/read?sid=' + encodeURIComponent(sid)
      + '&path=' + encodeURIComponent(path)),
    tagsPreview: (b) => req('/api/tags/preview', { method: 'POST', body: b }),
    tagsWrite: (b) => req('/api/tags/write', { method: 'POST', body: b }),
    tagsBackups: (limit) => req('/api/tags/backups?limit=' + encodeURIComponent(limit || 100)),
    tagsRestore: (b) => req('/api/tags/restore', { method: 'POST', body: b }),
    sourcesStatus: () => req('/api/sources/status'),
  };
})();

/* ------------------------------------------------------------ 工具 */
const U = {
  fmtBytes(n) {
    if (!n && n !== 0) return '-';
    if (n === 0) return '0 B';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let v = n, i = 0;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v.toFixed(0) : v.toFixed(v < 10 ? 2 : 1)) + ' ' + u[i];
  },
  fmtDur(s) {
    if (!s && s !== 0) return '-';
    s = Math.round(s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
    const p = (x) => String(x).padStart(2, '0');
    return h ? `${h}:${p(m)}:${p(ss)}` : `${m}:${p(ss)}`;
  },
  fmtDurLong(s) {
    if (!s) return '-';
    const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
    return h ? `${h} 小时 ${m} 分` : `${m} 分钟`;
  },
  fmtBitrate(b) { return b ? Math.round(b / 1000) + ' kbps' : '-'; },
  fmtRate(sr) { return sr ? (sr / 1000).toFixed(sr % 1000 ? 1 : 0) + ' kHz' : '-'; },
  fmtDate(ts) {
    if (!ts) return '-';
    const d = new Date(ts * 1000);
    return d.toLocaleDateString() + ' ' + d.toLocaleTimeString().slice(0, 5);
  },
  fmtAgo(ts) {
    if (!ts) return '从未';
    const s = Date.now() / 1000 - ts;
    if (s < 60) return '刚刚';
    if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
    if (s < 86400) return Math.floor(s / 3600) + ' 小时前';
    return Math.floor(s / 86400) + ' 天前';
  },
  esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  },
  hl(text, q) {
    const t = U.esc(text || '');
    if (!q) return t;
    try {
      const re = new RegExp('(' + q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
      return t.replace(re, '<span class="hl">$1</span>');
    } catch (e) { return t; }
  },
  base(p) { const s = String(p || '').replace(/\\/g, '/'); return s.slice(s.lastIndexOf('/') + 1); },
  parent(p) { const s = String(p || '').replace(/\\/g, '/'); const i = s.lastIndexOf('/'); return i < 0 ? '' : s.slice(0, i); },
  qualityLabel(t) {
    const parts = [];
    if (t.is_lossless) parts.push('无损');
    if (t.codec) parts.push(String(t.codec).toUpperCase());
    if (t.bitrate) parts.push(Math.round(t.bitrate / 1000) + 'k');
    if (t.sample_rate) parts.push((t.sample_rate / 1000).toFixed(t.sample_rate % 1000 ? 1 : 0) + 'kHz');
    if (t.bits_per_sample) parts.push(t.bits_per_sample + 'bit');
    return parts.join(' · ');
  },
  /* 图标：U.icon('play', 'ico lg')
     1903 的图标是细线描边（见 app.css 的 .ico），只有媒体控制类维持实心，
     这里自动给它们加上 fill。 */
  icon(name, cls = 'ico') {
    const solid = { play: 1, pause: 1, prev: 1, next: 1 };
    const c = solid[name] && !cls.includes('fill') ? cls + ' fill' : cls;
    return `<svg class="${c}"><use href="#i-${name}"></use></svg>`;
  },
  /* 数据源类型 -> 图标 */
  typeIcon(type) {
    return { local: 'disk' }[type] || 'db';
  },
  /* 由 id 稳定地映射到 Win10 磁贴色板 */
  tileColor(seed) {
    const palette = ['c-blue', 'c-teal', 'c-purple', 'c-magenta', 'c-orange',
                     'c-green', 'c-olive', 'c-steel', 'c-plum', 'c-gray'];
    let h = 0;
    for (const ch of String(seed || '')) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    return palette[h % palette.length];
  },
  debounce(fn, ms = 260) {
    let t = null;
    return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  },
  confText(c) {
    return { identical: '完全一致', high: '高度可信', medium: '中等可信', low: '需人工确认' }[c] || c;
  },
};
