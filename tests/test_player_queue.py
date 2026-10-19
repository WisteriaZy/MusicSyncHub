"""播放器队列的**形状契约**回归测试。

## 这个 bug 值得单独锁住

用户反馈"播放器中看不中用"。真实表现（在真实页面上复现并抓到了根因）：

  · 点曲目右侧的 ▶ 之后，队列**被正确填满**（items=300、index 正确）
  · `queue.current` 返回的对象**看起来也对**（打印出来就是那首歌的全部字段）
  · 但标题一直是"未播放"、`audio.src` 一直是空、`audio.paused` 一直是 true
  · **不抛任何异常**，控制台只有一句 `[playTrack] 没有曲目对象`

根因是**形状不一致**：

    queue.items      = [{ track, label }, ...]
    queue.step(dir)  → 返回条目 { track, label }        ✅
    queue.current    → 曾经返回裸曲目 this.items[i].track  ❌

而所有使用方都按**条目**在用：

    playList:  const cur = queue.current;      playTrack(cur.track, {label: cur.label})
    stepTrack: const nx  = queue.step(dir);    playTrack(nx.track,  {label: nx.label})
    循环重播:  const f   = queue.current;      playTrack(f.track,   {label: f.label})
    随机播放:  const it  = queue.current;      playTrack(it.track,  {label: it.label})
    队列面板:  const it  = queue.current;      playTrack(it.track,  {label: it.label})

于是 `cur.track` 是 `undefined`，`playTrack` 在开头一句 `if (!t) return;`
就静静返回了 —— 前面所有状态（队列、index、current 的形状）都是对的，
所以从外面看"什么都对，就是不出声"。

**所以这里用源码级契约测试锁住：`current` 和 `step` 必须返回同一种形状。**
这种 bug 用端到端测试很难覆盖（要真浏览器 + 真音频），
但契约测试能立刻抓到"有人又把 current 改成返回裸曲目"。
"""
from __future__ import annotations

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from .srchelp import fn_body  # noqa: E402

APP_JS = os.path.join(ROOT, "app", "web", "static", "js", "app.js")


class QueueShapeContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = open(APP_JS, encoding="utf-8").read()

    def _queue_block(self) -> str:
        i = self.src.index("const queue = {")
        j = self.src.index("\n  };", i)
        return self.src[i:j]

    def test_current_returns_entry_not_raw_track(self):
        """★ `current` 必须返回 `{track, label}` 条目，不能返回裸曲目。

        这是"点了播放没反应"的根因，见模块 docstring。
        """
        blk = self._queue_block()
        m = re.search(r"get current\(\)\s*\{(.*?)\n    \},", blk, re.S)
        self.assertIsNotNone(m, "找不到 current 取值器")
        body = m.group(1)
        self.assertIn("this.items[this.index]", body)
        # 关键断言：不能出现 `.track` 的解包
        self.assertNotIn(".track", body,
                         "current 又返回裸曲目了 —— playList/stepTrack 会读到 "
                         "undefined，表现就是'点了播放没反应'")

    def test_step_returns_entry(self):
        """`step` 返回条目（和 current 保持一致）。"""
        blk = self._queue_block()
        m = re.search(r"step\(dir\)\s*\{(.*?)\n    \},", blk, re.S)
        self.assertIsNotNone(m, "找不到 step")
        self.assertIn("return this.items[i]", m.group(1))

    def test_items_are_wrapped_entries(self):
        """`items` 里放的是 `{track, label}` 而不能是裸曲目。"""
        blk = self._queue_block()
        self.assertIn(".map((t) => ({ track: t, label }))", blk)

    def test_all_current_consumers_unwrap_track(self):
        """★ 所有用到 `current`/`step` 的地方都必须按配置解包 `.track`。

        这条是反向保险：如果将来有人"修"回去、改成把 current 变回裸曲目，
        这些使用方就会集体变成 `undefined` —— 这里会立刻报出来。
        """
        # 5 个使用方：playList / stepTrack / 循环重播 / 随机播放 / 队列面板
        pattern = re.compile(
            r"(?:const|let)\s+(\w+)\s*=\s*queue\.(current|step\([^)]*\))\s*;"
            r"[\s\S]{0,200}?playTrack\(\1\.track")
        hits = pattern.findall(self.src)
        self.assertGreaterEqual(
            len(hits), 5,
            f"按 .track 解包 queue.current/step 的地方少了：只找到 {hits}")
        # 明确列出这几个名字，改坏了能一眼看出漏了谁
        names = {h[0] for h in hits}
        for want in ("cur", "nx", "f", "it"):
            self.assertIn(want, names, f"少了 {want} 这一处的解包")

    def test_playtrack_guards_undefined(self):
        """`playTrack` 对 undefined 必须**明确报出来**而不是静默返回。

        原来只有一句 console.warn，用户/开发者都看不到 ——
        "点了没反应"因此极难查。现在至少要有提示。
        """
        seg = fn_body(self.src, "playTrack")
        self.assertTrue(seg)
        self.assertIn("if (!t)", seg)
        # 守卫里要有给用户/开发者的可见信号，不能只有一个 return
        self.assertTrue(
            "toast" in seg[:600] or "console.warn" in seg[:600],
            "playTrack 的 undefined 守卫必须留下可见线索")


