"""同步计划引擎：只生成"计划"，不直接动手。

三块能力：
* **查漏 (missing)**：A 有 B 没有（或反过来）→ 生成 copy 步骤，让用户勾选；
* **查重 (duplicates)**：同库/跨库同一首歌多份 → 生成 trash（进回收站）步骤，
  默认保留质量最好的那份，但用户随时可以改成别的；
* **改名冲突 (name_conflict)**：两边同一首歌文件名不同 → 可选 rename 统一命名。

每一步都带：风险等级、原因、体积；执行前必须显式传入 step id，默认永远不自动删。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import matching as M
from . import paths as P

RISK_INFO = {"high": 3, "medium": 2, "low": 1, "none": 0}


@dataclass
class Step:
    op: str                     # copy | trash | rename | link | mkdir
    src: str = ""               # 源数据源 id
    src_path: str = ""
    dst: str = ""               # 目标数据源 id
    dst_path: str = ""
    size: int = 0
    reason: str = ""
    risk: str = "low"
    kind: str = ""              # missing | duplicate | name_conflict | manual
    group_key: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    def to_dict(self) -> Dict[str, Any]:
        d = self.__dict__.copy()
        d["human_size"] = P.human_size(self.size)
        return d


def _t(r: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "path": r.get("path"), "name": r.get("name"), "size": r.get("size") or 0,
        "title": r.get("title"), "artist": r.get("artist"), "album": r.get("album"),
        "duration": r.get("duration"), "bitrate": r.get("bitrate"), "codec": r.get("codec"),
        "is_lossless": r.get("is_lossless"), "quality": r.get("quality"),
        "sample_rate": r.get("sample_rate"), "bits_per_sample": r.get("bits_per_sample"),
        "hash_quick": r.get("hash_quick"), "hash_state": r.get("hash_state"),
    }


# ------------------------------------------------------------------ 查漏

def missing_steps(matches: Sequence[M.Match], only_left: Sequence[Dict[str, Any]],
                  only_right: Sequence[Dict[str, Any]], left_id: str, right_id: str,
                  direction: str = "left_to_right", subdir: str = "",
                  overwrite: bool = False) -> List[Step]:
    """生成补齐步骤。`direction`: left_to_right | right_to_left。"""
    steps: List[Step] = []
    if direction == "left_to_right":
        src_id, dst_id, items = left_id, right_id, list(only_left)
        from_name, to_name = "左", "右"
    else:
        src_id, dst_id, items = right_id, left_id, list(only_right)
        from_name, to_name = "右", "左"

    for r in items:
        rel = r.get("rel_path") or P.basename(r.get("path") or "")
        if subdir:
            d = P.parent(rel)
            if d and not (d == subdir or d.startswith(subdir.rstrip("/") + "/")):
                continue
        # dst_path 用相对路径，由目标源的 root 决定实际落点，天然保持目录结构一致
        #
        # ★ 风险等级**不能**跟着 `overwrite` 变。
        #   原来写的是 `risk="low" if overwrite else "medium"` —— 而
        #   `overwrite` 在执行阶段**根本没人读**（真正决定同名怎么办的是
        #   `/api/execute` 的 `policy`）。后果是：用户勾上"覆盖"，
        #   风险徽标从「注意」变成「安全」，**实际行为一点没变**——
        #   等于用一个假的"安全"标签鼓励用户确认。
        #   现在统一按"这一步会不会碰到已存在的目标"给等级，
        #   勾选与否只写进 meta 供界面显示，不参与风险判断。
        steps.append(Step(
            op="copy", src=src_id, src_path=r.get("path") or "",
            dst=dst_id, dst_path=P.norm(rel), size=r.get("size") or 0,
            reason=f"{from_name}侧独有，{to_name}侧缺失",
            risk="medium",
            kind="missing", meta={"track": _t(r), "overwrite": bool(overwrite),
                                  "from_side": from_name, "to_side": to_name},
        ))
    return steps


# ------------------------------------------------------------------ 查重

def duplicate_steps(groups: Sequence[Dict[str, Any]], keep_map: Optional[Dict[str, str]] = None,
                    within: str = "same", trash_mode: str = "trash") -> List[Step]:
    """把重复组转成删除步骤。`keep_map`: group_key -> 要保留的 path。"""
    keep_map = keep_map or {}
    steps: List[Step] = []

    def _ident(x: Any) -> str:
        """把"保留谁"归一成一个可比较的字符串。

        ★ 踩过的坑：`suggested_keep` 在查重接口里已经被换成 `source::path` 形式，
        在 `build_plan` 的交叉重复组里又是**整个曲目 dict**。原来这里直接
        `item["path"] == keep` 比较 —— 字符串和 dict 比、裸路径和 `source::path` 比，
        **永远不相等**，于是"保留项"也会被生成一条 trash 步骤。
        那是一条会把用户想留的那份删掉的步骤。
        """
        if isinstance(x, dict):
            return str(x.get("did") or x.get("path") or "")
        s = str(x or "")
        if "::" in s or not s:
            return s
        return s

    for g in groups:
        gkey = g.get("key") or ""
        keep = _ident(keep_map.get(gkey) or g.get("suggested_keep"))
        for item in g.get("items", []):
            if _ident(item) == keep or (keep and str(item.get("path") or "") == keep):
                continue
            steps.append(Step(
                op="trash" if trash_mode == "trash" else "delete",
                src=item.get("source_id") or "", src_path=item.get("path") or "",
                dst=item.get("source_id") or "", dst_path=item.get("path") or "",
                size=item.get("size") or 0,
                reason=f"与保留版本重复：{P.basename(keep) if keep else ''}"
                       f"（质量 {(item.get('quality') or 0):.1f} < 保留 {(g.get('best_quality') or 0):.1f}）",
                risk="high",
                kind="duplicate", group_key=gkey,
                meta={"keep": keep, "title": g.get("title"), "artist": g.get("artist"),
                      "trash_mode": trash_mode, "group_count": g.get("count")},
            ))
    return steps


# ------------------------------------------------------------------ 改名冲突

def conflict_steps(conflicts: Sequence[Dict[str, Any]], left_id: str, right_id: str,
                   apply_to: str = "right") -> List[Step]:
    """同一首歌两边文件名不同 → 把一侧改成另一侧的名字。"""
    steps: List[Step] = []
    for c in conflicts:
        l, r = c["left"], c["right"]
        if apply_to == "right":
            sid, cur, want = right_id, r.get("path"), l.get("name")
        else:
            sid, cur, want = left_id, l.get("path"), r.get("name")
        if not cur or not want or P.basename(cur) == want:
            continue
        steps.append(Step(
            op="rename", src=sid, src_path=cur, dst=sid,
            dst_path=P.join(P.parent(cur), want),
            size=c["left"].get("size") or 0,
            reason=f"统一命名：{P.basename(cur)} → {want}",
            risk="medium", kind="name_conflict",
            meta={"from": P.basename(cur), "to": want, "track": _t(l)},
        ))
    return steps


# ------------------------------------------------------------------ 计划封装

def build_plan(left: Dict[str, Any], right: Dict[str, Any],
               cfg=None, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """左右两个库的整体对比计划。

    left/right: {"id":..., "label":..., "tracks":[...]}
    options: copy_missing / mirror_delete / only_left_to_right / subdir /
             overwrite / include_duplicates / keep_map / generate_conflicts
    """
    opts = options or {}
    tol = float(getattr(getattr(cfg, "settings", None), "duration_tolerance_sec", 2.0) or 2.0)
    thr = float(getattr(getattr(cfg, "settings", None), "name_similarity", 0.86) or 0.86)
    # ★ 阈值语义修正：调用方显式传了就用它；没传才用用户设置
    #   （`name_similarity`）。原来的 `min(0.62, thr)` 是反的 ——
    #   用户把 name_similarity 调大（要求更严）时被 0.62 压住，设置等于没有。
    if opts.get("match_threshold") is not None:
        match_threshold = float(opts["match_threshold"])
    else:
        match_threshold = thr

    lp = M.pair_sources(left["tracks"], right["tracks"], tolerance=tol,
                        threshold=match_threshold)
    steps: List[Step] = []
    warnings: List[str] = []

    # 1) 查漏 → 补齐
    if opts.get("copy_missing", True):
        direction = opts.get("direction") or "left_to_right"
        # ★ 方向值必须**白名单校验**，不能"不认识的都当 right_to_left"。
        #   原来 `missing_steps` 是二选一分支（`if direction == "left_to_right" … else …`），
        #   于是任何拼错的值（`left-right`、`BOTH`、`""`）都会**静默反转方向** ——
        #   用户以为在"左→右补齐"，实际把右库的东西往左库搬。
        if direction not in ("left_to_right", "right_to_left", "both"):
            warnings.append(
                f"不认识的补齐方向 {direction!r}，已按「左 ➜ 右」处理。")
            direction = "left_to_right"
        if direction == "both":
            steps += missing_steps(lp["matches"], lp["only_left"], lp["only_right"],
                                   left["id"], right["id"], "left_to_right",
                                   opts.get("subdir", ""), opts.get("overwrite", False))
            steps += missing_steps(lp["matches"], lp["only_left"], lp["only_right"],
                                   left["id"], right["id"], "right_to_left",
                                   opts.get("subdir", ""), opts.get("overwrite", False))
        else:
            steps += missing_steps(lp["matches"], lp["only_left"], lp["only_right"],
                                   left["id"], right["id"], direction,
                                   opts.get("subdir", ""), opts.get("overwrite", False))

    # 2) 镜像删除：右有左无 → 从右删
    #
    # ★★ 镜像删除只对"**右 ➜ 左不补**"的方向成立。
    #
    #   这条判据原来**完全不看 direction**，于是 `right_to_left` 或 `both` +
    #   镜像时，同一个右库文件会同时拿到两条步骤：
    #       copy  R → L      （第 1 步生成）
    #       trash R          （这里生成）
    #   而 `/api/execute` 用 `ThreadPoolExecutor(workers=3)` **并发**跑步骤、
    #   互相之间没有顺序依赖（`_reserve_targets` 只占 copy/move/rename/
    #   transfer 的目标，trash 不在其中）。trash 先跑完的话，
    #   文件进了回收站、**却从来没有被复制到左边** ——
    #   界面上两条都报成功，用户以为"补齐好了"。
    #
    #   语义上也自相矛盾：一边要把右库独有的文件搬到左边，
    #   一边又要把它从右边删掉。所以直接在源头把组合变成"镜像优先、
    #   跳过右➜左补齐"，并**明确告诉用户**发生了什么。
    _mirror = bool(opts.get("mirror_delete"))
    _dir_now = (opts.get("direction") or "left_to_right")
    _mirror_conflict = _mirror and opts.get("copy_missing", True) \
        and _dir_now in ("right_to_left", "both")
    if _mirror_conflict:
        # 摘掉刚刚生成的"右 ➜ 左"补齐步骤（保留左 ➜ 右那部分，
        # 因为镜像只删右库独有的文件，不影响左库往右补）。
        _before = len(steps)
        steps = [s for s in steps
                 if not (s.op == "copy" and s.src == right["id"]
                         and s.dst == left["id"])]
        _dropped = _before - len(steps)
        if _dropped:
            warnings.append(
                f"镜像模式会删除右库独有的文件，所以已跳过 {_dropped} 个"
                f"「右 ➜ 左」补齐步骤（否则同一个文件会一边被复制、"
                f"一边被删除，可能两头落空）。想保留那些文件，"
                f"请关掉镜像模式。")
    if _mirror:
        for r in lp["only_right"]:
            rel = r.get("rel_path") or P.basename(r.get("path") or "")
            steps.append(Step(
                op="trash", src=right["id"], src_path=r.get("path") or "",
                dst=right["id"], dst_path=r.get("path") or "",
                size=r.get("size") or 0,
                reason="镜像模式：右库存在但左库没有",
                risk="high", kind="missing", meta={"track": _t(r), "mirror": True},
            ))
        warnings.append("已启用镜像删除：右库中左库没有的文件会被移入回收站，请仔细核对。")

    duplicate_groups: List[Dict[str, Any]] = []

    # 3) 重复（跨库同名同歌多份）
    if opts.get("include_duplicates"):
        cross = [
            {"items": [m.left, m.right], "key": f"cross::{m.left.get('path')}",
             "title": m.left.get("title"), "artist": m.left.get("artist"),
             "count": 2, "suggested_keep": (m.left if (m.left.get("quality") or 0) >= (m.right.get("quality") or 0) else m.right).get("path"),
             "best_quality": max(m.left.get("quality") or 0, m.right.get("quality") or 0)}
            for m in lp["matches"] if m.identical and not opts.get("mirror_delete")
        ]
        if cross:
            # ★ 这里以前**只加一条 warning**，`cross` 算完就丢了。
            #   而对比页那个勾选项写着"同时列出两侧内容完全一致的重复（可清理其中一份）"
            #   —— 用户勾上之后除了多一行提示，方案里一条对应步骤都没有。
            #   现在真的转成 trash 步骤（进回收站，可恢复），并在返回值里带上分组，
            #   前端才能显示"哪几组、建议保留谁"。
            _keep_map = {}
            for _g in cross:
                _k = _g.get("suggested_keep")
                if isinstance(_k, dict):
                    _keep_map[_g["key"]] = _k.get("did") or _k.get("path") or ""
                elif _k:
                    _keep_map[_g["key"]] = str(_k)
            dup = duplicate_steps(cross, keep_map=_keep_map,
                                  within="cross", trash_mode="trash")
            steps += dup
            duplicate_groups = cross
            warnings.append(
                f"发现 {len(cross)} 组两侧内容完全相同的重复文件，"
                f"已生成 {len(dup)} 条「移入回收站」步骤（可恢复）；"
                f"默认保留每份质量更高的那一侧。")

    # 4) 命名冲突
    if opts.get("generate_conflicts"):
        conf = M.naming_conflicts(left["tracks"], right["tracks"])
        steps += conflict_steps(conf, left["id"], right["id"], opts.get("conflict_side", "right"))

    summary = _summarize(steps)
    return {
        "generated_at": time.time(),
        "left": {"id": left["id"], "label": left.get("label"), "total": len(left["tracks"])},
        "right": {"id": right["id"], "label": right.get("label"), "total": len(right["tracks"])},
        "match_threshold": match_threshold,
        "stats": lp["stats"],
        "duplicate_groups": duplicate_groups,
        "matches": [m.to_dict() for m in lp["matches"][: int(opts.get("match_preview", 500))]],
        "only_left": [_t(r) for r in lp["only_left"][: int(opts.get("preview", 500))]],
        "only_right": [_t(r) for r in lp["only_right"][: int(opts.get("preview", 500))]],
        "conflicts": M.naming_conflicts(left["tracks"], right["tracks"])[: int(opts.get("preview", 500))],
        "steps": [s.to_dict() for s in steps],
        "summary": summary,
        "warnings": warnings,
    }


def _summarize(steps: Sequence[Step]) -> Dict[str, Any]:
    by_op: Dict[str, Dict[str, Any]] = {}
    for s in steps:
        b = by_op.setdefault(s.op, {"count": 0, "bytes": 0, "risk_max": "none"})
        b["count"] += 1
        b["bytes"] += s.size
        if RISK_INFO[s.risk] > RISK_INFO[b["risk_max"]]:
            b["risk_max"] = s.risk
    return {
        "total_steps": len(steps),
        "by_op": by_op,
        "copy_bytes": sum(s.size for s in steps if s.op == "copy"),
        "trash_bytes": sum(s.size for s in steps if s.op in ("trash", "delete")),
        "high_risk": sum(1 for s in steps if s.risk == "high"),
    }


# ------------------------------------------------------------------ 界面用

def compare_sources(left: Dict[str, Any], right: Dict[str, Any], cfg=None,
                    options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """双栏视图的数据：一对一配对 + 两侧独有 + 统计。"""
    opts = options or {}
    tol = float(getattr(getattr(cfg, "settings", None), "duration_tolerance_sec", 2.0) or 2.0)
    thr = float(opts.get("match_threshold") or 0.62)
    lp = M.pair_sources(left["tracks"], right["tracks"], tolerance=tol, threshold=thr)
    limit = int(opts.get("limit") or 0)
    matches = [m.to_dict() for m in lp["matches"]]
    if limit:
        matches = matches[:limit]
    return {
        "stats": lp["stats"],
        "matches": matches,
        "only_left": [M.rec(r) for r in lp["only_left"]],
        "only_right": [M.rec(r) for r in lp["only_right"]],
    }

