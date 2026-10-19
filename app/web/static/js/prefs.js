/* ============================================================
   偏好记忆（统一入口）

   为什么单独做一层：以前各处零散地读写 localStorage，结果
   **有的记住了、有的没记** —— 用户每次打开都要重设一遍，
   又说不清到底哪些该记住。这里统一成一套：

     · 读取失败 / 隐私模式 / 数据损坏 → 一律回退到默认值，绝不报错；
     · 每个键带版本号，将来结构变了可以丢弃旧值而不是崩掉；
     · `Prefs.all()` / `Prefs.clear()` 给"设置"页做总览与重置。

   只记**界面偏好**（看了什么、怎么排、显示哪些列），
   不记任何业务数据 —— 那些的真相在后端。
   ============================================================ */
const Prefs = (() => {
  const NS = 'msh.';
  const listeners = [];

  // 所有会被记住的键，附说明（设置页展示用）
  //
  // ★ 这里登记什么，设置页就显示什么。所以规则是：
  //   **没有真正读写的键不要登记** —— 否则设置页会列出一堆永远
  //   显示"未设置"的条目，用户以为功能坏了（踩过：filesView/stationCols/
  //   dupSepVer/dupFilter 登记了却从没被写过）。
  //   反过来，真正在用的键必须登记，且必须走 Prefs（见 `_backfill`）。
  const KNOWN = {
    'navHidden': { d: '左侧导航窗格是否收起' },
    'volume': { d: '播放器音量' },
    'libMode': { d: '曲库显示方式（曲目 / 专辑）' },
    'libCols': { d: '曲库显示的列' },
    'libFilters': { d: '曲库的搜索词、排序与筛选' },
    'filesSort': { d: '文件浏览的排序方式' },
    'activeSource': { d: '上次选中的数据源' },
    'lastView': { d: '上次停留的页面' },
    'libPageSize': { d: '曲库每批加载条数' },
    // 候选区（跨目录凑齐待处理的清单）—— 它靠"跨刷新存活"才有意义，
    // 刷新一次就清空的话，用户挑到一半全白费。
    'stagingItems': { d: '候选区里待处理的文件清单' },
  };

  function key(k) { return NS + k; }

  /** 读一个偏好；坏了/没有就返回 `dft`。 */
  function get(k, dft = null) {
    try {
      const raw = localStorage.getItem(key(k));
      if (raw === null) return dft;
      const o = JSON.parse(raw);
      // 带版本的信封格式；没有 v 字段的当旧数据，直接丢弃更安全
      if (o && typeof o === 'object' && 'v' in o && 'd' in o) {
        return o.d === undefined ? dft : o.d;
      }
      return dft;
    } catch (e) {
      return dft;                    // 隐私模式 / JSON 坏了都不该影响使用
    }
  }

  /** 写一个偏好。写不进去（配额满/隐私模式）也静默失败。 */
  function set(k, v) {
    try {
      localStorage.setItem(key(k), JSON.stringify({ v: 1, d: v, t: Date.now() }));
      fire(k, v);
      return true;
    } catch (e) {
      return false;
    }
  }

  function del(k) {
    try { localStorage.removeItem(key(k)); fire(k, null); } catch (e) { /* ignore */ }
  }

  function onChange(fn) { listeners.push(fn); }
  function fire(k, v) {
    listeners.forEach((f) => { try { f(k, v); } catch (e) { /* ignore */ } });
  }

  /** 所有已知偏好的当前值（设置页展示 / 重置用）。 */
  function all() {
    const out = {};
    Object.keys(KNOWN).forEach((k) => { out[k] = get(k, undefined); });
    return out;
  }

  /** 清掉所有偏好（回到出厂默认）。 */
  function clear() {
    const n = Object.keys(KNOWN);
    n.forEach((k) => del(k));
    // 兼容早期直接写的裸键
    ['msh.navHidden', 'msh.volume', 'msh.libMode', 'msh.orgPref'].forEach((k) => {
      try { localStorage.removeItem(k); } catch (e) { /* ignore */ }
    });
    Object.keys(localStorage).forEach((k) => {
      if (k.startsWith(NS)) { try { localStorage.removeItem(k); } catch (e) {} }
    });
    return n.length;
  }

  /** 记住"上次停在哪个页面"。 */
  function rememberView(name) { set('lastView', name); }

  /**
   * 把早期**直接写裸键**的偏好搬进 Prefs 体系。
   *
   * 踩过：`volume` / `navHidden` / `libCols` 这几项当时用
   * `localStorage.setItem('msh.volume', ...)` 直接写，而 `Prefs.get`
   * 解析的是带版本的信封 `{v,d}` —— 裸值解析不出 `v` 字段，于是
   * **设置页把它们显示成"未设置"，但实际是有值的**。
   * 功能没坏，界面在说谎，用户会以为记忆功能失效。
   *
   * 只在 Prefs 里还没有值时搬一次，之后统统走 Prefs。
   */
  function _backfill() {
    const RAW = {
      // Prefs 键 -> 裸键（老写法同时试带前缀和不带前缀两种）
      volume: ['msh.volume', 'volume'],
      navHidden: ['msh.navHidden', 'navHidden'],
    };
    Object.keys(RAW).forEach((k) => {
      let have = null;
      try { have = get(k, null); } catch (e) { /* ignore */ }
      if (have !== null && have !== undefined) return;
      for (const raw of RAW[k]) {
        try {
          const v = localStorage.getItem(raw);
          if (v === null) continue;
          // navHidden 老值是 '1'/'0'，volume 是数字字符串
          set(k, k === 'navHidden' ? (v === '1') : (Number(v) || 0));
          break;
        } catch (e) { /* ignore */ }
      }
    });
  }
  try { _backfill(); } catch (e) { /* ignore */ }

  return { get, set, del, all, clear, onChange, rememberView, KNOWN };
})();