def _find_node() -> str | None:
    """找 node 可执行文件。

    顺序：`MSH_NODE` 环境变量 → PATH 里的 `node` → 几个常见安装位置
    → 从 `DSH_NODE` / 已知 IDE 运行时目录里找。

    **不写死本机绝对路径** —— 之前这段硬编码了本机的 node，
    导致别人 clone 下来这 5 条测试永远跳过。现在换成本机没有 node 时
    可以通过 `MSH_NODE` 指定，或者被环境里已有的 node 自动找到。
    """
    import shutil as _sh
    env = os.environ.get("MSH_NODE")
    if env and os.path.isfile(env):
        return env
    p = _sh.which("node")
    if p:
        return p
    cands = [
        r"C:\Program Files\nodejs\node.exe",
        os.path.expanduser(r"~\AppData\Roaming\npm\node.exe"),
        os.path.expanduser(r"~\scoop\apps\nodejs\current\node.exe"),
        "/usr/local/bin/node",
        "/usr/bin/node",
    ]
    # 有的打包环境会通过环境变量告诉外部 node 在哪
    for key in ("DSH_NODE", "NODE_BINARY", "NODE_EXE"):
        v = os.environ.get(key)
        if v and os.path.isfile(v):
            cands.insert(0, v)
    for cand in cands:
        if cand and os.path.isfile(cand):
            return cand
    return None


