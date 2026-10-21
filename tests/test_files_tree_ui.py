"""目录树多选 / 布局的回归测试。

这组来自用户反馈："文件夹多选功能怪怪的，没有勾选框，界面也会出问题"。
两个问题都是真的：

  1. **看不到勾选框**：原来是个自绘的小方块 `opacity:0`，只有悬停才隐约出现，
     而且要按 Ctrl 点击整行才能选 —— 用户根本不知道可以多选。
     改成**真正的 `<input type="checkbox">`，一直可见，普通点击即可切换**。

  2. **目录树被裁掉**：`.fb-tree-wrap` 高度塌成 2px，整棵树看不见。
     原因是嵌套的 `flex:1` 在 `min-height:auto` 下按内容算高度，
     而内容自己又是 `overflow:hidden` 的 flex 项 —— 最后整列被裁没。
     实测把高度改成明确的 `calc(100vh - …)` + `min-height` 立刻恢复
     （60vh 试验：0 → 192px）。同一处的 `.fb-list` 也有这个病。

这些都是 CSS / DOM 契约，没有接口可测，所以在这里按源码断言锁住。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

# 项目根（不要写死本机路径 —— 别人 clone 到别的目录就跑不了了）
_PROJ = __import__('pathlib').Path(__file__).resolve().parent.parent


ROOT = _PROJ


class TreeMultiSelectTest(unittest.TestCase):

    JS = None
    CSS = None

    @classmethod
    def setUpClass(cls):
        cls.JS = (ROOT / "app" / "web" / "static" / "js"
                  / "views.files.js").read_text(encoding="utf-8")
        cls.CSS = (ROOT / "app" / "web" / "static" / "css"
                   / "app.css").read_text(encoding="utf-8")

    # ---------------------------------------------------------- 勾选框
    def test_uses_real_checkbox(self):
        """必须是真的 checkbox，且**一直可见**。

        原来用自绘方块 + `opacity:0`，用户看不到可以勾。
        """
        self.assertRegex(self.JS, r'<input type="checkbox" class="fb-pick"',
                         "应该用真正的 checkbox")

    def test_checkbox_is_always_visible(self):
        css = self.CSS
        m = re.search(r"\.fb-node \.fb-pick\{([^}]*)\}", css)
        self.assertTrue(m, "找不到 .fb-pick 样式")
        body = m.group(1)
        self.assertNotIn("opacity:0", body.replace(" ", ""),
                         "勾选框不该默认透明（那就是看不到勾选框的原因）")
        self.assertIn("accent-color", body, "用 accent-color 跟随主题")

    def test_plain_click_toggles_selection(self):
        """普通点击勾选框就能多选，不需要按 Ctrl。

        注意 `cb.onchange` 在文件里出现多次（文件行的复选框也叫 cb），
        所以必须定位到**树的那一处**（用 treePicked 作为锚点）。
        """
        i = self.JS.index("treePicked.add(rel)")
        seg = self.JS[max(0, i - 300):i + 200]
        self.assertIn("treePicked.add(rel)", seg)
        self.assertIn("treePicked.delete(rel)", seg)

    def test_clicking_checkbox_does_not_navigate(self):
        """点勾选框不能顺带进入那个目录。"""
        self.assertIn("e.stopPropagation()", self.JS)
        self.assertRegex(self.JS, r"classList\.contains\('fb-pick'\)\)\s*return",
                         "整行点击要忽略来自勾选框的事件")

    def test_ctrl_click_still_works(self):
        """保留 Ctrl/Cmd/Shift 点击整行的快捷多选。"""
        seg = self.JS[self.JS.index("n.onclick = (e) => {"):]
        seg = seg[:900]
        self.assertIn("e.ctrlKey", seg)
        self.assertIn("e.metaKey", seg)
        self.assertIn("e.shiftKey", seg)

    def test_checkbox_state_stays_in_sync(self):
        """Ctrl 点整行时，勾选框本身也要跟着变（不能只有高亮）。"""
        seg = self.JS[self.JS.index("e.ctrlKey"):]
        seg = seg[:400]
        # 现在走级联：先算目标状态，再把 cb.checked 设成它
        self.assertRegex(seg, r"cb\.checked\s*=\s*(?:next|treePicked\.has)",
                         "整行多选后要同步 checkbox.checked")

    def test_navigation_still_works(self):
        """普通点击目录名仍然是「进入该目录」（不能被多选抢走）。"""
        seg = self.JS[self.JS.index("n.onclick = (e) => {"):]
        seg = seg[:1200]
        self.assertIn("go(rel ?", seg)

    def test_paint_tree_bar_called_after_render(self):
        """重绘后要刷新多选条（否则勾选状态和提示对不上）。"""
        seg = self.JS[self.JS.index("function paintTree()"):]
        seg = seg[:seg.index("function paintTreeBar")]
        self.assertIn("paintTreeBar()", seg)

    # ---------------------------------------------------------- 布局
    def test_tree_wrap_has_explicit_height(self):
        """★ `.fb-tree-wrap` 必须有**明确高度**，不能只靠 flex:1。

        踩过：高度塌成 2px，整棵目录树被 `overflow:hidden` 裁掉，
        界面上完全看不到。
        """
        m = re.search(r"\.fb-tree-wrap\{([^}]*)\}", self.CSS)
        self.assertTrue(m, "找不到 .fb-tree-wrap 样式")
        body = m.group(1)
        self.assertIn("height:", body,
                      "必须给明确高度（flex:1 在这个嵌套链里会算成 0）")
        self.assertIn("calc(100vh", body, "用视口高度兜底")
        self.assertIn("min-height:", body, "还要有下限，别在小窗里太矮")

    def test_file_list_has_explicit_height(self):
        """`.fb-list` 同理 —— 文件列表也塌过一次。

        注意 `.fb-list` 有两条规则（基础 `position:relative` 和带高度的这条），
        所以要对**所有**匹配逐个看，只要有一条带 height 就算通过。
        """
        bodies = re.findall(r"\.fb-list\{([^}]*)\}", self.CSS)
        self.assertTrue(bodies, "找不到 .fb-list 样式")
        hit = [b for b in bodies if "height:" in b and "min-height:" in b]
        self.assertTrue(hit,
                        f"必须有一条 .fb-list 规则给出明确高度，实际只有 {bodies}")

    def test_tree_wrap_and_tree_do_not_both_set_width(self):
        """宽度/边框只由 wrap 负责，`.fb-tree` 只滚动。

        两边都定 width/flex/overflow 会打架，还会叠出双滚动条。
        """
        m = re.search(r"\.fb-tree-wrap \.fb-tree\{([^}]*)\}", self.CSS)
        self.assertTrue(m, "找不到 .fb-tree 的样式")
        body = m.group(1)
        self.assertIn("overflow:auto", body)
        self.assertNotIn("width:290px", body.replace(" ", ""),
                         ".fb-tree 不该再定一次宽度")

    def test_view_does_not_scroll_itself(self):
        """.view 不能自己 `overflow:auto`。

        它一滚动，高度就变成「由内容决定」，里面的 flex:1 子项全部塌成 0。
        各视图内部本来就有自己的滚动容器。
        """
        m = re.search(r"\n\.view\{([^}]*)\}", self.CSS)
        self.assertTrue(m, "找不到 .view 样式")
        self.assertNotIn("overflow:auto", m.group(1).replace(" ", ""),
                         ".view 不该自己滚动（会让内部 flex 子项塌掉）")




class TrashPanelParityTest(unittest.TestCase):
    """回收站面板与中转站对齐的几条机制(用户反馈"按钮没反应 / 界面不同步")。

    这些是"源码契约",没有接口可测,所以在这里按源码锁住:

      · 两个动作按钮必须标 `data-exclusive` —— `app.js` 靠这个属性在**别的任务
        在跑时提前禁用**并说明原因。不标的话按钮看着能点,点下去才撞 409,
        报出来还是"还原失败"(误导),用户的感觉就是"点了没响应"。
        中转站(`#st-restore` 等)一直是这么标的。
      · 还原 / 彻底删除都必须走 `App.runExclusive`(受独占位约束),
        而不是裸调 API。
      · 忙的时候要有禁用 + 文案,并且 `finally` 一定恢复。
      · 还原 / 删除之后**原地刷新列表**(`reload()`),不再关窗重开。
    """

    @classmethod
    def setUpClass(cls):
        js = (ROOT / "app" / "web" / "static" / "js"
              / "views.files.js").read_text(encoding="utf-8")
        i = js.index("async function openTrash")
        cls.PANEL = js[i:js.index("function crumb()", i)]

    def test_action_buttons_marked_exclusive(self):
        for bid in ("tr-restore", "tr-purge"):
            m = re.search(r'<button[^>]*id="%s"[^>]*>' % bid, self.PANEL)
            self.assertIsNotNone(m, f"没找到按钮 {bid}")
            self.assertIn("data-exclusive", m.group(0),
                          f"#{bid} 没标 data-exclusive:别的任务在跑时不会提前禁用,"
                          f"点下去才撞 409,提示还是'还原失败'"
                          f"(用户感觉就是'点了没反应')")

    def test_actions_go_through_run_exclusive(self):
        for call in ("runExclusive('trash', () => API.trashRestore(",
                     "runExclusive('trash', () => API.trashPurge("):
            self.assertIn(call, self.PANEL,
                          "还原 / 彻底删除必须走 App.runExclusive(独占位 + 明确提示)")

    def test_busy_state_disables_and_always_restores(self):
        seg = self.PANEL[self.PANEL.index("async function run("):]
        seg = seg[:seg.index("async function doRestore")]
        self.assertIn("busy = true", seg)
        self.assertIn("finally", seg)
        self.assertIn("busy = false", seg)
        self.assertIn("disabled = busy", self.PANEL,
                      "忙的时候按钮必须真的禁用")

    def test_refreshes_list_in_place(self):
        self.assertIn("async function reload(", self.PANEL)
        self.assertIn("await reload()", self.PANEL,
                      "还原后要原地刷新列表,不要关窗重开")


class SelbarLiveStateTest(unittest.TestCase):
    """选择栏的批量操作必须按**实时**选中集合干活。

    用户反馈:"清除候选区里面的文件后,文件目录里仍然标注在候选区内;
    文件夹树与文件目录、候选区总是显示不同步;复杂操作之后经常丢状态。"

    实测复现出来的确定性缺陷(无头浏览器,同一状态下 A/B 对照):

      · A:直接点「加入候选区」→ 提示"选中的文件都已经在候选区里了",
        候选区仍是 0 个(按钮上连数字徽标都没有)—— 用户看到的就是"点了没反应";
      · B:先让选择栏重建(取消勾选→再勾选)再点 → **成功**加入。

    原因:按钮的 onclick 捕获的是**渲染那一刻**的 `picked` 快照,而局部重绘
    `repaintSelbarOnly()` 只更新按钮外观(数字、disabled),**不重绑 handler** ——
    于是界面上留着一个"看着能用、其实绑着几轮之前空集合"的按钮。

    修法:handler 一律走 `selbarPicked()` 实时求值;`repaintSelbarOnly()` 也要
    把「加入候选区」的 disabled 与数字徽标一起刷新;候选区监听只在 `mount()`
    注册一次(写在 `render()` 里会每次重绘再注册一个,越积越多)。
    """

    @classmethod
    def setUpClass(cls):
        cls.JS = (ROOT / "app" / "web" / "static" / "js"
                  / "views.files.js").read_text(encoding="utf-8")

    def _positions(self, needle: str) -> list:
        out, pos = [], 0
        while True:
            k = self.JS.find(needle, pos)
            if k < 0:
                return out
            out.append(k)
            pos = k + 1

    def test_both_selbars_bind_stage_add(self):
        """选择栏有两套实现(普通列表 / 目录树结果视图),两套都要有入口。"""
        self.assertEqual(
            2, len(self._positions("bar.querySelector('#fb-stage-add').onclick")),
            "两套选择栏都应绑定「加入候选区」")

    def test_stage_add_uses_live_state(self):
        for k in self._positions("bar.querySelector('#fb-stage-add').onclick"):
            seg = self.JS[k:k + 1200]
            # 普通列表那套把实时取数包成了 `sel()`(`const sel = () => selbarPicked();`),
            # 树结果视图那套直接调 `selbarPicked()` —— 两者都是实时求值,都算合格。
            self.assertTrue("selbarPicked()" in seg or "sel()" in seg,
                            "「加入候选区」必须按此刻的选中集合算(实时)")
            self.assertNotIn("stagedDid.has(did(t.path))", seg,
                             "不能用渲染时的 picked/stagedDid 快照:"
                             "局部重绘不重绑 handler,快照一旦过期,"
                             "按钮就会看着能点、点下去什么也不做")

    def test_repaint_refreshes_stage_button(self):
        i = self.JS.index("function repaintSelbarOnly")
        seg = self.JS[i:self.JS.index("function paintSelbarFrom", i)]
        self.assertIn("bar.querySelector('#fb-stage-add')", seg,
                      "局部重绘必须把「加入候选区」的状态一起刷新")
        self.assertIn("addBtn.disabled = !fresh", seg,
                      "禁用状态要按\"去掉已在候选区的之后还剩几个\"算")
        self.assertIn("badge.textContent", seg,
                      "数字徽标也要跟着更新,否则按钮上会挂着上一轮的数字")

    def test_staging_listener_registered_once(self):
        self.assertEqual(1, self.JS.count("Staging.onChange("),
                         "候选区监听只能注册一次")
        i_mount = self.JS.index("function mount(root)")
        i_hook = self.JS.index("Staging.onChange(")
        i_render = self.JS.index("function render()")
        self.assertTrue(i_mount < i_hook < i_render,
                        "监听要在 mount() 里注册;写在 render() 里会每次重绘"
                        "再注册一个,越积越多")
        self.assertEqual(1, self.JS.count("addEventListener('staging:move'"),
                         "「执行移动」的事件监听也只能注册一次")


if __name__ == "__main__":
    unittest.main()
