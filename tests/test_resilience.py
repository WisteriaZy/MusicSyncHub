"""执行容错：错误分类 + 可重试清单 + 批量全选。

来自用户的直接要求：

  · "系统在执行过程中出现文件夹突然不见了、IO 中断、文件被占用等等
     常见异常问题，系统要有能力去跳过他，继续执行其他操作，
     最后汇报哪些操作完成、哪些未分配、哪些出错，
     用户可以根据这个情况对那些未分配或者出错的文件进行重试"
  · "有些功能板块选择文件还是很麻烦，最基本的全选全不选都没有"

要点：
  1. **单个失败不能中断整批**（Executor 用 `ex.map` + 每步 try/except）；
  2. 失败要**分类**，因为"该怎么办"完全不同：
     被占用 → 过会儿重试；不存在 → 重试没用，得先扫描；
     空间不足 → 先清理；目标已存在 → 是冲突不是错误；
  3. 汇总里要给出 `retryable_steps`，界面才能一键重试。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from .srchelp import fn_body

from app.core import ops as OPS

# 项目根（不要写死本机路径 —— 别人 clone 到别的目录就跑不了了）
_PROJ = __import__('pathlib').Path(__file__).resolve().parent.parent



class ClassifyErrorTest(unittest.TestCase):

    def test_missing_file(self):
        self.assertEqual(OPS.ERR_MISSING,
                         OPS.classify_error(FileNotFoundError("x")))
        self.assertEqual(OPS.ERR_MISSING,
                         OPS.classify_error(OSError(2, "No such file")))
        self.assertEqual(OPS.ERR_MISSING,
                         OPS.classify_error(Exception("找不到文件")))

    def test_locked_or_permission(self):
        """被占用 / 权限不足是**最值得重试**的一类。"""
        self.assertEqual(OPS.ERR_LOCKED,
                         OPS.classify_error(PermissionError("denied")))
        self.assertEqual(OPS.ERR_LOCKED,
                         OPS.classify_error(OSError(13, "Permission denied")))
        self.assertEqual(OPS.ERR_LOCKED, OPS.classify_error(
            OSError("The process cannot access the file because it is being "
                    "used by another process")))
        self.assertEqual(OPS.ERR_LOCKED, OPS.classify_error(
            Exception("文件正由另一进程使用，因此该进程无法访问此文件")))

    def test_no_space(self):
        self.assertEqual(OPS.ERR_NOSPACE,
                         OPS.classify_error(OSError(28, "No space left")))
        self.assertEqual(OPS.ERR_NOSPACE,
                         OPS.classify_error(Exception("磁盘空间不足")))

    def test_readonly_is_its_own_kind(self):
        """只读要单独一类：重试没用，得换目标。"""
        self.assertEqual(OPS.ERR_READONLY,
                         OPS.classify_error(Exception("目标源只读，无法写入")))
        self.assertEqual(OPS.ERR_READONLY,
                         OPS.classify_error(Exception("read-only source")))

    def test_unknown_falls_back(self):
        self.assertEqual(OPS.ERR_OTHER,
                         OPS.classify_error(ValueError("莫名其妙")))

    def test_every_kind_has_a_label(self):
        """每种分类都要有中文标签，界面直接显示。"""
        for k in (OPS.ERR_MISSING, OPS.ERR_LOCKED, OPS.ERR_NOSPACE,
                  OPS.ERR_CONFLICT, OPS.ERR_READONLY, OPS.ERR_OTHER):
            self.assertIn(k, OPS.ERR_LABEL)
            self.assertTrue(OPS.ERR_LABEL[k])

    def test_retryable_set_is_a_subset(self):
        self.assertTrue(OPS.ERR_RETRYABLE)
        for k in OPS.ERR_RETRYABLE:
            self.assertIn(k, OPS.ERR_LABEL)


class TallyTest(unittest.TestCase):
    """汇总要能回答"哪些完成 / 哪些没做 / 哪些出错 / 哪些能重试"。"""

    def _results(self):
        return [
            {"step": "a", "op": "move", "state": "done", "bytes": 100},
            {"step": "b", "op": "move", "state": "error",
             "error_kind": OPS.ERR_LOCKED, "detail": "被占用"},
            {"step": "c", "op": "move", "state": "error",
             "error_kind": OPS.ERR_MISSING, "detail": "不见了"},
            {"step": "d", "op": "move", "state": "conflict",
             "error_kind": OPS.ERR_CONFLICT, "detail": "已存在"},
            {"step": "e", "op": "move", "state": "skipped"},
            {"step": "f", "op": "move", "state": "cancelled"},
        ]

    def test_counts_every_state(self):
        s = OPS._tally(self._results())
        self.assertEqual(6, s["total"])
        self.assertEqual(1, s["done"])
        self.assertEqual(2, s["error"])
        self.assertEqual(1, s["conflict"])
        self.assertEqual(1, s["skipped"])
        self.assertEqual(1, s["cancelled"])

    def test_error_kinds_and_labels(self):
        s = OPS._tally(self._results())
        self.assertEqual(1, s["error_kinds"][OPS.ERR_LOCKED])
        self.assertEqual(1, s["error_kinds"][OPS.ERR_MISSING])
        self.assertEqual(1, s["error_kinds"][OPS.ERR_CONFLICT])
        self.assertIn(OPS.ERR_LOCKED, s["error_labels"])

    def test_retryable_only_lists_retryable(self):
        """★ 只有"值得重试"的才进清单 —— 文件不见了重试也是白搭。"""
        s = OPS._tally(self._results())
        self.assertEqual(["b"], s["retryable_steps"])

    def test_bytes_counted_for_done_only(self):
        s = OPS._tally(self._results())
        self.assertEqual(100, s["bytes_moved"])

    def test_empty_results_are_safe(self):
        s = OPS._tally([])
        self.assertEqual(0, s["total"])
        self.assertNotIn("retryable_steps", s)


class ExecutorResilienceTest(unittest.TestCase):
    """单个失败不能拖垮整批，而且失败步骤要带分类。"""

    def test_run_continues_after_failure(self):
        from app.core.ops import Executor

        class Bad:
            """一个会抛异常、一个能成功的假数据源。"""
            can_write = True
            can_delete = True
            root = "D:/X"

            def abs_path(self, p):
                return "D:/X/" + str(p).lstrip("/")

            def exists(self, p):
                return False

            def stat(self, p):
                if "bad" in p:
                    raise PermissionError("被占用")
                return type("S", (), {"size": 1, "is_dir": False})()

        steps = [
            {"id": "1", "op": "copy", "src": "s", "dst": "s", "src_path": "bad1.flac",
             "dst_path": "b/bad1.flac"},
            {"id": "2", "op": "copy", "src": "s", "dst": "s", "src_path": "bad2.flac",
             "dst_path": "b/bad2.flac"},
        ]
        ex = Executor({"s": Bad()}, db=None)
        # 预演不该抛异常，而且两步都要有结果
        r = ex.run(steps, execute=False)
        self.assertEqual(2, len(r["results"]))
        for x in r["results"]:
            self.assertIn("state", x)


class BatchSelectTest(unittest.TestCase):
    """批量基础操作：全选 / 全不选（大量文件时的效率问题）。"""

    JS = None

    @classmethod
    def setUpClass(cls):
        cls.JS = (_PROJ
                  / "app" / "web" / "static" / "js"
                  / "views.files.js").read_text(encoding="utf-8")

    def test_selbar_has_select_all_toggle(self):
        """选中条上必须有全选/全不选，且显示可选数量。"""
        self.assertIn("全选", self.JS)
        self.assertIn("全不选", self.JS)

    def test_select_all_toggles_both_ways(self):
        """同一个全选框要能在"全选"和"全不选"之间切换。"""
        seg = fn_body(self.JS, "paintSelbarFrom")
        self.assertTrue(seg, "找不到 paintSelbarFrom")
        self.assertIn("allBox.onchange", seg)
        self.assertIn("checked.add", seg)
        self.assertIn("checked.delete", seg)
        # ★ 全选那条路径**不能**重建整张表：实测重建 800 行要 120 ms，
        #   用户看到的就是"点了全选，状态半天不更新"。
        #   注意只看 `allBox.onchange` 这一段 —— 函数里别处（比如
        #   "加入候选区"之后）重画列表是合理的。
        seg_all = fn_body(self.JS, "paintSelbarFrom")
        k = seg_all.index("allBox.onchange")
        handler = seg_all[k:seg_all.index("};", k)]
        self.assertIn("repaintSelbarOnly", handler)
        self.assertNotIn("redraw();", handler,
                         "全选又走了整表重画 —— 那会把 120 ms 的卡顿带回来")

    def test_tree_result_view_has_its_own_select_all(self):
        """目录树选中结果也要能全选（不能退化成走搜索重绘）。"""
        seg = fn_body(self.JS, "paintTreeFiles")
        self.assertTrue(seg, "找不到 paintTreeFiles")
        self.assertIn("paintSelbarFrom(treeFiles", seg)
        self.assertIn("paintTreeFiles", seg)
        # 索引必须在画列表时重建，否则半选/全选状态永远是空的
        self.assertIn("rebuildDirIndex()", seg)

    def test_tree_result_checkbox_state_is_not_hardcoded(self):
        """★ 树结果的勾选框必须按 `checked` 决定状态，**不能写死 checked**。

        踩过：写死 `checked` 之后，`paintTreeFiles()` 每次重绘都把用户刚
        取消的勾选又勾回来 —— 表现是"全不选点了没反应"（按钮文案变了、
        数据也清了，但界面上的框还是全勾着）。
        """
        # 按函数体取，不用固定字符窗口：往函数里加几行就会把断言挤出去
        seg = fn_body(self.JS, "paintTreeFiles")
        self.assertTrue(seg, "找不到 paintTreeFiles")
        self.assertIn("const on = checked.has(id)", seg,
                      "树结果行要按 checked 计算勾选状态")
        self.assertRegex(seg, r"\$\{on \? 'checked' : ''\}",
                         "勾选框要由 on 决定")
        self.assertNotRegex(seg, r'data-check="\$\{U\.esc\(id\)\}" checked>',
                            "不许写死 checked")

    def test_station_has_select_all(self):
        js = (_PROJ / "app"
              / "web" / "static" / "js" / "views.station.js").read_text(encoding="utf-8")
        self.assertIn("st-all", js)
        self.assertIn("st-none", js)

    def test_dedup_has_bulk_group_select(self):
        """查重页要有"全部勾选 / 忽略全部"的批量入口。"""
        js = (_PROJ / "app"
              / "web" / "static" / "js" / "views.duplicates.js").read_text(encoding="utf-8")
        self.assertIn("dp-keep-all", js)
        self.assertIn("dp-skip-all", js)


class TriStateCheckboxTest(unittest.TestCase):
    """全选 / 半选 / 全不选 必须**看得出区别**。

    用户原话："复选框甚至全选和部分选的显示是一样的 很不合规范"。
    之前的确是这样：只选了 3 个和全选 271 个，按钮都写"全选本目录"，
    而且从来没有 `indeterminate` —— 半选在视觉上根本不存在。
    """

    JS = None
    STG = None

    @classmethod
    def setUpClass(cls):
        base = _PROJ / "app" / "web" / "static" / "js"
        cls.JS = (base / "views.files.js").read_text(encoding="utf-8")
        cls.STG = (base / "staging.js").read_text(encoding="utf-8")

    def test_select_all_box_is_a_checkbox(self):
        """全选要做成复选框才能表达"半选"，按钮做不到。"""
        # 按**函数体**取，不用固定字符窗口：往模板里加一个按钮
        # 就会把后面的断言挤出窗口，测试变成"改动即失败"。
        seg = fn_body(self.JS, "paintSelbarFrom")
        self.assertTrue(seg, "找不到 paintSelbarFrom")
        self.assertIn('type="checkbox" id="fb-all"', seg)

    def test_three_states_have_distinct_text(self):
        seg = fn_body(self.JS, "paintSelbarFrom")
        self.assertTrue(seg, "找不到 paintSelbarFrom")
        self.assertIn("allOn ?", seg)
        self.assertIn("someOn ?", seg)
        # 三种文案都要有：取消全选 / 已选 n / N / 全选
        self.assertIn("取消全选", seg)
        self.assertIn("已选 ${n} / ${total}", seg)

    def test_indeterminate_is_set(self):
        """★ 半选必须真的设 indeterminate，否则和"全选"长得一样。"""
        seg = fn_body(self.JS, "paintSelbarFrom")
        self.assertRegex(seg, r"indeterminate\s*=\s*someOn")
        # 全选路径（只刷数字那条）也必须维护半选，否则点了全选之后
        # 半选状态就丢了
        fast = fn_body(self.JS, "repaintSelbarOnly")
        self.assertTrue(fast, "找不到 repaintSelbarOnly")
        self.assertRegex(fast, r"indeterminate\s*=\s*someOn")
        self.assertRegex(fast, r"checked\s*=\s*allOn")

    def test_plain_list_selbar_also_tri_state(self):
        seg = fn_body(self.JS, "paintSelbar")
        self.assertTrue(seg, "找不到 paintSelbar")
        self.assertIn("indeterminate = someOn", seg)
        self.assertIn('type="checkbox" id="fb-all"', seg)

    def test_staging_bulk_is_tri_state(self):
        self.assertIn('id="stg-ball-box"', self.STG)
        self.assertRegex(self.STG, r"ballBox\.indeterminate\s*=\s*someOn")
        self.assertIn("已部分勾选", self.STG)

    def test_staging_bulk_visible_without_selection(self):
        """★ 候选区的批量条**没勾选时也要显示**。

        原来"没勾选就不画这条"，于是"全选"按钮也一起消失 ——
        用户得先手动勾一个才看得到全选，正好和"想全选"的意图相反。
        """
        i = self.STG.index("function paintBulk")
        seg = self.STG[i:i + 700]
        self.assertIn("if (!items.length) return;", seg,
                      "只在**候选区为空**时才不显示")
        self.assertNotRegex(seg, r"if \(!sel\.length\) return;",
                            "不能因为没勾选就整条不显示")

    def test_staging_has_invert(self):
        self.assertIn("stg-binv", self.STG)

    def test_tree_parent_shows_partial(self):
        """★ 树的父节点要能显示"部分子目录已勾选"。

        之前 `paintTree` 只按 `treePicked.has(rel)` 决定父子节点的框，
        勾了子目录父节点还是空框，看起来像没生效。
        """
        self.assertIn("function treeCover", self.JS)
        i = self.JS.index("function treeCover")
        seg = self.JS[i:i + 900]
        self.assertIn("'some'", seg)
        self.assertIn("'all'", seg)
        i2 = self.JS.index("function paintTree()")
        seg2 = self.JS[i2:i2 + 3200]
        self.assertIn("treeCover(f.rel)", seg2)
        self.assertIn("indeterminate = true", seg2)

    def test_tree_refreshes_after_selection_changes(self):
        """勾选后必须刷新目录树，父节点的半选/全选状态才会更新。

        用 `refreshTreeChecks`（只改属性）而不是 `paintTree`（重建 DOM）——
        后者会调 `paintTreeBar` → `syncTreeToFiles` → 又回来，形成循环。
        """
        i = self.JS.index("async function syncTreeToFiles")
        seg = self.JS[i:i + 1600]
        self.assertIn("refreshTreeChecks()", seg)
        self.assertNotIn("paintTree()", seg,
                         "不能在这里重建整棵树（会递归）")


class StagedFileIndicationTest(unittest.TestCase):
    """已进候选区的文件要**看得出来**、**不能再选**。

    用户原话："选过的文件在上方仍然可选 虽然在候选区内不会重复 但也不直观
    要么灰色显示 要么就其他方法提示"。
    """

    JS = None

    @classmethod
    def setUpClass(cls):
        cls.JS = (_PROJ / "app"
                  / "web" / "static" / "js" / "views.files.js").read_text(encoding="utf-8")

    def test_staged_rows_disable_checkbox(self):
        i = self.JS.index("function paintStagedMarks")
        seg = self.JS[i:i + 3000]
        self.assertIn("cb.disabled = true", seg)
        self.assertIn("cb.checked = false", seg)

    def test_staged_checkbox_has_explaining_tooltip(self):
        i = self.JS.index("function paintStagedMarks")
        seg = self.JS[i:i + 3000]
        self.assertIn("这个文件已经在候选区里了", seg)

    def test_staged_rows_show_badge(self):
        i = self.JS.index("function paintStagedMarks")
        seg = self.JS[i:i + 3000]
        self.assertIn("已在候选区", seg)

    def test_staged_files_removed_from_selection(self):
        """已进候选区的要从 `checked` 剔除，否则"已选 N"和批量操作会算重。"""
        i = self.JS.index("function paintStagedMarks")
        seg = self.JS[i:i + 3200]
        self.assertIn("checked.delete(didKey)", seg)

    def test_no_redraw_loop(self):
        """剔勾选会触发选中条重绘 → 又回到 paintStagedMarks，必须防循环。"""
        i = self.JS.index("function paintStagedMarks")
        seg = self.JS[i:i + 3200]
        self.assertIn("_busy", seg)

    def test_tree_result_view_marks_staged_too(self):
        """目录树选中结果里也要标出来（不能只有普通列表有）。"""
        i = self.JS.index("function paintTreeFiles")
        seg = self.JS[i:i + 4200]
        self.assertIn("stagedDid", seg)
        self.assertIn("已在候选区", seg)
        self.assertIn("disabled", seg)

    def test_add_button_only_counts_new_files(self):
        """按钮上的数字只算"还没进候选区的"，否则数字会骗人。"""
        i = self.JS.index("function paintSelbar(")
        seg = self.JS[i:i + 2600]
        self.assertIn("stagedDid", seg)
        self.assertIn("n - stagedHere", seg)


class TreeUncheckCascadeTest(unittest.TestCase):
    """★ 取消勾选目录必须**真的**取消掉里面的文件。

    用户反馈："文件浏览依旧有联动问题 比如取消点击文件夹复选框
    文件仍然被全选"。

    复现出来是这样的（父节点带子目录）：
      1. 父节点是半选（indeterminate）时，浏览器一点会把 `checked` 置为
         `true`（半选语义上先"补齐"），于是 `treeCover(parent)==='all'`，
         子节点被全部勾上 —— 这步没问题；
      2. 再点一次想取消，如果只 `treePicked.delete(parent)`，
         **子节点还留在集合里**，`treeCover(parent)` 立刻又变回 `'some'`；
      3. 结果父节点永远停在半选，文件永远清不掉。

    修法是**级联**：勾/取消父节点时把它下面所有子目录一起处理，
    父子状态永远自洽。下面这些断言把关键点锁住。
    """

    JS = None

    @classmethod
    def setUpClass(cls):
        cls.JS = (_PROJ / "app"
                  / "web" / "static" / "js" / "views.files.js").read_text(encoding="utf-8")

    def test_tree_set_all_is_recursive(self):
        """级联函数必须递归处理所有子目录（不只是直接子级）。"""
        self.assertIn("function treeSetAll", self.JS)
        i = self.JS.index("function treeSetAll")
        seg = self.JS[i:i + 500]
        self.assertIn("treePicked.add(rel)", seg)
        self.assertIn("treePicked.delete(rel)", seg)
        # 递归调用自己 = 处理所有层级
        self.assertIn("treeSetAll(k.rel, on)", seg)

    def test_checkbox_handler_cascades(self):
        """勾选框 onchange 必须走级联，不能只改自己那一个。

        注意 `cb.onchange` 在文件里出现多次（文件行的复选框也叫 cb），
        所以用 `treeSetAll` 作锚点定位到**树的那一处**。
        """
        i = self.JS.index("treeSetAll(rel, cb.checked)")
        seg = self.JS[max(0, i - 420):i + 420]
        self.assertIn("cb.onchange", seg, "级联要发生在树的复选框 onchange 里")
        self.assertNotRegex(
            seg, r"if \(cb\.checked\) treePicked\.(?:add|delete)\(rel\)",
            "不能只增删自己，子目录会残留")

    def test_ctrl_click_row_cascades_too(self):
        """Ctrl 点整行也要级联，否则两种操作行为不一致。"""
        i = self.JS.index("if (e.ctrlKey || e.metaKey || e.shiftKey)")
        seg = self.JS[i:i + 400]
        self.assertIn("treeSetAll(rel, next)", seg)

    def test_cascade_helper_exists(self):
        """级联 + 轻量刷新这两个辅助函数都要在。"""
        self.assertIn("function treeSetAll", self.JS)
        self.assertIn("function refreshTreeChecks", self.JS)

    def test_refresh_prefers_file_level_truth(self):
        """★ 树的框要按**文件的真实勾选**算，不能只看"目录被点过"。

        踩过：勾了目录 → 再取消其中 3 个文件 → 目录框**还是全勾**，
        和下面的列表对不上。用户看到的就是"复选框状态不对"。
        现在：全选中→全勾，选一部分→半选，一个没选→空。
        """
        i = self.JS.index("function refreshTreeChecks")
        seg = fn_body(self.JS[i:], "refreshTreeChecks")
        self.assertTrue(seg, "找不到 refreshTreeChecks")
        # ★ 判据收敛到 `treeCover()`：树视图下由 `coverFromFiles()` 数文件，
        #   平时按目录勾选算。以前这里读预建索引（dirOn/dirTotal）、
        #   而 `paintTree` 读 `treePicked` —— 两套算法必然打架，
        #   表现就是"右侧全选/部分选与树不同步"。
        self.assertIn("treeCover(", seg)
        cov = fn_body(self.JS, "coverFromList")
        self.assertTrue(cov, "找不到 coverFromFiles")
        self.assertIn("checked.has(", cov)
        self.assertIn("'some'", cov)
        # 没有这个目录的文件 → 未选（不许回退 treePicked，否则旧目录会"复活"）
        self.assertNotIn("treePicked.has(rel)", cov)

    def test_selection_changes_refresh_tree(self):
        """每次文件勾选变化都要刷新树的框。"""
        # 普通列表的复选框 / 行点击 / 全选
        for anchor in ("refreshTreeChecks();       // 目录树的半选状态要跟着变",
                       "refreshTreeChecks();\n        paintSelbarFrom(files.tracks || []);"):
            self.assertIn(anchor.replace("\n", "\n"), self.JS)
        # 树结果列表：勾一行也必须刷新树（现在走增量索引 + 只刷数字）
        seg = fn_body(self.JS[self.JS.index("function paintTreeFiles"):],
                      "paintTreeFiles")
        self.assertIn("refreshTreeChecks()", seg,
                      "列表里勾一行之后没有刷新目录树的框")
        # 增量路径：沿祖先链加减，不再重扫全表
        self.assertIn("bumpDirIndex(", seg)

    def test_tree_bar_counts_top_level_only(self):
        """★ 多选条只数"最小的那批目录"。

        `treePicked` 里会带上所有祖先，直接数它的话
        勾一个 Shiroi-ice 会显示"已选 2 个目录"，数字对不上用户的认知。
        """
        i = self.JS.index("function paintTreeBar")
        seg = self.JS[i:i + 1200]
        self.assertIn("lastIndexOf('/')", seg)   # 往上找父级
        self.assertIn("picked.push(rel)", seg)

    def test_tree_bar_clears_stale_text(self):
        """清空时要把条上的文字也清掉，不能留着上一次的数字。"""
        i = self.JS.index("function paintTreeBar")
        seg = self.JS[i:i + 1400]
        self.assertIn("treeFiles = []", seg)
        self.assertIn("box.textContent = ''", seg)


class StagingPersistenceTest(unittest.TestCase):
    """候选区：跨刷新存活 + 基本操作完整。

    候选区的用途就是"跨目录凑齐再一起处理"，用户可能花十几分钟挑歌 ——
    刷新一下全没了的话，这个功能根本没法用。
    """

    @classmethod
    def setUpClass(cls):
        base = _PROJ / "app" / "web" / "static" / "js"
        cls.S = (base / "staging.js").read_text(encoding="utf-8")
        cls.P = (base / "prefs.js").read_text(encoding="utf-8")
        # 下面三个方法是从 TreeUncheckCascadeTest 挪过来时留下的，
        # 它们断言的是 views.files.js —— 这里一并提供，避免 AttributeError。
        cls.JS = (base / "views.files.js").read_text(encoding="utf-8")

    def test_has_save_and_restore(self):
        self.assertIn("function saveNow", self.S)
        self.assertIn("function save()", self.S)
        self.assertIn("function restore()", self.S)
        self.assertIn("save, restore,", self.S)

    def test_registered_in_prefs(self):
        self.assertIn("stagingItems", self.P)

    def test_every_mutation_saves(self):
        """增 / 删 / 改去向 / 清空 都要落盘，否则刷新后状态不完整。"""
        for fn in ("function remove", "function setDst", "function clear"):
            i = self.S.index(fn)
            seg = self.S[i:i + 900]
            self.assertIn("save()", seg, f"{fn} 之后没有落盘")

    def test_restore_on_attach(self):
        """挂载时恢复，且**只恢复一次**（视图可能被重复 mount）。"""
        i = self.S.index("function attach(node)")
        seg = self.S[i:i + 700]
        self.assertIn("restore()", seg)
        self.assertIn("_restored", seg)

    def test_save_is_debounced(self):
        """连续加很多首时不该每首写一次 localStorage。"""
        i = self.S.index("function save() {")
        seg = self.S[i:i + 400]
        self.assertIn("setTimeout", seg)

    def test_restore_is_defensive(self):
        """存的东西可能损坏/过期 —— 恢复不能把界面搞崩。"""
        i = self.S.index("function restore()")
        seg = self.S[i:i + 1000]
        self.assertIn("Array.isArray", seg)
        self.assertIn("catch", seg)

    def test_colors_rebuilt_after_restore(self):
        """刷新后同一个去向要还是同一个颜色（否则用户会以为分配乱了）。"""
        i = self.S.index("function restore()")
        seg = self.S[i:i + 1100]
        self.assertIn("colorFor(", seg)

    def test_save_strips_volatile_fields(self):
        """只存必要字段；封面 URL 能按 id 重建，别占 localStorage。"""
        i = self.S.index("function saveNow")
        seg = self.S[i:i + 900]
        self.assertIn("slim", seg)

    def test_core_operations_present(self):
        """候选区的基本操作要齐。"""
        for fn in ("add", "remove", "setDst", "clear", "render",
                   "checkedDids", "pickDst", "groupByDst",
                   "ready", "pendingCount", "targets", "onChange"):
            self.assertRegex(self.S, rf"\b{fn}\b", f"缺少 {fn}")
        # 视图里要暴露「执行移动」入口
        files = (_PROJ / "app"
                 / "web" / "static" / "js" / "views.files.js").read_text(encoding="utf-8")
        self.assertIn("staging:move", files)

    def test_cover_returns_all_only_when_everything_picked(self):
        """父节点只有"自己 + 所有子目录"都勾了才算 `all`。

        否则取消父节点后会停在 `some`（半选），文件清不掉。
        """
        i = self.JS.index("function treeCover")
        seg = self.JS[i:i + 700]
        self.assertIn("allKids", seg)
        self.assertRegex(seg, r"if \(self && allKids\) return 'all'")

    def test_unchecking_clears_file_selection(self):
        """★ 取消之后必须把 `checked` 清空。

        树里全选过的那批文件如果不清理，会把普通目录列表也传染成全选 ——
        这正是用户看到的现象。
        """
        i = self.JS.index("async function syncTreeToFiles")
        seg = self.JS[i:i + 900]
        self.assertIn("checked.clear()", seg)
        self.assertIn("treeFiles = []", seg)

    def test_leaving_tree_view_repaints_list(self):
        """退出树视图要重画列表（走 load），非树视图也要刷掉残留勾选。"""
        i = self.JS.index("async function syncTreeToFiles")
        seg = self.JS[i:i + 900]
        self.assertIn("treeViewOn = false", seg)
        self.assertIn("await load()", seg)
        self.assertIn("paintList()", seg)


if __name__ == "__main__":
    unittest.main()

class SubdirHintTest(unittest.TestCase):
    """本层没文件、歌都在子目录时，必须给提示和出口。

    用户反馈："没有全选文件夹的时候显示文件夹内没有文件，
    全选文件夹后文件就冒出来了。"

    真实原因不是 bug 而是**没有提示**：根目录本层就是 0 首
    （`file_count=0`、`dir_audio_total=2301`），界面只显示 101 个目录行，
    用户不知道歌在下一层，只能碰巧发现"勾左边目录"。

    现在列表顶部会出一条：
      "这个文件夹本层没有音频文件 —— 它下面有 N 个子目录、共 M 首
       [包含子目录查看]"
    点按钮 = 勾上当前目录（级联），直接进入"选中结果"视图。
    """

    @classmethod
    def setUpClass(cls):
        cls.JS = (_PROJ / "app" / "web" / "static" / "js" / "views.files.js"
                  ).read_text(encoding="utf-8")

    def test_list_renders_hint_when_subdirs_have_audio(self):
        seg = fn_body(self.JS, "paintList")
        self.assertTrue(seg, "找不到 paintList")
        self.assertIn("dir_audio_total", seg)
        self.assertIn('id="fb-include-sub"', seg)
        # 光算出来不行，必须真的拼进 innerHTML
        self.assertRegex(seg, r"box\.innerHTML\s*=\s*subHint\s*\+")

    def test_hint_button_checks_the_folder(self):
        seg = fn_body(self.JS, "paintList")
        self.assertIn("treeSetAll(rel, true)", seg)
        self.assertIn("syncTreeToFiles()", seg)

    def test_search_view_does_not_use_list_only_fields(self):
        """搜索结果是 {files, folders}，没有 dir_audio_total / dirs。

        这条是防回归：提示条一开始被我插到了 `paintSearch` 里，
        那里 `d` 根本不存在 —— 搜索会直接抛 ReferenceError。
        """
        seg = fn_body(self.JS, "paintSearch")
        self.assertTrue(seg, "找不到 paintSearch")
        self.assertNotIn("dir_audio_total", seg)
        self.assertNotIn("fb-include-sub", seg)

    def test_tree_empty_state_has_escape(self):
        """树结果为空时也要能一键退回目录视图。"""
        self.assertIn('id="fb-tree-clear"', self.JS)

class TreeSelectionSyncTest(unittest.TestCase):
    """目录树勾选与右侧文件勾选必须**永远一致**。

    用户反馈两条：
      1. 取消上一个文件夹的全选后，点另一个文件夹全选，会把上一个也勾上
      2. 右侧文件全选 / 部分全选，与左边目录树不同步

    根因是**两套状态 + 两套算法**：
      · `treePicked`（勾了哪些目录）由 `paintTree` 读
      · `checked`（勾了哪些文件）由 `refreshTreeChecks` 读
    两套算法必然算出不同结果；而且 `syncTreeToFiles` 里
    `refreshTreeChecks()` 被放在 `paintTreeFiles()` **之前**，
    刷新时读的目录索引还是上一轮的。

    现在：两边都走 `treeCover()`；树视图下按文件算；
    刷新顺序改为"先画列表（重建索引）→ 再刷树"。
    """

    @classmethod
    def setUpClass(cls):
        cls.JS = (_PROJ / "app" / "web" / "static" / "js" / "views.files.js"
                  ).read_text(encoding="utf-8")

    def test_refresh_happens_after_list_paint(self):
        seg = fn_body(self.JS, "syncTreeToFiles")
        self.assertTrue(seg, "找不到 syncTreeToFiles")
        # 只看"成功取到文件"那条主路径：空结果分支里先 refresh 是合理的
        k = seg.index("treeViewOn = true")
        tail = seg[k:]
        i_paint = tail.index("paintTreeFiles()")
        i_refresh = tail.index("refreshTreeChecks()")
        self.assertLess(i_paint, i_refresh,
                        "必须先 paintTreeFiles（重建索引）再刷新树")

    def test_both_paths_use_the_same_cover_function(self):
        for fn in ("refreshTreeChecks", "paintTree"):
            seg = fn_body(self.JS, fn)
            self.assertTrue(seg, f"找不到 {fn}")
            self.assertIn("treeCover(", seg, f"{fn} 必须用 treeCover 判断状态")
        # 旧索引读取方式不许再出现（那会和 treeCover 打架）
        seg = fn_body(self.JS, "refreshTreeChecks")
        self.assertNotIn("dirTotal.get", seg)

    def test_tree_view_cover_comes_from_files(self):
        seg = fn_body(self.JS, "coverFromList")
        self.assertTrue(seg, "找不到 coverFromFiles")
        self.assertIn("checked.has(", seg)
        # 树视图下没有文件就是"未选"，不能回退到 treePicked
        self.assertNotIn("treePicked.has(rel) ? 'all'", seg)

    def test_checking_a_folder_clears_manual_exclusions(self):
        seg = fn_body(self.JS, "treeSetAll")
        self.assertTrue(seg, "找不到 treeSetAll")
        self.assertIn("treeExcluded.delete", seg)

    def test_root_node_is_not_all_when_only_a_subfolder_picked(self):
        """根节点（rel=""）不能因为"子目录的文件全勾"就显示成全选。

        用户反馈："左侧选中某个文件夹后，上方的根目录也会被勾选（全选）"。
        原因：根节点的 rel 是空串，`if (rel && ...)` 的过滤对空串不生效，
        于是它统计的是**当前树结果的全部文件** —— 右侧全勾 → 根就"全选"。
        """
        cov = fn_body(self.JS, "coverFromList")
        self.assertTrue(cov, "找不到 coverFromFiles")
        self.assertIn("rel === '' && strictRoot", cov, "根节点必须单独判断")
        self.assertIn("treePicked.has('')", cov,
                      "只有显式勾了根目录才算全选")
        self.assertIn("? 'all' : 'some'", cov)

    def test_normal_list_mode_reflects_current_folder(self):
        """普通目录视图里点「全选本目录」，树里该目录也要变全选。

        用户反馈："使用右侧上方工具栏的全选本目录，依旧不与文件夹树状态同步"。
        原因：`treeCover` 在非树视图只按 `treePicked` 算，而"全选本目录"
        只改 `checked` —— 树自然不动。现在当前目录按**本层文件**算。
        """
        seg = fn_body(self.JS, "treeCover")
        self.assertTrue(seg, "找不到 treeCover")
        self.assertIn("listData && listData.files", seg)
        self.assertIn("rel === curRel", seg)
        self.assertIn("curRel.startsWith(rel + '/')", seg)

    def test_list_checkbox_refreshes_tree(self):
        """普通列表里勾单个文件也要刷新树（否则半选不会出现）。"""
        seg = fn_body(self.JS, "paintList")
        self.assertTrue(seg, "找不到 paintList")
        self.assertIn("refreshTreeChecks()", seg)

    def test_cancel_all_clears_folder_picks(self):
        seg = fn_body(self.JS, "paintSelbarFrom")
        self.assertTrue(seg, "找不到 paintSelbarFrom")
        k = seg.index("allBox.onchange")
        handler = seg[k:k + 900]
        self.assertIn("treePicked.clear()", handler,
                      "取消全选必须同时清掉目录勾选，否则下次勾别的会连带")
        self.assertIn("syncTreeToFiles()", handler)