class QueueBehaviourTest(unittest.TestCase):
    """用 Node 真跑一遍队列逻辑（把那段 JS 抠出来执行）。"""

    @classmethod
    def setUpClass(cls):
        import shutil
        cls.node = shutil.which("node")
        cls.src = open(APP_JS, encoding="utf-8").read()

    def _run(self, script: str) -> str:
        import subprocess
        node = self.node or _find_node()
        if not node:
            self.skipTest("需要 node（装 nodejs 或设 MSH_NODE 环境变量）")
        r = subprocess.run([node, "-e", script], capture_output=True,
                           text=True, timeout=120)
        if r.returncode != 0:
            self.fail(f"node 失败: {r.stderr[:400]}")
        return r.stdout.strip()

    @unittest.skipUnless(_find_node(), "需要 node（装 nodejs 或设 MSH_NODE 环境变量）")
    def test_current_and_step_agree(self):
        """真跑：`current` 和 `step` 返回的形状必须一致。"""
        i = self.src.index("const queue = {")
        j = self.src.index("\n  };", i) + len("\n  };")
        blk = self.src[i:j]
        script = blk + """
const tr = (id) => ({ id, name: 'T' + id });
queue.set([tr(1), tr(2), tr(3)], 2, 'L');
const shape = (x) => x === null ? 'null' : Object.keys(x).sort().join(',');
// ★ 先取 current 再 step：step() 会推进 index，
//   如果放在同一个对象字面量里，`queue.current` 读到的是**推进之后**的项。
const a = queue.current;
const aId = a && a.track && a.track.id;
const aShape = shape(a);
const b = queue.step(1);
const bShape = shape(b);
console.log(JSON.stringify({
  aId, aShape, bShape,
  bId: b && b.track && b.track.id,
  items: queue.items.length,
  sameShape: aShape === bShape,
  unwrapOk: aId === 2,
}));
"""
        import json
        out = json.loads(self._run(script))
        self.assertEqual(3, out["items"])
        self.assertEqual(2, out["aId"], f"current.track.id 应是 2: {out}")
        self.assertEqual(3, out["bId"], f"step(1) 后应是 id=3: {out}")
        self.assertTrue(out["sameShape"],
                        f"current 与 step 形状不一致: {out}")
        self.assertTrue(out["unwrapOk"],
                        f"current.track 取不到曲目: {out}")

    @unittest.skipUnless(_find_node(), "需要 node（装 nodejs 或设 MSH_NODE 环境变量）")
    def test_startid_not_found_falls_back_to_first(self):
        """startId 不在列表里 → 从头开始，而不是播不出来。"""
        i = self.src.index("const queue = {")
        j = self.src.index("\n  };", i) + len("\n  };")
        blk = self.src[i:j]
        script = blk + """
queue.set([{id:1},{id:2}], 999, 'L');
console.log(JSON.stringify({
  index: queue.index,
  cur: queue.current && queue.current.track.id,
}));
"""
        import json
        out = json.loads(self._run(script))
        self.assertEqual(0, out["index"])
        self.assertEqual(1, out["cur"])

    @unittest.skipUnless(_find_node(), "需要 node（装 nodejs 或设 MSH_NODE 环境变量）")
    def test_empty_queue_current_is_null(self):
        i = self.src.index("const queue = {")
        j = self.src.index("\n  };", i) + len("\n  };")
        blk = self.src[i:j]
        script = blk + """
queue.clear();
console.log(JSON.stringify({ cur: queue.current, step: queue.step(1) }));
"""
        import json
        out = json.loads(self._run(script))
        self.assertIsNone(out["cur"])
        self.assertIsNone(out["step"])

    @unittest.skipUnless(_find_node(), "需要 node（装 nodejs 或设 MSH_NODE 环境变量）")
    def test_step_skips_dead_entries(self):
        """`step` 必须跳过失效条目，不能把它交出去。

        踩过：队列里混进 `{track: null}`（曲目被删 / 重扫）之后，
        `step()` 把坏条目返回给调用方 → `playTrack(null)` 在开头静默
        return → 表现就是"按了下一首完全没反应"。极难查。
        """
        i = self.src.index("const queue = {")
        j = self.src.index("\n  };", i) + len("\n  };")
        blk = self.src[i:j]
        script = blk + """
queue.set([{id:1},{id:2},{id:3}], 1, 'L');
// 在 1 和 2 之间塞两个坏条目
queue.items.splice(1, 0, {track:null,label:'bad'}, {track:{id:null},label:'bad2'});
const nx = queue.step(1);
console.log(JSON.stringify({
  nx: nx && nx.track && nx.track.id,
  index: queue.index,
}));
"""
        import json
        out = json.loads(self._run(script))
        self.assertEqual(2, out["nx"], "step 应该跳到下一个**有效**条目")
        self.assertEqual(3, out["index"], "index 应该落在有效条目上")

    @unittest.skipUnless(_find_node(), "需要 node（装 nodejs 或设 MSH_NODE 环境变量）")
    def test_prune_keeps_position(self):
        """`prune` 剪掉坏条目后，当前这首歌不能跑掉（两种情况）。

        A) 当前这首**本身有效**、别的条目失效 → 当前必须还是它。
        B) 当前这首**自己被删了** → 落到它原来位置之后第一个有效条目
           （后面没有就往前退）。原来我写"退到同序号"，序号会越界，
           会选到用户完全不期望的条目。
        """
        i = self.src.index("const queue = {")
        j = self.src.index("\n  };", i) + len("\n  };")
        blk = self.src[i:j]

        # ---- A) 当前有效，只删前后别的 ----
        script_a = blk + """
const q = queue;
q.set([{id:1},{id:2},{id:3},{id:4}], 3, 'L');   // 当前 = id 3（index 2）
q.items[0] = { track: null, label: 'dead' };     // 删掉第 1 首
q.items[3] = { track: { id: null }, label: 'dead2' };  // 删掉第 4 首
const removed = q.prune();
console.log(JSON.stringify({
  removed,
  curAfter: q.current && q.current.track && q.current.track.id,
  ids: q.items.map((x) => x.track && x.track.id),
}));
"""
        import json
        a = json.loads(self._run(script_a))
        self.assertEqual(2, a["removed"])
        self.assertEqual([2, 3], a["ids"])
        self.assertEqual(3, a["curAfter"], "A) 当前这首自身有效，剪完必须还是它")

        # ---- B) 当前这首自己也被删了 ----
        script_b = blk + """
const q = queue;
q.set([{id:1},{id:2},{id:3},{id:4}], 3, 'L');   // 当前 = id 3（index 2）
q.items[2] = { track: null, label: 'dead' };     // 当前这首被删掉了
q.items[0] = { track: { id: null }, label: 'dead2' };
const removed = q.prune();
console.log(JSON.stringify({
  removed,
  curAfter: q.current && q.current.track && q.current.track.id,
  ids: q.items.map((x) => x.track && x.track.id),
}));
"""
        b = json.loads(self._run(script_b))
        self.assertEqual(2, b["removed"])
        self.assertEqual([2, 4], b["ids"])
        self.assertEqual(4, b["curAfter"],
                         "B) 当前这首被删了 → 应该落到它后面第一个有效的（id=4）")

    def test_playtrack_manages_queue(self):
        """`playTrack` 必须保证"正在播的这首"在队列里。

        踩过（用户报的）：`playTrack` 只换歌、**不动队列**。
        于是在「曲库」播过一次（队列被填成整库）之后，再去「文件浏览」
        双击单曲 → 歌换了，队列还是整库，而正在播的这首**根本不在队列里**。
        表现就是"播单个音乐时，播放列表里出现整个库源"。
        """
        body = fn_body(self.src, "playTrack")
        self.assertIn("queue.prune()", body, "播之前要剪掉失效条目")
        self.assertIn("_fromQueue", body, "要能区分'队列内部调用'")
        self.assertIn("queue.set(", body, "不在队列里时应把队列换成这一首")
        self.assertIn("queue.index = at", body, "已在队列里时应只移动当前位置")
        # 队列内部的调用方必须带标记，否则会把队列结构冲掉
        for caller in ("playList", "stepTrack"):
            cb = fn_body(self.src, caller)
            if "playTrack(" in cb:
                self.assertIn("_fromQueue: true", cb,
                              f"{caller} 里的 playTrack 调用必须带 _fromQueue")


if __name__ == "__main__":
    unittest.main()
