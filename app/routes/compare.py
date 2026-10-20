"""compare.py —— 见 `app/main.py` 顶部导航索引。

路由函数体与拆分前**完全一致**，只是从 `@app.` 换成了 `@router.`。
共享 helper 在 `app/deps.py`。
"""

from __future__ import annotations

import argparse
import logging
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import (APIRouter, Body, Depends, Header, HTTPException,
                     Query, Request, Response)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)


from ..core import audio as A
from ..core import device as DEV
from ..core import matching as M
from ..core import paths as P
from ..core import plan as PLAN
from ..core import scanner as S
from ..core.config import (AppConfig, Settings, SourceConfig, SourceNotFound,
                          data_dir, default_db_path, get_secret, load_config,
                          resolve_source, save_config, save_secret, test_mode)
from ..core.db import Database
from ..core.jobs import JobBusy, get_jobs, init as init_jobs, rebind as rebind_jobs
from ..core.ops import Executor
from ..sources import available_types, manager
from ..sources.base import ReadOnlyError, SourceError

log = logging.getLogger("msh")

from ..core.guard import exclusive_guard

from ..deps import (
    as_dicts,
    as_list,
    _browse_cache_clear,
    _connect_tip,
    _content_clusters,
    _db_size,
    _device_view,
    _dir_summary,
    _disk_view,
    _folder_map,
    _folder_rows,
    _hash_groups,
    _reindex_after_transfer,
    _serve_cover_bytes,
    _source_view,
    _stable_hash,
    _step_fingerprint,
    _steps_fingerprint,
    _track_view,
    _tracks_capped,
    _tracks_for,
    _validate_source,
    _wanted_db_path,
    db,
    require_writable,
)


router = APIRouter()

