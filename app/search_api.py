"""搜索 / 集合 / 智能整理的路由。

三块能力共用一处，因为它们都建立在同一个基础上："先在库里按模糊条件找出
一批曲目，再对这批曲目做点什么"（看、集合成一个新文件夹、或按规则重新归位）。

安全取向与其它路由一致：
* 只读的搜索随便用；**任何写操作**都要求路径在数据源根目录内、只读源直接拒绝；
* 整理先出计划（`/api/organize/plan`），确认后才执行（`/api/organize/apply`），
  并且执行走的是同一套 Executor，继承"跳过/改名/覆盖 + 移动后校验"的全部保护。
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Depends, HTTPException

from . import mappings_api as MA
from .core import organize as OG
from .core import paths as P
from .core.guard import exclusive_guard

from .deps import as_list, as_text


def _small_policy(payload: Dict[str, Any]) -> str:
    """小分组怎么处理：`keep`（保留平铺）/ `sink`（归入收纳区）。

    兼容旧入参：显式 `keep_flat_below=False` 等价于 `sink`。
    """
    sp = str(payload.get("small_policy") or "").strip().lower()
    if sp in ("keep", "sink"):
        return sp
    if payload.get("keep_flat_below") is False:
        return "sink"
    return "keep"


router = APIRouter()

# 路径命中要打折：目录名往往被很多首歌共享（一个 `日语歌精选集` 目录下 375 首），
# 让路径命中排在"歌名/作者"命中之后，搜单曲时才不会被整目录刷屏。
PATH_WEIGHT = 0.8


def _deps():
    from . import main
    return main


def _resolve_src(sid: str):
    """(source 实例, 根路径)；顺带校验数据源存在。"""
    from .core.config import SourceNotFound
    m = _deps()
    try:
        m.resolve_source(sid)
    except (SourceNotFound, KeyError):
        # `SourceNotFound` 是现在的类型；`KeyError` 留着兼容旧调用点
        raise HTTPException(404, f"数据源不存在: {sid}") from None
    src = m.manager.get(sid)
    root = P.norm(src.root)
    if not root:
        raise HTTPException(400, "该数据源没有设置根目录")
    return src, root


def _guard_write(src) -> None:
    if not src.can_write:
        raise HTTPException(403, f"数据源「{src.cfg.name}」已设为只读，拒绝写入操作")


def _abs_in_root(sid: str, raw: str) -> str:
    """把用户给的目录/路径规整为根目录内的绝对内部路径。

    同时接受两种写法：
      * 绝对（`D:/Music/0单曲`）—— 界面上的文件夹选择器给的就是这种
      * 相对数据源根（`0单曲`、`日语歌精选集/子目录`）—— 计划/预览接口
        返回的目录名是这种，界面把它原样回传，所以必须支持。
    """
    _src, root = _resolve_src(sid)
    if not raw or not str(raw).strip():
        raise HTTPException(400, "缺少路径")
    p = P.norm(str(raw))
    # 越界检查必须在"拼接到根目录"**之前**做：`P.join` 会把 `..` 折叠掉，
    # 拼完再查就什么也查不到了（`../etc` 会变成根目录的兄弟目录）。
    parts = [x for x in p.split("/") if x]
    if ".." in parts:
        raise HTTPException(400, "路径不允许包含 ..")
    if not P.is_windows_abs(p) and not p.startswith("/"):
        # 相对路径：拼到数据源根下面
        p = P.join(root, p)
    if not (p == root or p.startswith(root.rstrip("/") + "/")):
        raise HTTPException(400, "路径超出数据源根目录，已拒绝")
    return p


def _track_rows(sid: str, prefix: str = "") -> List[Dict[str, Any]]:
    """取索引里的曲目（可选限定在某个目录子树内）。"""
    m = _deps()
    db = m.db()
    if prefix:
        rows = db.tracks(sid, prefix=prefix)
    else:
        rows = db.all_tracks(sid)
    return [dict(r) for r in rows]


# ==================================================================== 搜索

@router.get("/api/search")
def search(sid: str, q: str = "", scope: str = "all", path: str = "",
           kind: str = "track", limit: int = 60, min_score: float = 0.0) -> Dict[str, Any]:
    """模糊搜索曲目 / 文件夹。

    * `kind=track`：匹配 文件名 / 标题 / 作者 / 专辑 / 路径，容忍错别字与缩写
    * `kind=folder`：匹配目录名与完整相对路径
    * `scope=here` + `path=`：只在这个目录子树里找（文件浏览页"在当前文件夹内搜"）
    * `scope=all`：整库找（默认）

    归一化里会把 `01 -`、`(320k)`、`[FLAC]` 这类噪声去掉，所以搜"晴天"能命中
    "01 - 周杰伦 - 晴天 (320k).flac"。
    """
    src, root = _resolve_src(sid)
    q = (q or "").strip()
    if not q:
        return {"source": sid, "query": q, "kind": kind, "scope": scope,
                "track_total": 0, "folder_total": 0, "tracks": [], "folders": []}

    prefix = ""
    if scope == "here" and path:
        prefix = _abs_in_root(sid, path)

    cap = max(1, min(500, int(limit)))
    needle = P.normalize_text(q)

    tracks_out: List[Dict[str, Any]] = []
    if kind in ("track", "both"):
        rows = _track_rows(sid, prefix)
        for r in rows:
            name = r.get("name") or ""
            # 按字段分别打分，而不是拿整个文件名去比：
            # 1) 让"歌名/作者"命中优先于"路径"命中——否则在一个 `日语歌精选集`
            #    目录里搜"日语"，会把里面 375 首歌全当成命中，全是噪音；
            # 2) 短查询 vs 长文件名直接算相似度会被拉低（"青花词" vs
            #    "周杰伦 - 青花瓷.flac" 只有 0.27），错别字就搜不出来。
            f_title = P.fuzzy_match(q, r.get("title") or "")
            f_name = P.fuzzy_match(q, name)
            f_artist = P.fuzzy_match(q, r.get("artist") or "")
            f_album = P.fuzzy_match(q, r.get("album") or "")
            f_path = P.fuzzy_match(q, r.get("rel_path") or "", r.get("path") or "")
            fields = {"标题": f_title, "文件名": f_name, "作者": f_artist,
                      "专辑": f_album, "路径": f_path}
            core = max(f_title, f_name, f_artist, f_album)
            # 路径命中打 0.8 折，只有在没有别的东西命中时才靠它兜底
            score = max(core, f_path * PATH_WEIGHT)
            if score <= 0 or score < float(min_score or 0):
                continue
            tracks_out.append({
                "id": r.get("id"), "did": f"{sid}::{r.get('path')}",
                "source_id": sid, "path": r.get("path"), "name": name,
                "title": r.get("title"), "artist": r.get("artist"),
                "album": r.get("album"), "size": r.get("size"),
                "duration": r.get("duration"), "codec": r.get("codec"),
                "quality": r.get("quality"), "is_lossless": r.get("is_lossless"),
                "has_cover": r.get("has_cover"), "mtime": r.get("mtime"),
                "cover_url": f"/api/cover/{r.get('id')}?size=160" if r.get("cover_hash") else None,
                "human_size": P.human_size(r.get("size")),
                "score": score,
                # 告诉界面"凭什么被搜出来"，命中路径和命中歌名不是一回事
                "hit": [k for k, v in fields.items() if v > 0],
                "matched_in": max(fields, key=lambda k: fields[k]),
                "only_path": core <= 0,
                "folder": P.parent(P.norm(r.get("path") or "")),
                "rel_folder": P.parent(r.get("rel_path") or ""),
            })
        tracks_out.sort(key=lambda x: (x["only_path"], -x["score"], str(x["name"]).lower()))

    folders_out: List[Dict[str, Any]] = []
    if kind in ("folder", "both"):
        m = _deps()
        fm = m._folder_map(sid, root)
        for path_key, node in fm.items():
            rel = node.get("rel") or ""
            if not rel:
                continue
            if prefix and not (P.norm(path_key) == prefix
                               or P.norm(path_key).startswith(prefix.rstrip("/") + "/")):
                continue
            leaf = rel.rsplit("/", 1)[-1]
            score = max(P.fuzzy_match(q, leaf), P.fuzzy_match(q, rel) * 0.95)
            if score <= 0 or score < float(min_score or 0):
                continue
            folders_out.append({
                "path": node["path"], "rel": rel, "name": leaf,
                "count": node.get("count_all", 0), "count_here": node.get("count", 0),
                "bytes": node.get("bytes_all", 0),
                "human_bytes": P.human_size(node.get("bytes_all", 0)),
                "score": score,
            })
        folders_out.sort(key=lambda x: (-x["score"], -x["count"], x["rel"].lower()))

    return {
        "source": sid, "query": q, "kind": kind, "scope": scope, "path": prefix,
        "track_total": len(tracks_out), "folder_total": len(folders_out),
        "tracks": tracks_out[:cap], "folders": folders_out[:cap],
    }


# ==================================================================== 集合文件夹

@router.post("/api/collect/preview")
def collect_preview(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """集合前的检查：目标目录会有几个同名冲突、总共多大。"""
    sid = payload.get("source")
    if not sid:
        raise HTTPException(400, "缺少 source")
    src, root = _resolve_src(sid)
    name = as_text(payload, "name", "文件名").strip()
    parent = payload.get("parent")
    parent_abs = _abs_in_root(sid, parent) if parent else root
    dest = P.join(parent_abs, name) if name else parent_abs

    paths = [p for p in as_list(payload, "paths", "曲目路径") if p]
    if not paths:
        raise HTTPException(400, "没有选中任何曲目")
    total = 0
    conflicts: List[Dict[str, Any]] = []
    missing: List[str] = []
    for p in paths:
        ap = _abs_in_root(sid, p)
        st = src.stat(ap)
        if st is None:
            missing.append(ap)
            continue
        total += st.size or 0
        target = P.join(dest, P.basename(ap))
        if src.exists(target):
            conflicts.append({"path": ap, "target": target})
    return {
        "ok": True, "source": sid, "dest": dest,
        "dest_rel": dest[len(root) + 1:] if dest.startswith(root + "/") else dest,
        "dest_exists": bool(src.exists(dest)),
        "count": len(paths) - len(missing), "bytes": total,
        "human_bytes": P.human_size(total),
        "conflicts": conflicts, "missing": missing,
    }


@router.post("/api/collect/apply")
def collect_apply(payload: Dict[str, Any] = Body(...), exclusive: None = Depends(exclusive_guard)) -> Dict[str, Any]:
    """集合：把选中的曲目移动到一个（可新建的）目录里。

    `name` 给出时会在 `parent` 下建这个子目录；不给就表示"移动进 parent 本身"。
    不删任何东西——只搬。
    """
    sid = payload.get("source")
    if not sid:
        raise HTTPException(400, "缺少 source")
    src, root = _resolve_src(sid)
    _guard_write(src)
    m = _deps()

    parent = payload.get("parent")
    parent_abs = _abs_in_root(sid, parent) if parent else root
    name = _seg(payload.get("name"))
    dest = P.join(parent_abs, name) if name else parent_abs
    if not (dest == root or dest.startswith(root.rstrip("/") + "/")):
        raise HTTPException(400, "目标路径超出数据源根目录，已拒绝")

    paths = [p for p in as_list(payload, "paths", "曲目路径") if p]
    if not paths:
        raise HTTPException(400, "没有选中任何曲目")
    if len(paths) > 20000:
        raise HTTPException(400, "一次集合的曲目过多，请分批处理")

    execute = bool(payload.get("execute", False))
    if execute and not payload.get("confirm"):
        raise HTTPException(400, "执行写操作需要 confirm=true")
    policy = str(payload.get("policy") or "skip")
    if policy not in ("skip", "rename", "overwrite"):
        raise HTTPException(400, f"不支持的同名冲突策略: {policy}")

    plan: List[Dict[str, Any]] = []
    for i, p in enumerate(paths):
        ap = _abs_in_root(sid, p)
        st = src.stat(ap)
        if st is None:
            continue
        plan.append({
            "id": f"collect-{i}", "op": "move", "kind": "collect",
            "src": sid, "src_path": ap, "dst": sid,
            "dst_path": P.join(dest, P.basename(ap)),
            "size": st.size or 0,
        })
    if not plan:
        raise HTTPException(400, "选中的文件都不存在了")

    if not execute:
        # 预演也要真的建目录吗？不 —— 预演只报计划，不落任何东西
        ex = m.Executor({sid: src}, None, default_policy=policy)
        res = ex.run(plan, execute=False, policy=policy)
        return {"dry_run": True, "dest": dest, "policy": policy,
                "results": res.get("results", []), "summary": res.get("summary", {})}

    # 真正执行时先建目录（空目录也算"集合"的产物）
    try:
        src.mkdirs(dest)
    except Exception as exc:
        raise HTTPException(400, f"创建目录失败 {dest}: {exc}")

    ex = m.Executor({sid: src}, m.db(), default_policy=policy)
    res = ex.run(plan, execute=True, policy=policy, verify=True)
    summary = dict(res.get("summary") or {})
    return {"dry_run": False, "dest": dest, "policy": policy,
            "dest_rel": dest[len(root) + 1:] if dest.startswith(root + "/") else dest,
            "results": res.get("results", []), "summary": summary,
            "moved": summary.get("done", 0), "failed": summary.get("error", 0)}


def _seg(name: Any) -> str:
    s = str(name or "").strip()
    for ch in '\\/:*?"<>|':
        s = s.replace(ch, "_")
    return s.strip().strip(".")


# ==================================================================== 智能整理

@router.get("/api/organize/preview")
def organize_preview(sid: str, limit: int = 60) -> Dict[str, Any]:
    """现状体检：识别出的收纳区、目录画像、以及"疑似放错位置"的歌。"""
    _src, root = _resolve_src(sid)
    rows = _track_rows(sid)
    folders = OG.stats_by_folder(rows, root)
    sinks = OG.find_sinks(rows, root)
    return {
        "source": sid, "root": root, "total": len(rows),
        "folders": len([f for f in folders if f["folder"]]),
        "sinks": sinks,
        "top": folders[:int(limit)],
        # "≥5 首自动合并"真正该管的场景：名字只差一点、内容也重合的重复目录
        "merge_suggestions": OG.suggest_folder_merges(rows, root),
        # 界面上给用户一个"一键勾选"的入口，不用自己敲目录名
        "sink_candidates": [f["folder"] for f in folders
                            if f["folder"] and (f["sink"] or f["count"] >= 20)][:40],
        # 带曲目数的候选，供界面做成"和忽略范围一样"的复选列表
        "sink_options": [{"folder": f["folder"], "count": f["count"],
                          "sink": bool(f["sink"])}
                         for f in folders
                         if f["folder"] and (f["sink"] or f["count"] >= 20)][:40],
    }


@router.get("/api/organize/analyze")
def organize_analyze(sid: str, path: str = "", q: str = "", limit: int = 20) -> Dict[str, Any]:
    """搜索 + 结合现有文件夹分析："这首歌应该放哪？"

    这是"排错分配"的核心接口：给一首歌（或一段关键词），
    **在库里现有的文件夹之间找它该去的地方**，并给出证据。

    `q`    关键词（歌名/作者/专辑/文件名，支持模糊）；或
    `path` 直接指定一首歌的路径

    返回这首歌的标签、判定结论，以及按证据强度排序的候选文件夹。
    一条候选的 `score` 反映"有多少条独立证据支持它"：
      4  目录名就是这首歌的专辑
      3  目录里已有同专辑的歌
      2  目录名就是这首歌的作者
      1  目录里已有同作者的歌
    """
    _src, root = _resolve_src(sid)
    rows = _track_rows(sid)
    folders = OG.build_folders(rows, root)
    sinks = set(OG.find_sinks(rows, root))

    target: Optional[Dict[str, Any]] = None
    others: List[Dict[str, Any]] = []
    if path:
        want = _abs_in_root(sid, path)
        target = next((r for r in rows if P.norm(r.get("path") or "") == want), None)
        if target is None:
            raise HTTPException(404, f"索引里没有这首歌: {path}")
    elif q:
        nq = P.normalize_text(q)
        scored: List[Tuple[float, float, Dict[str, Any]]] = []
        for r in rows:
            sc = P.fuzzy_match(q, r.get("name"), r.get("title"), r.get("artist"),
                               r.get("album"), r.get("rel_path"))
            if sc <= 0:
                continue
            # 光看 fuzzy_match 不够：子串命中一律 1.0，搜 "Beginning" 时
            # 曲名正好叫 Beginning 的和文件名叫 "...Beginning..." 的会同分，
            # 再按字母序排就会把不相干的排前面。补一个"匹配得多完整"的分。
            full = 0.0
            for val in (r.get("title"), re.sub(r"\.[A-Za-z0-9]{2,5}$", "",
                                               str(r.get("name") or ""))):
                nv = P.normalize_text(str(val or ""))
                if not nv:
                    continue
                if nv == nq:
                    full = max(full, 1.0)          # 曲名就等于关键词
                elif nv.startswith(nq):
                    full = max(full, 0.8)
                elif nq in nv:
                    full = max(full, 0.6)
            scored.append((sc, full, r))
        if not scored:
            raise HTTPException(404, f"没搜到匹配「{q}」的歌")
        scored.sort(key=lambda x: (-x[0], -x[1], str(x[2].get("name") or "").lower()))
        if scored[0][0] < 0.45:
            raise HTTPException(404, f"没搜到匹配「{q}」的歌")
        target = scored[0][2]
        others = [{"path": r.get("path"), "name": r.get("name"),
                   "title": r.get("title"), "artist": r.get("artist"),
                   "album": r.get("album"),
                   "folder": OG._folder_of(OG._rel(root, r.get("path") or ""))}
                  for _s, _f, r in scored[1:6]]
    else:
        raise HTTPException(400, "需要给 q（关键词）或 path（曲目路径）")

    rec = dict(target)
    cur = OG._rel(root, rec.get("path") or "")
    cur_folder = OG._folder_of(cur)
    artist = str(rec.get("artist") or "").strip()
    album = str(rec.get("album") or "").strip()
    name = rec.get("name") or ""
    nname = P.normalize_text(name)
    key_a, key_al = P.normalize_text(artist), P.normalize_text(album)

    def dirname_rel(f: str) -> bool:
        return OG.folder_matches_name(f, artist) or OG.folder_matches_name(f, album)

    cands: List[Dict[str, Any]] = []
    for folder, d in folders.items():
        if folder == cur_folder:
            continue
        score, why = 0, []
        leaf = P.normalize_text(folder.rsplit("/", 1)[-1] if folder else "")
        # 目录名本身对得上
        if key_al and (leaf == key_al or key_al in leaf):
            score += 4
            why.append(f"目录名就是它的专辑「{album}」")
        elif key_a and (leaf == key_a or key_a in leaf):
            score += 2
            why.append(f"目录名就是它的作者「{artist}」")
        # 目录里已有的歌支持
        if album and d["albums"].get(album):
            score += 3
            why.append(f"该目录已有 {d['albums'][album]} 首同专辑《{album}》")
        if artist and d["artists"].get(artist):
            score += 1
            why.append(f"该目录已有 {d['artists'][artist]} 首同作者的歌")
        # 文件名里出现了这个目录的主流特征 → 更强的证据
        top = None
        if d["albums"]:
            top = max(d["albums"].items(), key=lambda x: x[1])
        if top and top[1] >= 2 and P.normalize_text(top[0]) in nname:
            score += 1
            why.append(f"文件名与目录里的《{top[0]}》一致")
        if score <= 0:
            continue
        if folder in sinks:
            why.append("（这是收纳区，放错位置的歌也可以先收进来）")
        cands.append({
            "folder": folder,
            "count": d["count"],
            "is_sink": folder in sinks,
            "score": score,
            "why": why,
            "sample": [i.get("name") for i in d["items"][:5]],
        })
    cands.sort(key=lambda x: (-x["score"], -x["count"]))

    verdict = _verdict(rec, cur_folder, folders, sinks)
    # 结论说"位置正确"，但存在明显更强的候选 → 这是**信息冲突**，如实说出来。
    # 真实例子：`Minecraft - Volume Alpha/Beginning - C418.flac` 的专辑标签
    # 写成了 Volume Beta，于是 Volume Beta 目录的得分很高。工具不替用户裁决
    # 标签和文件名哪个对，但要让人看见这个矛盾。
    if verdict.get("state") == "ok" and cands:
        best = cands[0]
        if best["score"] >= 4:
            verdict["conflict"] = {
                "folder": best["folder"],
                "score": best["score"],
                "why": best["why"],
                "detail": f"按标签它更像属于「{best['folder']}」，"
                          "但看起来它现在的位置也没问题（文件名/同作者聚集支持现状）。"
                          "两套信息矛盾，需要你自己确认以哪个为准。",
            }

    return {
        "source": sid, "root": root,
        "track": {
            "path": rec.get("path"), "name": name,
            "title": rec.get("title"), "artist": artist, "album": album,
            "folder": cur_folder, "rel": cur,
            "size": rec.get("size"),
            "human_size": P.human_size(rec.get("size")),
            "child_of": OG._folder_of(cur),
            "is_derivative": OG.is_derivative(name, rec.get("title") or ""),
            "filename_artist": OG.filename_artist(name),
        },
        "verdict": verdict,
        "candidates": cands[:max(1, min(200, int(limit)))],
        "sinks": sorted(sinks),
        "also_matched": others,
    }


def _verdict(rec: Dict[str, Any], cur_folder: str,
             folders: Dict[str, Dict[str, Any]], sinks: set) -> Dict[str, Any]:
    """给这首歌下个结论：位置对不对、为什么。"""
    name = rec.get("name") or ""
    if cur_folder in sinks or not cur_folder:
        return {"state": "sink", "label": "在收纳区里",
                "detail": "这个目录本来就是有意混放各种歌的，不需要动"}
    if OG.is_derivative(name, rec.get("title") or ""):
        fa = OG.filename_artist(name)
        if fa and OG.folder_matches_name(cur_folder, fa):
            return {"state": "ok", "label": "位置正确",
                    "detail": f"派生作品（remix/翻唱），文件名署名「{fa}」与所在目录一致；"
                              "标签里的 artist 是原作者"}
        return {"state": "unsure", "label": "疑似派生作品",
                "detail": "remix/翻唱类的标签常写原作者，名称又对不上所在目录，"
                          "建议你自己确认"}
    if OG.folder_fits(cur_folder, rec):
        return {"state": "ok", "label": "位置正确",
                "detail": f"目录名「{cur_folder.rsplit('/', 1)[-1]}」与它的作者/专辑对得上"}
    d = folders.get(cur_folder) or {}
    if OG.has_same_artist_cohort(cur_folder, rec, folders):
        return {"state": "ok", "label": "位置正确",
                "detail": "该目录里已经有同作者的歌（专辑标签或目录名可能有拼写差异）"}
    if _folder_of_rel(cur_folder) and OG.folder_fits(
            OG._folder_of(cur_folder), rec):
        return {"state": "ok", "label": "位置正确",
                "detail": "上层目录与它的作者/专辑对得上"}
    return {"state": "misplaced", "label": "疑似放错位置",
            "detail": "所在目录的名字跟它的作者/专辑都对不上，"
                      "下面的候选目录里有更合适的位置"}


def _folder_of_rel(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


@router.post("/api/organize/plan-paths")
def organize_plan_paths(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """**只为一组指定文件**推算去向，规则与「智能整理」完全相同。

    候选区、入库这些地方要的是"我挑的这几个文件各自该去哪"，不是
    "把整个库重排一遍"。所以这里把规划范围**限定在传入的 paths** 上，
    但库里已有的目录画像仍然读全量（否则找不到可复用的现成目录）。

    入参：
      `source`   数据源
      `paths`    要处理的文件路径（绝对或相对都行）
      `group_by` / `structure` / `min_group` 同「智能整理」
    """
    sid = str(payload.get("source") or "")
    if not sid:
        raise HTTPException(400, "缺少 source")
    raw_paths = payload.get("paths") or []
    if not isinstance(raw_paths, list) or not raw_paths:
        raise HTTPException(400, "paths 不能为空")
    _src, root = _resolve_src(sid)

    want: set = set()
    for p in raw_paths:
        s = P.norm(str(p))
        if not s:
            continue
        want.add(s)
        if not s.startswith("/") and not re.match(r"^[A-Za-z]:", s):
            want.add(P.join(root, s))
    if not want:
        raise HTTPException(400, "paths 里没有有效路径")

    try:
        min_group = max(1, min(100, int(payload.get("min_group") or 5)))
    except (TypeError, ValueError):
        raise HTTPException(400, "min_group 必须是数字")
    group_by = str(payload.get("group_by") or "artist_album")
    if group_by not in ("artist", "album", "artist_album"):
        raise HTTPException(400, f"不支持的分组依据: {group_by}")
    structure = str(payload.get("structure") or "nested")
    if structure not in ("flat", "nested"):
        raise HTTPException(400, f"不支持的结构: {structure}")

    rows = _track_rows(sid)
    # 关键：规划只喂选中的文件；但"库内有哪些目录"仍由 plan_by_album
    # 从这批文件里重建 —— 目录画像来自全量更准，所以这里把全量行也带上，
    # 只让**分组**受限于选中的文件。
    all_rows = rows
    picked = [r for r in rows if P.norm(str(r.get("path") or "")) in want]
    if not picked:
        return {"moves": [], "actions": [], "summary": {"total": 0, "move": 0},
                "note": "这些文件还不在索引里（可能刚上传，尚未扫描）"}
    plan = OG.plan_by_album(
        picked, root, min_group=min_group, group_by=group_by,
        # ★ small_policy 优先；旧入参 keep_flat_below 保留兼容：
        #   显式传 False 等价于 small_policy="sink"。
        small_policy=_small_policy(payload),
        keep_flat_below=payload.get("keep_flat_below",
                                    (structure == "nested")),
        folder_pool_rows=all_rows)
    plan["source"] = sid
    plan["scoped_paths"] = len(picked)
    plan["requested_paths"] = len(want)
    return plan


@router.post("/api/organize/album-plan")
def organize_album_plan(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """生成整理方案（按分组依据分组，输出目标目录与依据）。

    `group_by`   分组依据：`artist` 仅作者 / `album` 仅专辑 / `artist_album` 作者+专辑
    `structure`  目标目录结构：`flat` 单层 / `nested` 两层
    `scopes`     要**忽略**的目录（界面上勾掉的）
    `min_group`  一组达到几首才单独建目录（默认 5）
    `sinks`      收纳区目录（自动识别的 + 手动勾选的）
    `custom`     自定义映射：专辑名或作者名 → 指定目录（优先级最高）

    ★ **默认后台跑**。实测真实库（2301 首）按专辑分组要 **6.0 秒**、
    按作者 1.6 秒 —— 同步返回时界面只有一个转圈，不知道进度也没法取消，
    用户以为卡死就会刷新甚至强杀进程。这和查重/对比早就后台化的做法不一致。

    `payload.wait=true` 时仍然同步返回结果（脚本、测试、以及"小库秒回"的
    场景不必多一次往返）；界面走后台 + 轮询进度。
    """
    from .core.bgjob import run_in_background

    if payload.get("wait"):
        return _album_plan_core(payload)

    def work(job):
        return _album_plan_core(payload, job)

    job = run_in_background("organize", 100, work)
    return {"job": job.id, "async": True,
            "hint": "方案已在后台开始生成；用 /api/jobs/{id} 查进度，"
                    "完成后结果显示在 result"}


def _album_plan_core(payload: Dict[str, Any], job=None) -> Dict[str, Any]:
    """实际计算。`job` 用来报进度 / 响应取消（没有 job 就同步跑完）。"""
    def step(pct: int, msg: str) -> None:
        if job is None:
            return
        job.total = 100
        job.done = max(0, min(100, int(pct)))
        job.message = msg
        job.touch()

    sid = payload.get("source")
    if not sid:
        raise HTTPException(400, "缺少 source")
    step(3, "读取数据源…")
    _src, root = _resolve_src(sid)
    try:
        min_group = int(payload.get("min_group") or OG.DEFAULT_MIN_GROUP)
    except (TypeError, ValueError):
        raise HTTPException(400, "min_group 必须是数字")
    min_group = max(1, min(100, min_group))

    group_by = str(payload.get("group_by") or "artist_album")
    if group_by not in ("artist", "album", "artist_album"):
        raise HTTPException(400, f"不支持的分组依据: {group_by}")
    # 只有"作者+专辑"才谈得上层数；另外两种天然是单层
    structure = str(payload.get("structure") or "nested")
    if structure not in ("flat", "nested"):
        raise HTTPException(400, f"不支持的结构: {structure}")
    if group_by != "artist_album":
        structure = "flat"

    scopes = [_abs_in_root(sid, s) for s in as_list(payload, "scopes", "范围目录") if s]
    sinks = [_abs_in_root(sid, s) for s in as_list(payload, "sinks", "收纳目录") if s]
    # 映射规则：以**保存的规则**为底，请求里带来的覆盖它。
    # 这样用户在别处编辑过的映射不会因为向导没带上就失效。
    custom: Dict[str, str] = {}
    for k, v in (MA.get_mappings(sid) or {}).items():
        try:
            custom[k] = (_abs_in_root(sid, str(v)) if k == "__sink__"
                         else _abs_in_root(sid, str(v)))
        except Exception:
            continue
    custom_in = payload.get("custom") or {}
    if not isinstance(custom_in, dict):
        raise HTTPException(400, "custom 必须是对象（键=专辑/作者，值=目录）")
    for k, v in custom_in.items():
        if not k or not v:
            continue
        custom[str(k)] = _abs_in_root(sid, str(v))

    # 结构决定"未达门槛的组是否还留在作者目录里"：
    #   nested → 小专辑留在作者目录（不再分层），层级不乱
    #   flat   → 一律落到单层目录
    keep_flat_below = structure == "nested"

    step(20, "读取曲目…")
    rows = _track_rows(sid)
    if job is not None and job.cancel:
        return {"cancelled": True, "source": sid}
    step(45, f"计算分组（{len(rows)} 首）…")
    plan = OG.plan_by_album(rows, root, scopes=scopes, min_group=min_group,
                            sinks=sinks, custom=custom,
                            group_by=group_by,
                            keep_flat_below=keep_flat_below,
                            small_policy=str(payload.get("small_policy") or "keep"),
                            include_artist_fallback=bool(
                                payload.get("include_artist_fallback", True)))
    step(90, "整理结果…")
    plan["source"] = sid
    plan["root"] = root
    plan["group_by"] = group_by
    plan["structure"] = structure
    plan["generated_at"] = time.time()
    plan["hashes"] = _plan_hash(plan)
    step(100, "完成")
    return plan


@router.post("/api/organize/album-apply")
def organize_album_apply(payload: Dict[str, Any] = Body(...), exclusive: None = Depends(exclusive_guard)) -> Dict[str, Any]:
    """执行"按专辑整理"。可以只执行勾选的专辑（`only` 传专辑键）。"""
    sid = payload.get("source")
    if not sid:
        raise HTTPException(400, "缺少 source")
    src, root = _resolve_src(sid)
    _guard_write(src)
    m = _deps()

    plan = payload.get("plan")
    if not plan:
        # ★ 必须 wait=True：这个端点默认后台跑，不带 wait 会返回
        #   {"job": ...} 而拿不到 moves，紧接着的校验必然 400。
        plan = organize_album_plan({**payload, "source": sid, "wait": True})
    only = set(as_list(payload, "only", "要执行的组"))
    moves = plan.get("moves") or []
    if only:
        keys = {str(k) for k in only}
        actions = plan.get("actions") or []
        keep_paths = set()
        for a in actions:
            # 一个 action 可能由多个分组键**合并**而来（同一目标目录），
            # `keys` 记着它全部的来源键；只看 `key` 会漏掉其余键的曲目，
            # 表现是"勾了却没全搬"。
            akeys = {str(k) for k in (a.get("keys") or [a.get("key")])}
            if akeys & keys:
                keep_paths.update(i["path"] for i in (a.get("items") or []))
        moves = [mv for mv in moves if mv.get("path") in keep_paths]
    if not moves:
        raise HTTPException(400, "没有需要移动的文件")

    expect = payload.get("plan_hash")
    if expect and plan.get("hashes") and expect != plan.get("hashes"):
        raise HTTPException(409, "方案已变化，请重新生成后再执行")

    execute = bool(payload.get("execute", False))
    if execute and not payload.get("confirm"):
        raise HTTPException(400, "执行写操作需要 confirm=true")
    policy = str(payload.get("policy") or "skip")
    if policy not in ("skip", "rename", "overwrite"):
        raise HTTPException(400, f"不支持的同名冲突策略: {policy}")

    steps = [{
        "id": f"alb-{i}", "op": "move", "kind": "organize",
        "src": sid, "src_path": mv.get("path"), "dst": sid,
        "dst_path": mv.get("dst_path"), "size": int(mv.get("size") or 0),
    } for i, mv in enumerate(moves)]
    ex = m.Executor({sid: src}, m.db() if execute else None, default_policy=policy)
    res = ex.run(steps, execute=execute, policy=policy, verify=True,
                 workers=int(payload.get("workers") or 3))
    summary = dict(res.get("summary") or {})
    return {
        "dry_run": not execute, "policy": policy, "source": sid,
        "results": res.get("results", []), "summary": summary,
        "moved": summary.get("done", 0), "failed": summary.get("error", 0),
        "skipped": summary.get("skipped", 0),
        "target_dirs": sorted({str(mv.get("dst_dir") or "") for mv in moves})[:200],
    }


@router.post("/api/organize/plan")
def organize_plan(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """生成整理方案（只算不动）。

    默认策略是"只修放错位置的单曲"：尊重用户已有的目录体系
    （单曲01、英语歌精选集、作者目录……），只在能证明某首歌走错门时才动它。
    """
    sid = payload.get("source")
    if not sid:
        raise HTTPException(400, "缺少 source")
    _src, root = _resolve_src(sid)
    try:
        min_group = int(payload.get("min_group") or OG.DEFAULT_MIN_GROUP)
    except (TypeError, ValueError):
        raise HTTPException(400, "min_group 必须是数字")
    min_group = max(1, min(100, min_group))

    # 用户手动指定的收纳区：允许传绝对路径或相对路径
    sinks_in = payload.get("sinks") or []
    if not isinstance(sinks_in, list):
        raise HTTPException(400, "sinks 必须是数组")
    sinks: List[str] = []
    for s in sinks_in:
        if not s:
            continue
        sinks.append(_abs_in_root(sid, s))

    scopes: List[str] = []
    for s in as_list(payload, "scopes", "范围目录"):
        if s:
            scopes.append(_abs_in_root(sid, s))
    # 兼容旧参数：path 等价于单元素 scopes
    if payload.get("path"):
        scopes.append(_abs_in_root(sid, payload["path"]))

    rows = _track_rows(sid)
    try:
        plan = OG.plan_relocate(
            rows, root,
            sinks=sinks, scopes=scopes,
            min_group=min_group,
            singles_dir=str(payload.get("singles_dir") or "单曲"),
            fallback_sink=(_abs_in_root(sid, payload["fallback_sink"])
                           if payload.get("fallback_sink") else ""),
            include_gather=bool(payload.get("include_gather", True)),
            orphan_policy=str(payload.get("orphan_policy") or "keep"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    # 数据源根作为"最终兜底落点"：优先用用户库里已有的收纳区
    plan["source"] = sid
    plan["root"] = root
    plan["generated_at"] = time.time()
    plan["hashes"] = _plan_hash(plan)
    return plan


def _plan_hash(plan: Dict[str, Any]) -> str:
    """给方案算个指纹，apply 时校验"用户确认的就是这份方案"。

    ★ 指纹要包含**源文件的身份**（大小），不只是 (path, dst_dir)。

    原来只把 `path` 和 `dst_dir` 拼进去，于是这种改动检测不到：

        同一路径的文件**被替换**了（重新下载、换了质量、文件损坏后修复）
        → path 没变、dst_dir 没变 → 指纹一模一样
        → 用户点"执行"时搬走的是**他确认时没看过的那个文件**

    大小是最便宜的"这还是不是同一个文件"的判据：同格式重下几乎不可能
    刚好同字节数。带上它，文件换了就会 409 要求重新生成方案。
    （不用 mtime：重新扫描/同步工具碰一下就会变，会误报。）
    """
    import hashlib
    h = hashlib.sha1()
    for m in plan.get("moves", []):
        h.update((str(m.get("path")) + ">" + str(m.get("dst_dir"))
                  + "@" + str(m.get("size") or "")).encode("utf-8"))
    return h.hexdigest()[:16]


@router.post("/api/organize/apply")
def organize_apply(payload: Dict[str, Any] = Body(...), exclusive: None = Depends(exclusive_guard)) -> Dict[str, Any]:
    """执行整理方案。可以直接传 plan（界面复用它算好的那份），或重算一遍。"""
    sid = payload.get("source")
    if not sid:
        raise HTTPException(400, "缺少 source")
    src, root = _resolve_src(sid)
    _guard_write(src)
    m = _deps()

    plan = payload.get("plan")
    if not plan:
        # 没传就按同样参数现算一份，保证"界面看到的"和"执行的"一致
        plan = organize_plan({**payload, "source": sid})
    moves = plan.get("moves") or []
    if not moves:
        raise HTTPException(400, "这份方案里没有需要移动的文件")

    expect = payload.get("plan_hash")
    if expect and plan.get("hashes") and expect != plan.get("hashes"):
        raise HTTPException(409, "方案已变化（可能又重新扫描过），请重新生成后再执行")

    only = set(as_list(payload, "only", "要执行的组"))
    if only:
        moves = [x for x in moves if x.get("path") in only]
        if not moves:
            raise HTTPException(400, "选中的步骤为空")

    execute = bool(payload.get("execute", False))
    if execute and not payload.get("confirm"):
        raise HTTPException(400, "执行写操作需要 confirm=true")
    policy = str(payload.get("policy") or "skip")
    if policy not in ("skip", "rename", "overwrite"):
        raise HTTPException(400, f"不支持的同名冲突策略: {policy}")

    plan_steps: List[Dict[str, Any]] = []
    for i, mv in enumerate(moves):
        plan_steps.append({
            "id": f"org-{i}", "op": "move", "kind": "organize",
            "src": sid, "src_path": mv.get("path"), "dst": sid,
            "dst_path": mv.get("dst_path"), "size": int(mv.get("size") or 0),
        })
    ex = m.Executor({sid: src}, m.db() if execute else None, default_policy=policy)
    res = ex.run(plan_steps, execute=execute, policy=policy, verify=True,
                 workers=int(payload.get("workers") or 3))
    summary = dict(res.get("summary") or {})
    created = sorted({str(mv.get("dst_dir") or "") for mv in moves if mv.get("dst_dir")})
    return {
        "dry_run": not execute, "policy": policy, "source": sid,
        "results": res.get("results", []), "summary": summary,
        "target_dirs": created[:200],
        "moved": summary.get("done", 0), "failed": summary.get("error", 0),
        "skipped": summary.get("skipped", 0),
    }


@router.post("/api/organize/folder")
def organize_folder(payload: Dict[str, Any] = Body(...), exclusive: None = Depends(exclusive_guard)) -> Dict[str, Any]:
    """对**单个目录**做整理：把它里面的歌按作者/专辑分到子目录去。

    给"这个文件夹太乱了，就地拆一下"用，比整库整理快也更好理解。
    """
    sid = payload.get("source")
    path = payload.get("path")
    if not sid or not path:
        raise HTTPException(400, "缺少 source / path")
    src, root = _resolve_src(sid)
    target = _abs_in_root(sid, path)
    if target == root:
        raise HTTPException(400, "这是数据源根目录，请指明要整理的子目录")
    if not src.exists(target):
        raise HTTPException(404, f"目录不存在: {path}")

    mode = str(payload.get("mode") or "artist_album")
    if mode not in ("artist_album", "artist", "album"):
        raise HTTPException(400, f"不支持的整理方式: {mode}")
    min_group = max(1, min(100, int(payload.get("min_group") or OG.DEFAULT_MIN_GROUP)))
    template = str(payload.get("template") or "{artist}/{album}")

    rows = [r for r in _track_rows(sid)
            if P.parent(P.norm(r.get("path") or "")) == target]
    if not rows:
        raise HTTPException(400, "这个目录里没有已索引的曲目（先扫描一次）")

    # 单目录整理：只看这个目录里的歌，去处仍然可以是库里别处的现成目录
    all_rows = _track_rows(sid)
    plan = OG.plan_relocate(
        all_rows, root,
        sinks=payload.get("sinks") or [],
        scopes=[target],
        min_group=max(1, min(100, int(payload.get("min_group") or OG.DEFAULT_MIN_GROUP))),
        singles_dir=str(payload.get("singles_dir") or "单曲"),
        fallback_sink=(_abs_in_root(sid, payload["fallback_sink"])
                       if payload.get("fallback_sink") else ""),
        include_gather=bool(payload.get("include_gather", True)),
        orphan_policy=str(payload.get("orphan_policy") or "keep"),
    )
    # 目标目录要相对"数据源根"，这样 Executor 搬完位置才是对的
    base_rel = target[len(root) + 1:] if target.startswith(root + "/") else ""
    for mv in plan.get("moves", []):
        rel = mv.get("dst_dir") or ""
        abs_dir = P.join(root, base_rel, rel) if base_rel else P.join(root, rel)
        mv["dst_dir"] = abs_dir
        mv["dst_path"] = P.join(abs_dir, mv.get("name") or "")

    execute = bool(payload.get("execute", False))
    if execute and not payload.get("confirm"):
        raise HTTPException(400, "执行写操作需要 confirm=true")
    if not execute:
        return {"dry_run": True, "plan": plan, "source": sid, "path": target,
                "summary": plan["summary"]}

    _guard_write(src)
    m = _deps()
    moves = plan.get("moves") or []
    only = set(as_list(payload, "only", "要执行的组"))
    if only:
        moves = [x for x in moves if x.get("path") in only]
    if not moves:
        raise HTTPException(400, "没有需要移动的文件")
    policy = str(payload.get("policy") or "skip")
    ex = m.Executor({sid: src}, m.db(), default_policy=policy)
    steps = [{
        "id": f"orgf-{i}", "op": "move", "kind": "organize",
        "src": sid, "src_path": mv.get("path"), "dst": sid,
        "dst_path": mv.get("dst_path"), "size": int(mv.get("size") or 0),
    } for i, mv in enumerate(moves)]
    res = ex.run(steps, execute=True, policy=policy, verify=True,
                 workers=int(payload.get("workers") or 3))
    summary = dict(res.get("summary") or {})
    return {"dry_run": False, "policy": policy, "source": sid, "path": target,
            "results": res.get("results", []), "summary": summary,
            "moved": summary.get("done", 0), "failed": summary.get("error", 0),
            "skipped": summary.get("skipped", 0)}