@router.post("/api/compare")
def compare(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """跨库对比。

    大库上 `compare_sources` 要跑几十秒（匹配是 O(n·m) 级别的），
    所以和查重一样走**后台任务**：有进度、能取消、刷新不丢。
    `payload.wait=true` 走同步分支（脚本/测试用）。
    """
    from ..core.bgjob import run_in_background
    left_id = payload.get("left")
    right_id = payload.get("right")
    if not left_id or not right_id:
        raise HTTPException(400, "需要 left / right 两个数据源 id")
    if left_id == right_id:
        raise HTTPException(400, "两侧不能是同一个数据源")
    for sid in (left_id, right_id):
        resolve_source(sid)
    if payload.get("wait"):
        job = get_jobs().create("compare", total=100, exclusive=False)
        try:
            return {"sync": True, **_compare_core(payload, job)}
        finally:
            get_jobs().finish(job)
    job = run_in_background("compare", 100,
                            lambda j: _compare_core(payload, j))
    return {"job": job.id, "async": True,
            "hint": "对比已在后台开始；用 /api/jobs/{id} 查进度"}


def _compare_core(payload: Dict[str, Any], job=None) -> Dict[str, Any]:
    def step(pct: int, msg: str) -> None:
        if job is None:
            return
        job.total = 100
        job.done = max(0, min(100, int(pct)))
        job.message = msg
        job.touch()

    left_id = payload.get("left")
    right_id = payload.get("right")
    cfg = load_config()
    step(5, "读取左侧曲目…")
    lp, lcut, ltotal = _tracks_capped(left_id, payload.get("left_prefix", ""))
    if job is not None and job.cancel:
        return {"cancelled": True}
    step(25, "读取右侧曲目…")
    rp, rcut, rtotal = _tracks_capped(right_id, payload.get("right_prefix", ""))
    if job is not None and job.cancel:
        return {"cancelled": True}
    step(40, f"匹配中（左 {len(lp)} · 右 {len(rp)}）…")
    result = PLAN.compare_sources(
        {"id": left_id, "label": resolve_source(left_id).name, "tracks": lp},
        {"id": right_id, "label": resolve_source(right_id).name, "tracks": rp},
        cfg=cfg,
        options={"match_threshold": payload.get("match_threshold")},
    )
    step(95, "整理结果…")
    result["stats"]["left_total_all"] = ltotal
    result["stats"]["right_total_all"] = rtotal
    result["stats"]["scanned_left"] = len(lp)
    result["stats"]["scanned_right"] = len(rp)
    result["truncated"] = {"left": lcut, "right": rcut}
    if lcut or rcut:
        result["warnings"] = [
            f"曲目过多已截断（左 {len(lp)}/{ltotal} · 右 {len(rp)}/{rtotal}）。"
            f"建议先在侧栏选目录再对比，或调大「设置 → 单次对比上限」。"
        ]
    # ★ 把两侧数据源的**设备性质**告诉界面。
    #   对比结果里最容易被忽略的坑：一侧在内置盘、另一侧在 U 盘或网络位置，
    #   用户看到"计划生成好了"就点执行，然后卡几十分钟 ——
    #   其实该先知道"对面是慢盘，而且执行时别拔"。
    try:
        dev_notes: List[str] = []
        for sid, side in ((left_id, "左侧"), (right_id, "右侧")):
            s = manager.get(sid)
            if s is None:
                continue
            di = s.device_info()
            nm = getattr(getattr(s, "cfg", None), "name", "") or sid
            if di.get("removable"):
                dev_notes.append(
                    f"{side}「{nm}」是可移除存储（U 盘 / 移动硬盘）："
                    f"执行同步时**不要拔盘**，中途断开可能留下不完整的文件")
            if di.get("network"):
                dev_notes.append(f"{side}「{nm}」在网络位置上，读写较慢，"
                                 f"大批量同步会比较久")
            elif di.get("slow"):
                dev_notes.append(f"{side}「{nm}」是慢速盘，同步时会自动降低并发")
        # 去重：同一个源可能既 removable 又 slow，或者两侧是同一块盘
        seen_n: set = set()
        uniq: List[str] = []
        for n in dev_notes:
            if n not in seen_n:
                seen_n.add(n)
                uniq.append(n)
        if uniq:
            result["device_notes"] = uniq
            # 也并进 warnings，让只读 warnings 的地方也能看到（去重后合并）
            w = list(result.get("warnings") or [])
            for n in uniq:
                if n not in w:
                    w.append(n)
            result["warnings"] = w
    except Exception:
        pass
    return result


@router.post("/api/plan")
def make_plan(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    left_id, right_id = payload.get("left"), payload.get("right")
    if not left_id or not right_id or left_id == right_id:
        raise HTTPException(400, "需要两个不同的数据源 id")
    # ★ 生成方案要一致快照：别的流程正在移动/删除文件时算出来的方案是错的，
    #   用户照着它执行等于照着过期状态动手。忙就 409（全局处理器转）。
    get_jobs().ensure_free()
    cfg = load_config()
    ltracks, lcut, ltotal = _tracks_capped(left_id, payload.get("left_prefix", ""))
    rtracks, rcut, rtotal = _tracks_capped(right_id, payload.get("right_prefix", ""))
    left = {"id": left_id, "label": resolve_source(left_id).name, "tracks": ltracks}
    right = {"id": right_id, "label": resolve_source(right_id).name, "tracks": rtracks}
    options = {
        "copy_missing": payload.get("copy_missing", True),
        "mirror_delete": payload.get("mirror_delete", False),
        "direction": payload.get("direction", "left_to_right"),
        "subdir": payload.get("subdir", ""),
        "overwrite": payload.get("overwrite", False),
        "include_duplicates": payload.get("include_duplicates", False),
        "generate_conflicts": payload.get("generate_conflicts", False),
        "conflict_side": payload.get("conflict_side", "right"),
        "match_threshold": payload.get("match_threshold"),
        # 补齐时用「复制 → 校验 → 删源」的传输语义，而不是纯复制。
        # ★ 必须放进 options —— 只在下面 `if options.get("transfer")` 里读它、
        #   却不把它放进来，那个分支永远不会执行（我第一次就是这么写的，
        #   实测计划里还是 copy，等于没生效）。
        "transfer": bool(payload.get("transfer", False)),
        "preview": 300,
    }
    result = PLAN.build_plan(left, right, cfg=cfg, options=options)
    # ★ 「安全搬运」：把补齐用的 `copy` 步骤换成 `transfer`
    #   （复制 → 校验大小 → 再删源）。
    #
    #   纯 `copy` 的语义是"补齐"：缺的补过去、源那份留着，两边各一份。
    #   但很多时候用户的真实意图是**搬** —— 尤其是往 U 盘 / 网络位置搬，
    #   纯复制会悄悄把两边都占满，还得多做一步手工删源。
    #
    #   这里**只在计划阶段换 op**，执行仍走同一个 Executor，所以
    #   独占、进度、暂停/取消、步骤重放这些机制全都照旧。
    if options.get("transfer"):
        n_xfer = 0
        for st in (result.get("steps") or []):
            if st.get("op") == "copy":
                st["op"] = "transfer"
                st["reason"] = (str(st.get("reason") or "")
                                + "（安全搬运：校验通过后删源）").strip()
                n_xfer += 1
        if n_xfer:
            result["warnings"] = list(result.get("warnings") or []) + [
                f"已把 {n_xfer} 个「复制补齐」步骤改成「安全搬运」："
                f"先复制到目标、**校验大小一致之后**才删源那份（进回收站）。"
                f"任何一步失败，源都不会消失。"]
    result["options"] = options
    result["truncated"] = {"left": lcut, "right": rcut}
    result["totals"] = {"left": ltotal, "right": rtotal}
    # 只读保护提示
    warnings = list(result.get("warnings") or [])
    if lcut or rcut:
        warnings.append(
            f"曲目过多已截断（左 {len(ltracks)}/{ltotal} · 右 {len(rtracks)}/{rtotal}），"
            f"计划可能不完整。建议按目录分批处理，或调大「设置 → 单次对比上限」。")
    for sid, side in ((left_id, "左"), (right_id, "右")):
        sc = resolve_source(sid)
        if sc.read_only:
            warnings.append(f"{side}侧「{sc.name}」被标记为只读，写操作会被拒绝。")
    result["warnings"] = warnings
    # ★ 方案指纹：`/api/execute` 会拿它校验"要执行的就是用户确认过的那份方案"。
    #   必须放在**最后**算 —— 上面「安全搬运」把 copy 改成了 transfer，
    #   指纹要盖住改完之后的最终步骤。
    result["plan_hash"] = _steps_fingerprint(result.get("steps") or [])
    result["step_hashes"] = {str(st.get("id")): _step_fingerprint(st)
                             for st in (result.get("steps") or [])}
    return result


@router.post("/api/execute")
def execute_plan(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    steps = as_dicts(payload, "steps", "计划步骤")
    if not steps:
        raise HTTPException(400, "没有要执行的步骤")
    # 上限由服务端定；客户端只能往小了调（原来 max_steps 完全由客户端给）
    if len(steps) > min(int(payload.get("max_steps") or 20000), 20000):
        raise HTTPException(400, "步骤过多，请分批执行")
    execute = bool(payload.get("execute", False))
    if execute and not payload.get("confirm"):
        raise HTTPException(400, "执行写操作需要 confirm=true")
    # ★ 方案指纹校验。
    #
    #   `/api/plan` 和 `/api/execute` 是**两次独立请求**。用户看方案时库里是状态 A，
    #   点执行时可能已经是状态 B（另一个标签在扫描、同步工具刚跑过、文件被替换）。
    #   执行器按现在的磁盘状态动手，而用户以为自己批准的是 A。
    #
    #   带了 `plan_hash` 就校验；不一致 → 409 要求重新生成方案。
    #   不带则放行（向后兼容：老客户端 / 单独构造 steps 的调用方）。
    _expect = str(payload.get("plan_hash") or "")
    _hashes = payload.get("step_hashes") or {}
    if execute and isinstance(_hashes, dict) and _hashes:
        # 按**单步**比对：用户取消勾选是正常操作，不算"方案变了"；
        # 但这几步的内容（路径/体积/操作类型）必须和生成方案时一致。
        _bad = [str(st.get("id")) for st in steps
                if str(st.get("id")) in _hashes
                and str(_hashes[str(st.get("id"))]) != _step_fingerprint(st)]
        if _bad:
            raise HTTPException(
                409,
                f"有 {len(_bad)} 个步骤的内容和生成方案时不一致"
                f"（或曲库已变化）。请重新生成方案再执行 —— "
                f"基于过期快照执行可能搬错或删错文件。")
    elif execute and _expect:
        # 兼容只发整份哈希的老客户端（要求执行全部步骤）
        if _expect != _steps_fingerprint(steps):
            raise HTTPException(
                409,
                "曲库在这份方案生成之后发生了变化（或步骤被改动过），"
                "指纹不一致。请重新生成方案再执行 —— "
                "基于过期快照执行可能搬错或删错文件。")
    sources: Dict[str, Any] = {}
    for st in steps:
        for key in ("src", "dst"):
            sid = st.get(key)
            if sid and sid not in sources:
                sources[sid] = manager.get(sid)
    # 全部相关源都只读 → 直接 403（否则会返回一个"看着成功"的 200，
    # 而 results[].state 全是 blocked，用户以为做过什么）
    if execute:
        require_writable(*list(sources.keys()), what="执行计划")
    ex = Executor(sources, db() if execute else None,
                  default_policy=payload.get("policy") or "skip")
    # 写操作要独占：多页面同时执行会基于过期状态操作文件。
    # 预演（execute=False）是只读的，不占位，随便看。
    job = None
    if execute:
        try:
            job = get_jobs().create("sync", total=len(steps),
                                    owner=str(payload.get("client") or ""))
        except JobBusy as exc:
            raise HTTPException(409, str(exc))
    try:
        return ex.run(
            steps, execute=execute,
            policy=payload.get("policy") or "skip",
            verify=payload.get("verify", True),
            verify_hash=bool(payload.get("verify_hash")),
            hard_delete=bool(payload.get("hard_delete")),
            workers=int(payload.get("workers") or 3),
            job=job,
        )
    finally:
        if job is not None and job.state == "running":
            # Executor 正常收尾时会自己 finish；这里兜底，避免异常路径
            # 把独占位永远占住。
            get_jobs().finish(job)


@router.post("/api/duplicates")
def duplicates(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """单库查重（**后台任务**）。

    以前这里是同步跑完再返回：大库上要几十秒，界面只有一个转圈，
    不知道进度、也没法取消 —— 用户以为卡死就会刷新甚至强杀进程。
    现在改成后台任务：立刻返回 job 句柄，前端轮询进度，
    支持取消，而且关掉页面/刷新都不会中断。

    `hash_mode`: none | quick | full —— full 更准但要走流量，默认 quick。
    `payload.sources`: 要检查的数据源 id 列表；为空则检查全部。
    `payload.wait`: true 时同步等待结果（脚本/测试用；界面走后台）。
    """
    from ..core.bgjob import run_in_background

    def work(job):
        return _duplicates_core(payload, job)

    if payload.get("wait"):
        job = get_jobs().create("duplicates", total=100, exclusive=False)
        try:
            return {"sync": True, **_duplicates_core(payload, job)}
        finally:
            get_jobs().finish(job)
    job = run_in_background("duplicates", 100, work)
    return {"job": job.id, "async": True,
            "hint": "任务已在后台开始；用 /api/jobs/{id} 查进度，完成后结果显示在 result"}


def _duplicates_core(payload: Dict[str, Any], job=None) -> Dict[str, Any]:
    """查重的实际计算。`job` 用来报进度 / 响应取消。"""
    def step(pct: int, msg: str) -> None:
        if job is None:
            return
        job.total = 100
        job.done = max(0, min(100, int(pct)))
        job.message = msg
        job.touch()

    def stopped() -> bool:
        return bool(job is not None and job.cancel)

    sid_list = payload.get("sources") or [s.id for s in load_config().sources]
    within = payload.get("within", "same")     # same=单库内 | cross=跨库
    hash_mode = payload.get("hash_mode", "quick")
    threshold = float(payload.get("threshold") or 0.9)
    min_size = int(payload.get("min_size") or 0)
    cfg = load_config()

    step(3, "读取索引…")
    cap = max(100, int(cfg.settings.max_compare_tracks or 8000))
    pools: Dict[str, List[Dict[str, Any]]] = {}
    dup_truncated: Dict[str, Any] = {}
    for sid in sid_list:
        all_rows = [r for r in db().all_tracks(sid) if (r["size"] or 0) >= min_size]
        if len(all_rows) > cap:
            dup_truncated[sid] = {"used": cap, "total": len(all_rows)}
            all_rows = all_rows[:cap]
        pools[sid] = [M.rec(r) for r in all_rows]
    if stopped():
        return {"cancelled": True, "groups": [], "stats": {}}

    step(15, "分组比对…")
    groups: List[List[Dict[str, Any]]] = []
    if within == "cross" and len(pools) > 1:
        ids = list(pools)
        pairs: List[Any] = []
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                lp = M.pair_sources(pools[a], pools[b],
                                    tolerance=cfg.settings.duration_tolerance_sec,
                                    threshold=threshold)
                for m in lp["matches"]:
                    if m.score >= threshold:
                        pairs.append((m.left, m.right))
        # ★ 两两配对之后必须**合并成互不重叠的组**。
        #   3 个库以上时，同一份文件会出现在多个配对里
        #   （A+B、A+C、B+C 三对都含 A），各自成组的话
        #   "每组保留一份"就自相矛盾：实测会生成把同一文件删两次的计划，
        #   第二条报 `FileNotFoundError`（幻影错误），
        #   而且在一个组里选了保留的文件可能在另一个组里被删掉。
        #   合并后 3 份 = 一组 3 条成员，和单库查重的组形状一致，
        #   下游的 clusters / suggested_keep / 保留策略全都能直接复用。
        groups.extend(M.merge_matched_pairs(
            pairs, tolerance=cfg.settings.duration_tolerance_sec))
    else:
        for sid, rows in pools.items():
            found = M.group_duplicates(rows, tolerance=cfg.settings.duration_tolerance_sec,
                                       threshold=threshold)
            groups.extend(found)
    if stopped():
        return {"cancelled": True, "groups": [], "stats": {}}

    # 抽样/全量哈希，用来确认"到底是不是同一份文件"
    if hash_mode in ("quick", "full"):
        _hash_groups(groups, hash_mode, cfg, job=job,
                     on_progress=lambda p, m: step(20 + int(p * 0.6), m))
    if stopped():
        return {"cancelled": True, "groups": [], "stats": {}}

    step(85, "整理结果…")

    # 用哈希把"假重复"分成两档：完全一致 / 只是相似
    # 把"像"和"是同一份"分开：
    #   相似组（歌相同、可能不同音质）一个总体视图，
    #   组内再按内容指纹切成若干"完全一致子簇"——真正可以直接删的只有子簇内的多余份。
    out_groups = []
    for g in groups:
        # 给每条记录带上版本标记，界面才能显示"伴奏 / 重制版"这类徽标
        for r in g:
            if "_ver_tags" not in r:
                r["_ver_tags"] = P.version_tags(r.get("name") or "", r.get("title") or "", r.get("rel_path") or "")
        d = M.group_to_dict(g)
        # 组键必须带 source_id：dup_key 只由 标题+艺术家+时长+版本 组成，
        # 两个库里存在同一首歌时 key 完全相同，前端会把它当成同一组
        # （radio 组名相同 → 两份"保留谁"互相干扰）。
        # 单库查重时一组必然只属于一个源，所以加上源前缀是安全的。
        sid_of = (g[0].get("source_id") if g else "") or ""
        d["key"] = f"{sid_of}::{d['key']}"
        d["base_key"] = f"{sid_of}::{d['base_key']}"
        d["source_id"] = sid_of
        for item in d["items"]:
            item["version_tags"] = item.pop("_ver_tags", "")
        d["size_equal"] = len({r.get("size") for r in g}) == 1
        # 组内"内容完全一致"的 path 集合：清理时只动这个名单里的文件，
        # 不同音质的版本永远留给用户自己决定
        identical_paths = set()
        for cl in _content_clusters(g):
            if len(cl) >= 2 and all(_stable_hash(r) for r in cl):
                identical_paths.update(r.get("path") for r in cl)
        d["identical_paths"] = sorted(p for p in identical_paths if p)
        clusters = _content_clusters(g)
        d["clusters"] = clusters
        # 跨库查重时一组里会同时有两个源的文件，而 path 只在一部分源内部保证唯一
        # （两个库都可能是 D:\Music）。给每条记录补一个"数据集标识"，
        # 前端用它而不是 path 作为唯一键，否则两份文件会被当成同一份。
        for item in d["items"]:
            raw = item.get("path") or ""
            item["did"] = f"{item.get('source_id') or ''}::{raw}"
        for cl in d["clusters"]:
            for item in cl:
                if "did" not in item:
                    raw = item.get("path") or ""
                    item["did"] = f"{item.get('source_id') or ''}::{raw}"
        # suggested_keep 也必须用 did 的同一种写法：前端拿它和 items[].did 做相等比较，
        # 只给 path 的话界面永远匹配不上（每行都会显示"将移入回收站"）。
        by_path = {it.get("path"): it["did"] for it in d["items"]}
        if d.get("suggested_keep"):
            d["suggested_keep"] = by_path.get(d["suggested_keep"], d["suggested_keep"])
        verified = len(d["clusters"]) == 1 and len(g) > 1 and all(_stable_hash(r) for r in g)
        d["exact"] = bool(verified)
        d["hash_verified"] = bool(verified)
        d["verified_items"] = sum(1 for r in g if _stable_hash(r))
        # 真正能安全清理的体积：各子簇内只留一份后剩下的
        safe = 0
        for cl in clusters:
            if len(cl) < 2:
                continue
            keep = M.pick_representative(cl)
            safe += sum((r.get("size") or 0) for r in cl if r.get("path") != keep.get("path"))
        d["safe_wasted_bytes"] = safe
        # 这里必须按 did 比。上面刚把 suggested_keep 从 path 换成 did，
        # 这里还拿 path 去比，于是每一项都不相等，wasted_bytes 变成整组体积，
        # 连保留下来的那份也被算成浪费，界面的可清理体积和排序全跟着错。
        d["wasted_bytes"] = sum((r.get("size") or 0) for r in g
                                if (r.get("did") or r.get("path")) != d.get("suggested_keep"))
        out_groups.append(d)
    out_groups.sort(key=lambda d: (-d["wasted_bytes"], -d["count"]))
    # 给前端一组明确的口径，避免"看起来只有几组"这种误判：
    #   groups    = 总组数
    #   exact     = 组内含"字节完全一致"子簇的组数（可安全清理）
    #   similar   = 只是相似、需要人自己判断的组数
    exact_groups = sum(1 for d in out_groups
                       if any(len(c) >= 2 for c in (d.get("clusters") or [])))
    return {
        "groups": out_groups,
        "stats": {
            "groups": len(out_groups),
            "groups_total": len(out_groups),
            "groups_exact": exact_groups,
            "groups_similar": len(out_groups) - exact_groups,
            "files": sum(d["count"] for d in out_groups),
            "wasted_bytes": sum(d["wasted_bytes"] for d in out_groups),
            "safe_wasted_bytes": sum(d.get("safe_wasted_bytes") or 0 for d in out_groups),
            "exact_groups": exact_groups,
            "hash_mode": hash_mode,
            "sources": sid_list,
            "within": within,
            "truncated": dup_truncated,
        },
        "keep_policies": [
            {"id": "best_quality", "label": "保留音质最好的"},
            {"id": "largest", "label": "保留体积最大的"},
            {"id": "oldest", "label": "保留最早的"},
            {"id": "newest", "label": "保留最新的"},
            {"id": "manual", "label": "我自己挑"},
        ],
    }


@router.post("/api/duplicates/keep")
def set_keep(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """按策略给出每组"保留哪份"的建议，用户可在返回后手工覆盖。"""
    groups = as_dicts(payload, "groups", "查重分组", max_items=5000)
    policy = payload.get("policy") or load_config().settings.default_keep_policy
    # ★ 标识符必须和 /api/duplicates 返回的**完全一致**（用 did）。
    #
    #   踩过的坑：这里原来返回 `path`，而 `/api/duplicates` 返回的
    #   `items[].did` 是 `source_id::path`。前端拿 did 和 keep 做严格比较，
    #   永远不相等 → 用户明确选的"保留最早的那份"被**静默丢弃**，
    #   回落到按音质挑，于是删掉的可能是用户想留的那一份。
    def _key_of(r: Dict[str, Any]) -> str:
        return str(r.get("did") or r.get("path") or "")

    out: List[Dict[str, Any]] = []
    for g in groups:
        items = g.get("items") or []
        if not items:
            continue
        if policy == "largest":
            keep = _key_of(max(items, key=lambda r: (r.get("size") or 0)))
        elif policy == "oldest":
            keep = _key_of(min(items, key=lambda r: (r.get("mtime") or 0)))
        elif policy == "newest":
            keep = _key_of(max(items, key=lambda r: (r.get("mtime") or 0)))
        else:
            keep = _key_of(M.pick_representative(items))
        out.append({"key": g.get("key"), "keep": keep,
                    "trash": [_key_of(r) for r in items if _key_of(r) != keep],
                    "count": len(items)})
    return {"policy": policy, "selections": out}
